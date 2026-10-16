"""
sync_manager.py — Sincronização de blocos entre nós BRN (v8)
================================================================
v8:
  [NEW] CRÍTICO: sync loop agora tenta TODOS os peers em sequência.
        Se o "melhor" falhar (resposta vazia, altura 0), tenta o
        próximo. Antes, ficava travado no primeiro (que podia ser
        o próprio nó ou um peer inválido).
  [NEW] Filtra automaticamente o próprio IP do sync (evita
        registrar o próprio nó como peer).
  [NEW] Log detalhado ao consultar cada peer (INFO em vez de DEBUG).
v7:
  [FIX] _refresh_peers_from_p2p inclui peers já registrados.
v6:
  [FIX] apply_block_batch checa se bloco já existe.
  [FIX] local_height normaliza valores < -1.
================================================================
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import threading
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger("sync")


NETWORK_MAGIC         = b"BRN5"
MAX_MSG_SIZE          = 8 * 1024 * 1024
DEFAULT_TIMEOUT       = 10.0
DEFAULT_BATCH_SIZE    = int(os.environ.get("BRN_SYNC_BATCH", "50"))
DEFAULT_INTERVAL_S    = float(os.environ.get("BRN_SYNC_INTERVAL", "5"))
BATCH_DELAY_S         = float(os.environ.get("BRN_SYNC_BATCH_DELAY", "0.06"))


# ============================================================
# DESCOBERTA DO PRÓPRIO IP (para filtrar do sync)
# ============================================================
_MY_IP_CACHE: Optional[str] = None


def _detect_my_ip() -> str:
    """Descobre o IP Tailscale local (cacheado)."""
    global _MY_IP_CACHE
    if _MY_IP_CACHE is not None:
        return _MY_IP_CACHE

    # Tenta tailscale ip -4
    try:
        r = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if line.startswith("100."):
                    _MY_IP_CACHE = line
                    return line
    except Exception:
        pass

    # Fallback: variáveis de ambiente
    for key in ("BRN_MY_IP", "TS_IP"):
        v = os.environ.get(key, "").strip()
        if v:
            _MY_IP_CACHE = v
            return v

    _MY_IP_CACHE = ""
    return ""


@dataclass
class PeerStatus:
    node_id: str
    ip: str
    port: int
    height: int = 0
    cumulative_work: int = 0
    last_seen: float = field(default_factory=time.time)
    latency_ms: float = 0.0
    fail_count: int = 0
    last_error: str = ""

    @property
    def addr(self) -> str:
        return f"{self.ip}:{self.port}"


@dataclass
class SyncStatus:
    mode: str = "full"
    local_height: int = 0
    target_height: int = 0
    synced: bool = False
    syncing: bool = False
    progress: float = 0.0
    active_peer: Optional[str] = None
    peers_known: int = 0
    peers_synced: int = 0
    last_error: str = ""


class ChainSyncManager:
    def __init__(self, blockchain, db, p2p_manager,
                 send_fn: Optional[Callable[[str, int, dict, float], Optional[dict]]] = None):
        self.chain = blockchain
        self.db = db
        self.p2p = p2p_manager
        self._send_fn = send_fn

        self.status = SyncStatus()
        self.peers: Dict[str, PeerStatus] = {}
        self.peer_lock = threading.Lock()
        self.sync_lock = threading.Lock()

        self.batch_size = DEFAULT_BATCH_SIZE
        self.spv_headers_only = False

        self._stop_sync = threading.Event()
        self.sync_thread: Optional[threading.Thread] = None

        self._has_save_header = hasattr(self.db, "save_header")
        self._my_ip = _detect_my_ip()

        if self._my_ip:
            log.info("sync: IP proprio detectado: %s", self._my_ip)

    # ============================================================
    # SETUP
    # ============================================================
    def set_sync_mode(self, mode: str = "full") -> None:
        if mode not in ("full", "spv"):
            raise ValueError("Modo inválido: use 'full' ou 'spv'")
        if mode == "spv" and not self._has_save_header:
            log.warning("SPV indisponível — usando FULL")
            mode = "full"
        self.spv_headers_only = (mode == "spv")
        self.status.mode = mode
        log.info("Modo de sincronização: %s", mode.upper())

    def register_peer(self, node_id: str, ip: str, port: int,
                      height: int = 0, cumulative_work: int = 0) -> None:
        if not node_id:
            return

        # ⭐ [v8] Filtra o próprio IP
        if self._my_ip and ip == self._my_ip:
            log.info("sync: ignorando peer proprio %s:%d", ip, port)
            return

        with self.peer_lock:
            existing = self.peers.get(node_id)
            if existing:
                existing.height = height
                existing.cumulative_work = cumulative_work
                existing.last_seen = time.time()
                existing.ip = ip
                existing.port = port
            else:
                self.peers[node_id] = PeerStatus(
                    node_id=node_id, ip=ip, port=port,
                    height=height, cumulative_work=cumulative_work,
                )
            self.status.peers_known = len(self.peers)

    def unregister_peer(self, node_id: str) -> None:
        with self.peer_lock:
            self.peers.pop(node_id, None)
            self.status.peers_known = len(self.peers)

    # ============================================================
    # [v8] Retorna TODOS os peers ordenados (não só o melhor)
    # ============================================================
    def get_all_peers_sorted(self) -> List[PeerStatus]:
        """Retorna peers ordenados por (work, height) desc — filtra os vazios."""
        with self.peer_lock:
            peers = list(self.peers.values())

        # Filtra: só peers com altura > local
        local = self._safe_height()
        candidates = [p for p in peers if p.height > local]

        # Ordena: maior work/height primeiro
        candidates.sort(key=lambda p: (p.cumulative_work, p.height), reverse=True)
        return candidates

    def get_best_peer(self) -> Optional[PeerStatus]:
        peers = self.get_all_peers_sorted()
        return peers[0] if peers else None

    select_best_chain_peer = get_best_peer

    # ============================================================
    # REFRESH PEERS
    # ============================================================
    def _refresh_peers_from_p2p(self) -> int:
        p2p = self.p2p
        if p2p is None:
            return 0

        addrs_set: set = set()

        # 1a) p2p.discovery
        disc = getattr(p2p, "discovery", None)
        if disc is not None and hasattr(disc, "listar_peers"):
            try:
                for a in (disc.listar_peers() or []):
                    if a:
                        addrs_set.add(str(a).strip())
            except Exception:
                pass

        # 1b) p2p.listar_peers
        if hasattr(p2p, "listar_peers"):
            try:
                for a in (p2p.listar_peers() or []):
                    if a:
                        addrs_set.add(str(a).strip())
            except Exception:
                pass

        # 1c) peers já registrados
        with self.peer_lock:
            for p in self.peers.values():
                if p.ip and p.port:
                    addrs_set.add(f"{p.ip}:{p.port}")

        addrs = [a for a in addrs_set if a]
        if not addrs:
            return 0

        updated = 0
        for addr in addrs:
            try:
                ip, _, port_s = addr.rpartition(":")
                port = int(port_s)
            except (ValueError, AttributeError):
                continue

            # ⭐ [v8] Pula o próprio IP
            if self._my_ip and ip == self._my_ip:
                continue

            # ⭐ [v8] Log INFO (era DEBUG)
            log.info("sync: consultando peer %s ...", addr)

            resp = None
            try:
                from p2p_unified import P2PClient
                resp, _lat = P2PClient.get_chain_height(ip, port)
            except Exception as e:
                log.warning("sync: get_chain_height(%s) ERRO: %s", addr, e)
                # Marca falha no peer
                with self.peer_lock:
                    for p in self.peers.values():
                        if p.ip == ip and p.port == port:
                            p.fail_count += 1
                            p.last_error = str(e)[:80]
                continue

            if not resp:
                log.warning("sync: peer %s respondeu VAZIO", addr)
                with self.peer_lock:
                    for p in self.peers.values():
                        if p.ip == ip and p.port == port:
                            p.fail_count += 1
                            p.last_error = "resposta vazia"
                continue

            node_id = resp.get("_uuid") or addr
            try:
                height = int(resp.get("height", 0))
                work = int(resp.get("work", 0))
            except (ValueError, TypeError):
                continue

            log.info("sync: peer %s OK — altura=%d work=%d", addr, height, work)

            # Só re-registra se mudou
            with self.peer_lock:
                existing = self.peers.get(node_id) or self.peers.get(addr)
                if (existing
                        and existing.height == height
                        and existing.cumulative_work == work):
                    continue

            self.register_peer(node_id, ip, port, height, work)
            updated += 1

            if disc is not None and hasattr(disc, "register_uuid") and resp.get("_uuid"):
                try:
                    disc.register_uuid(resp["_uuid"], ip, port)
                except Exception:
                    pass

        return updated

    # ============================================================
    # SEND
    # ============================================================
    def _do_send(self, ip: str, port: int, msg: dict,
                 timeout: float = DEFAULT_TIMEOUT) -> Optional[dict]:
        if self._send_fn is not None:
            try:
                return self._send_fn(ip, port, msg, timeout)
            except Exception as e:
                log.debug("send_fn falhou: %s", e)
                return None

        try:
            from p2p_unified import P2PClient
            resp, _lat = P2PClient.send_message(ip, port, msg, timeout=timeout)
            return resp
        except Exception as e:
            log.debug("P2PClient falhou: %s", e)

        p2p = self.p2p
        if p2p is not None:
            for name in ("send_message", "send_to_peer", "send"):
                fn = getattr(p2p, name, None)
                if callable(fn):
                    try:
                        r = fn(ip, port, msg, timeout=timeout)
                        if isinstance(r, dict):
                            return r
                    except TypeError:
                        try:
                            r = fn(ip, port, msg)
                            if isinstance(r, dict):
                                return r
                        except Exception:
                            pass
                    except Exception:
                        pass

        return self._raw_rpc(ip, port, msg, timeout)

    @staticmethod
    def _raw_rpc(ip: str, port: int, msg: dict,
                 timeout: float) -> Optional[dict]:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((ip, port))
            s.sendall(NETWORK_MAGIC + json.dumps(msg).encode("utf-8"))
            raw = s.recv(MAX_MSG_SIZE)
            if not raw or not raw.startswith(NETWORK_MAGIC):
                return None
            return json.loads(raw[len(NETWORK_MAGIC):].decode("utf-8"))
        finally:
            try:
                s.close()
            except Exception:
                pass

    def request_block_batch(self, peer: PeerStatus,
                            start_height: int,
                            count: int) -> List[Dict]:
        payload = {
            "type": "get_blocks_range",
            "start": start_height,
            "end": start_height + count,
            "headers_only": self.spv_headers_only,
        }
        t0 = time.time()
        resp = self._do_send(peer.ip, peer.port, payload, DEFAULT_TIMEOUT)
        peer.latency_ms = (time.time() - t0) * 1000.0

        if resp is None:
            log.debug("get_blocks_range(%d-%d) -> None",
                      start_height, start_height + count)
            return []

        if "blocks" not in resp:
            log.debug("get_blocks_range(%d-%d) sem 'blocks'",
                      start_height, start_height + count)
            return []

        blocks = resp["blocks"]
        if not isinstance(blocks, list):
            return []
        return blocks

    # ============================================================
    # APPLY
    # ============================================================
    def _block_exists(self, height: int) -> bool:
        try:
            return self.db.get_block(height) is not None
        except Exception:
            return False

    def apply_block_batch(self, blocks: List[Dict]) -> Tuple[int, int]:
        applied = 0
        failed = 0

        for block_data in blocks:
            try:
                h = block_data.get("height")

                if h is not None and self._block_exists(h):
                    applied += 1
                    continue

                if self.spv_headers_only and self._has_save_header:
                    self.db.save_header(
                        block_data["height"],
                        block_data["hash"],
                        block_data["prev_hash"],
                        block_data.get("merkle_root", ""),
                        block_data.get("timestamp", 0),
                    )
                    applied += 1
                    continue

                ok, msg = self.chain.accept_block(block_data)
                if ok:
                    applied += 1
                else:
                    log.warning("accept_block falhou em #%s: %s", h, msg)
                    failed += 1
                    if failed >= 1:
                        break
            except Exception as e:
                log.warning("Erro aplicando bloco #%s: %s",
                            block_data.get("height"), e)
                failed += 1
                break

        return applied, failed

    # ============================================================
    # SYNC WITH PEER
    # ============================================================
    def sync_with_peer(self, peer: PeerStatus) -> bool:
        local_h = self._safe_height()
        self.status.local_height = local_h
        self.status.target_height = peer.height
        self.status.last_error = ""

        if local_h >= peer.height:
            self.status.synced = True
            return True

        log.info("Sync com %s — local=%d remoto=%d",
                 peer.node_id[:12], local_h, peer.height)

        while not self._stop_sync.is_set():
            local_h = self._safe_height()
            if local_h >= peer.height:
                break

            current = local_h + 1
            end = min(current + self.batch_size - 1, peer.height)
            count = end - current + 1

            blocks = self.request_block_batch(peer, current, count)

            if not blocks:
                self.status.last_error = f"lote {current}..{end} vazio"
                peer.fail_count += 1
                peer.last_error = "lote vazio"
                if self._stop_sync.wait(timeout=2):
                    break
                # ⭐ [v8] Sai e tenta outro peer
                log.warning("Peer %s não retornou blocos — tentando outro",
                            peer.node_id[:12])
                return False

            applied, failed = self.apply_block_batch(blocks)

            self.status.local_height = self._safe_height()
            target = max(1, self.status.target_height)
            self.status.progress = min(
                100.0, self.status.local_height / target * 100.0
            )

            log.info("Sync %d/%d (%.1f%%) — +%d ok, %d falhas",
                     self.status.local_height, peer.height,
                     self.status.progress, applied, failed)

            if applied == 0:
                log.warning("Nenhum bloco aplicado — abortando este peer")
                self.status.last_error = "lote rejeitado"
                peer.fail_count += 1
                peer.last_error = "lote rejeitado"
                return False

            if failed > 0:
                log.warning("Parando sync: %d falha(s) no lote", failed)
                self.status.last_error = f"{failed} falha(s)"
                peer.fail_count += 1
                peer.last_error = f"{failed} falha(s)"
                return False

            if self._stop_sync.wait(timeout=BATCH_DELAY_S):
                break

        if not self._stop_sync.is_set():
            self.status.synced = (self._safe_height() >= peer.height)
            if self.status.synced:
                log.info("Sincronizado com %s", peer.node_id[:12])

        return self.status.synced

    # ============================================================
    # [v8] SYNC LOOP — itera por TODOS os peers
    # ============================================================
    def start_sync_loop(self) -> None:
        if self.sync_lock.locked():
            log.debug("sync loop já está rodando")
            return
        self.sync_lock.acquire()
        self._stop_sync.clear()
        self.status.syncing = True

        def _loop():
            try:
                while not self._stop_sync.is_set():
                    try:
                        # 1) Atualiza alturas dos peers conhecidos
                        n = self._refresh_peers_from_p2p()
                        if n > 0:
                            log.info("sync: %d peer(s) atualizados", n)

                        # 2) Pega TODOS os peers que têm altura > local
                        candidates = self.get_all_peers_sorted()

                        if not candidates:
                            self.status.synced = True
                            if self._stop_sync.wait(timeout=DEFAULT_INTERVAL_S):
                                break
                            continue

                        log.info("sync: %d peer(s) com altura > local — tentando",
                                 len(candidates))

                        # 3) ⭐ Tenta CADA peer em sequência
                        synced_ok = False
                        for i, peer in enumerate(candidates, 1):
                            if self._stop_sync.is_set():
                                break

                            # Pula peers que falharam 3+ vezes
                            if peer.fail_count >= 3:
                                log.debug("sync: peer %s com %d falhas — pulando",
                                          peer.addr, peer.fail_count)
                                continue

                            log.info("sync: tentando peer %d/%d: %s (h=%d)",
                                     i, len(candidates), peer.addr, peer.height)

                            self.status.active_peer = peer.node_id

                            try:
                                ok = self.sync_with_peer(peer)
                            except Exception as e:
                                log.warning("sync_with_peer(%s) erro: %s",
                                            peer.addr, e)
                                ok = False

                            if ok:
                                synced_ok = True
                                break  # já sincronizou — não precisa mais

                            # Se não sincronizou, tenta o próximo
                            log.warning("sync: peer %s falhou — tentando proximo",
                                        peer.addr)

                        if not synced_ok and not self._stop_sync.is_set():
                            log.warning("sync: nenhum peer funcionou nesta rodada")

                    except Exception as e:
                        log.warning("sync loop erro: %s", e)
                        self.status.last_error = str(e)

                    if self._stop_sync.wait(timeout=DEFAULT_INTERVAL_S):
                        break
            finally:
                self.status.syncing = False
                self.status.active_peer = None
                if self.sync_lock.locked():
                    try:
                        self.sync_lock.release()
                    except RuntimeError:
                        pass

        self.sync_thread = threading.Thread(
            target=_loop, daemon=True, name="ChainSync",
        )
        self.sync_thread.start()

    def stop(self) -> None:
        self._stop_sync.set()
        t = self.sync_thread
        if t is not None and t.is_alive():
            t.join(timeout=5)

    # ============================================================
    # GET STATUS
    # ============================================================
    def get_status(self) -> Dict:
        local = self._safe_height()

        target = local
        active_peer = None
        peers_known = 0

        # Pega peers registrados
        with self.peer_lock:
            peers_list = list(self.peers.values())
            peers_known = len(peers_list)
            if peers_list:
                best = max(peers_list, key=lambda p: p.height)
                if best.height > target:
                    target = best.height
                    active_peer = best.addr

        if target > 0:
            progress = min(100.0, local / target * 100.0)
        else:
            progress = 100.0

        synced = (local >= target and target >= 0)

        return {
            "mode": "full",
            "local_height": local,
            "target_height": target,
            "synced": synced,
            "syncing": not synced and target > 0,
            "progress": round(progress, 2),
            "active_peer": active_peer,
            "peers_known": peers_known,
            "peers_synced": self.status.peers_synced,
            "last_error": self.status.last_error,
        }

    # ============================================================
    # SAFE HEIGHT
    # ============================================================
    def _safe_height(self) -> int:
        try:
            h = int(self.chain.db.height())
        except Exception:
            return -1
        if h < -1:
            log.debug("_safe_height: valor anômalo %d, normalizando para -1", h)
            return -1
        return h

    def __enter__(self):
        self.start_sync_loop()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False
