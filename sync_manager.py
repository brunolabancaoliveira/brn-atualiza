"""
sync_manager.py — Sincronização de blocos entre nós BRN (v6)
================================================================
v6:
  [FIX] CRÍTICO: apply_block_batch checa se bloco já existe antes de
        aplicar. Antes, se o p2p_unified interno já tinha baixado os
        blocos, o sync_manager batia em "UNIQUE constraint failed:
        blocks.height" e abortava o peer.
  [FIX] local_height agora usa max(-1, height) — antes podia mostrar
        valores negativos estranhos (ex: -1838) se a query SQLite
        retornasse algo inesperado.
  [FIX] log "Sync X/Y" mostra o local_height atual em vez do local
        capturado no início do loop.
v5:
  [FIX] get_status() espelha o estado real do p2p.discovery
v4:
  [FIX] Sleep entre lotes (60ms) — respeita RATE_LIMIT_MSGS_PER_SEC
  [FIX] Progresso parcial não é descartado quando um lote falha
v3:
  [FIX] refresh periódico de peers do p2p.discovery
================================================================
"""
from __future__ import annotations

import json
import os
import socket
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


@dataclass
class PeerStatus:
    node_id: str
    ip: str
    port: int
    height: int = 0
    cumulative_work: int = 0
    last_seen: float = field(default_factory=time.time)
    latency_ms: float = 0.0

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

    def set_sync_mode(self, mode: str = "full") -> None:
        if mode not in ("full", "spv"):
            raise ValueError("Modo inválido: use 'full' ou 'spv'")
        if mode == "spv" and not self._has_save_header:
            log.warning("Modo SPV solicitado, mas db.save_header não existe — "
                        "caindo para FULL")
            mode = "full"
        self.spv_headers_only = (mode == "spv")
        self.status.mode = mode
        log.info("Modo de sincronização: %s", mode.upper())

    def register_peer(self, node_id: str, ip: str, port: int,
                      height: int = 0, cumulative_work: int = 0) -> None:
        if not node_id:
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

    def get_best_peer(self) -> Optional[PeerStatus]:
        local_h = self._safe_height()
        with self.peer_lock:
            if not self.peers:
                return None
            best = max(
                self.peers.values(),
                key=lambda p: (p.cumulative_work, p.height),
            )
            if best.height <= local_h:
                return None
            return best

    select_best_chain_peer = get_best_peer

    def _refresh_peers_from_p2p(self) -> int:
        p2p = self.p2p
        if p2p is None:
            return 0

        addrs: list[str] = []
        disc = getattr(p2p, "discovery", None)
        if disc is not None and hasattr(disc, "listar_peers"):
            try:
                addrs = disc.listar_peers() or []
            except Exception:
                addrs = []
        if not addrs and hasattr(p2p, "listar_peers"):
            try:
                addrs = p2p.listar_peers() or []
            except Exception:
                addrs = []

        if not addrs:
            return 0

        updated = 0
        for addr in addrs:
            try:
                ip, _, port_s = addr.rpartition(":")
                port = int(port_s)
            except (ValueError, AttributeError):
                continue

            try:
                from p2p_unified import P2PClient
                resp, lat = P2PClient.get_chain_height(ip, port)
            except Exception:
                continue

            if not resp:
                continue

            node_id = resp.get("_uuid") or addr
            height = int(resp.get("height", 0))
            work = int(resp.get("work", 0))

            self.register_peer(node_id, ip, port, height, work)
            updated += 1

            if disc is not None and hasattr(disc, "register_uuid") and resp.get("_uuid"):
                try:
                    disc.register_uuid(resp["_uuid"], ip, port)
                except Exception:
                    pass

        return updated

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
    # [v6] apply_block_batch — pula blocos que já existem
    # ============================================================
    def _block_exists(self, height: int) -> bool:
        """Verifica se um bloco já está no DB."""
        try:
            return self.db.get_block(height) is not None
        except Exception:
            return False

    def apply_block_batch(self, blocks: List[Dict]) -> Tuple[int, int]:
        """
        Aplica blocos. Retorna (aplicados, pulados/falhas).

        v6: se o bloco já existe no DB (baixado por outro sync, por ex.
        o p2p_unified interno), conta como aplicado e pula — antes
        lançava UNIQUE constraint e abortava o peer.
        """
        applied = 0
        failed = 0

        for block_data in blocks:
            try:
                h = block_data.get("height")

                # Skip se já existe
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
                if self._stop_sync.wait(timeout=2):
                    break
                continue

            applied, failed = self.apply_block_batch(blocks)

            # [v6] Recalcula sempre — outro sync pode ter avançado o DB
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
                break

            if failed > 0:
                log.warning("Parando sync: %d falha(s) no lote", failed)
                self.status.last_error = f"{failed} falha(s)"
                break

            if self._stop_sync.wait(timeout=BATCH_DELAY_S):
                break

        if not self._stop_sync.is_set():
            self.status.synced = (self._safe_height() >= peer.height)
            if self.status.synced:
                log.info("Sincronizado com %s", peer.node_id[:12])

        return self.status.synced

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
                        n = self._refresh_peers_from_p2p()
                        if n > 0:
                            log.debug("sync: %d peer(s) atualizados", n)

                        best = self.get_best_peer()
                        if best is not None:
                            self.status.active_peer = best.node_id
                            self.sync_with_peer(best)
                        else:
                            self.status.synced = True
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

    def get_status(self) -> Dict:
        local = self._safe_height()

        target = local
        active_peer = None
        peers_known = 0

        p2p = self.p2p
        if p2p is not None:
            disc = getattr(p2p, "discovery", None)
            if disc is not None and hasattr(disc, "listar_peers_com_altura"):
                try:
                    peers_info = disc.listar_peers_com_altura() or []
                    peers_known = len(peers_info)
                    for p in peers_info:
                        try:
                            h = int(p.get("height", 0) or 0)
                        except (TypeError, ValueError):
                            continue
                        if h > target:
                            target = h
                            active_peer = p.get("address") or p.get("addr")
                except Exception as e:
                    log.debug("get_status/listar_peers_com_altura: %s", e)

            if peers_known == 0 and hasattr(disc, "listar_peers"):
                try:
                    peers_known = len(disc.listar_peers() or [])
                except Exception:
                    pass

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
            "peers_known": peers_known or self.status.peers_known,
            "peers_synced": self.status.peers_synced,
            "last_error": self.status.last_error,
        }

    # ============================================================
    # [v6] _safe_height — normaliza negativos esquisitos
    # ============================================================
    def _safe_height(self) -> int:
        """
        Retorna altura atual da chain.

        v6: se o valor for negativo (< -1), normaliza para -1. Isso evita
        que valores estranhos (SQLite WAL em transição, etc.) apareçam
        como 'local=-1838' no log e confundam o cálculo de progresso.
        """
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
