"""
sync_manager.py — Sincronização + Reorg automático (v8.4)
================================================================
v8.4:
  [NEW] Fallback HTTP: se o P2P (porta 6001) falhar, o sync_manager
        baixa os blocos via API HTTP (porta 5000).
        Essencial para funcionar em redes sem Multicast (Tailscale).
================================================================
"""
from __future__ import annotations

import json
import os
import socket
import time
import threading
import logging
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger("sync")

# ============================================================
# CONFIGURAÇÃO
# ============================================================
NETWORK_MAGIC      = b"BRN5"
MAX_MSG_SIZE       = 8 * 1024 * 1024
DEFAULT_TIMEOUT    = 10.0
DEFAULT_BATCH_SIZE = int(os.environ.get("BRN_SYNC_BATCH", "50"))
DEFAULT_INTERVAL_S = float(os.environ.get("BRN_SYNC_INTERVAL", "5"))
BATCH_DELAY_S      = float(os.environ.get("BRN_SYNC_BATCH_DELAY", "0.06"))

REORG_LOOKBACK       = int(os.environ.get("BRN_REORG_LOOKBACK", "500"))
REORG_WORK_THRESHOLD = float(os.environ.get("BRN_REORG_WORK_THRESHOLD", "1.0"))
REORG_FETCH_BATCH    = int(os.environ.get("BRN_REORG_FETCH_BATCH", "200"))
REORG_MAX_FAILS      = int(os.environ.get("BRN_REORG_MAX_FAILS", "3"))
REORG_COOLDOWN_S     = float(os.environ.get("BRN_REORG_COOLDOWN_S", "60"))

# ============================================================
# TIPOS
# ============================================================
@dataclass
class PeerStatus:
    node_id: str
    ip: str
    port: int
    height: int = 0
    cumulative_work: int = 0
    last_seen: float = field(default_factory=time.time)
    latency_ms: float = 0.0
    is_http: bool = False # [NOVO] Indica se o peer é HTTP fallback

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
    forking: bool = False
    reorgs_total: int = 0
    last_reorg_height: int = -1

# ============================================================
# ChainSyncManager
# ============================================================
class ChainSyncManager:

    def __init__(self, blockchain, db, p2p_manager,
                 send_fn: Optional[Callable[[str, int, dict, float],
                                             Optional[dict]]] = None):
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

        if hasattr(self.chain, "_fork_signal") and self.chain._fork_signal is not None:
            self._fork_signal = self.chain._fork_signal
        else:
            self._fork_signal = threading.Event()
            self.chain._fork_signal = self._fork_signal

        self._reorg_fails: Dict[str, dict] = {}
        self._reorg_fails_lock = threading.Lock()

    def set_sync_mode(self, mode: str = "full") -> None:
        if mode not in ("full", "spv"): return
        self.spv_headers_only = (mode == "spv")
        self.status.mode = mode

    def register_peer(self, node_id: str, ip: str, port: int,
                      height: int = 0, cumulative_work: int = 0) -> None:
        if not node_id: return
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

    # [NOVO] Método para registrar um peer HTTP (fallback)
    def register_http_peer(self, node_id: str, ip: str) -> None:
        with self.peer_lock:
            self.peers[node_id] = PeerStatus(
                node_id=node_id, ip=ip, port=5000, is_http=True
            )

    def unregister_peer(self, node_id: str) -> None:
        with self.peer_lock:
            self.peers.pop(node_id, None)
            self.status.peers_known = len(self.peers)

    def _safe_height(self) -> int:
        try: return max(-1, int(self.chain.db.height()))
        except Exception: return -1

    def _local_work(self) -> int:
        try: return int(self.chain.cumulative_work())
        except Exception: return 0

    def _do_send(self, ip: str, port: int, msg: dict, timeout: float = DEFAULT_TIMEOUT) -> Optional[dict]:
        if self._send_fn is not None:
            try: return self._send_fn(ip, port, msg, timeout)
            except Exception: return None
        try:
            from p2p_unified import P2PClient
            resp, _ = P2PClient.send_message(ip, port, msg, timeout=timeout)
            return resp
        except Exception: return None

    # [NOVO] Fetch de blocos via HTTP
    def _http_fetch_blocks(self, peer: PeerStatus, start: int, count: int) -> List[Dict]:
        url = f"http://{peer.ip}:5000/api/blocks?start={start}&limit={count}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "brn-sync-fallback"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                return data.get("blocks", [])
        except Exception as e:
            log.debug(f"HTTP fetch {url} falhou: {e}")
            return []

    def _get_peer_height_http(self, peer: PeerStatus) -> int:
        url = f"http://{peer.ip}:5000/api/status"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "brn-sync-fallback"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                return int(data.get("height", 0))
        except Exception:
            return 0

    def _refresh_peers_from_p2p(self) -> int:
        p2p = self.p2p
        count = 0
        
        # 1. Tenta P2P normal
        if p2p is not None:
            disc = getattr(p2p, "discovery", None)
            if disc is not None and hasattr(disc, "listar_peers_com_altura"):
                try:
                    for p in disc.listar_peers_com_altura() or []:
                        addr = p.get("address", "")
                        if not addr or ":" not in addr: continue
                        ip, _, port_s = addr.rpartition(":")
                        try: port = int(port_s)
                        except ValueError: continue
                        height = int(p.get("height", 0) or 0)
                        work = int(p.get("work", 0) or 0)
                        self.register_peer(addr, ip, port, height, work)
                        count += 1
                except Exception: pass

        # 2. Fallback: Tenta HTTP direto nos bootstrap peers
        bootstrap = os.environ.get("BRN_BOOTSTRAP_PEERS", "").split(",")
        for b in bootstrap:
            b = b.strip()
            if not b or ":" not in b: continue
            ip = b.split(":")[0]
            nid = f"http-{ip}"
            with self.peer_lock:
                if nid in self.peers and not self.peers[nid].is_http:
                    continue # Já achou via P2P
                
                p = self.peers.get(nid)
                if p is None:
                    p = PeerStatus(node_id=nid, ip=ip, port=5000, is_http=True)
                    self.peers[nid] = p
                    
            p.height = self._get_peer_height_http(p)
            p.last_seen = time.time()
            self.status.peers_known = len(self.peers)
            count += 1

        return count

    def _best_peer_by_work(self) -> Optional[PeerStatus]:
        local_h = self._safe_height()
        with self.peer_lock:
            candidates = [p for p in self.peers.values() if p.height > local_h]
        if not candidates:
            return None
        return max(candidates, key=lambda p: (p.height if p.is_http else p.cumulative_work, p.height))

    def sync_with_peer(self, peer: PeerStatus) -> bool:
        local_h = self._safe_height()
        self.status.local_height = local_h
        self.status.target_height = peer.height
        self.status.last_error = ""

        if local_h >= peer.height:
            self.status.synced = True
            return True

        while not self._stop_sync.is_set():
            local_h = self._safe_height()
            if local_h >= peer.height:
                break

            current = local_h + 1
            end = min(current + self.batch_size - 1, peer.height)
            count = end - current + 1

            # [NOVO] Escolhe P2P ou HTTP
            if peer.is_http:
                blocks = self._http_fetch_blocks(peer, current, count)
            else:
                payload = {"type": "get_blocks_range", "start": current, "end": current + count}
                resp = self._do_send(peer.ip, peer.port, payload, DEFAULT_TIMEOUT)
                blocks = resp.get("blocks", []) if resp and "blocks" in resp else []
                
                # Se P2P falhar, tenta HTTP
                if not blocks:
                    log.warning(f"P2P falhou para {peer.ip}:{peer.port}. Tentando HTTP fallback...")
                    peer.is_http = True
                    peer.port = 5000
                    blocks = self._http_fetch_blocks(peer, current, count)

            if not blocks:
                self.status.last_error = f"lote {current}..{end} vazio"
                if self._stop_sync.wait(timeout=2): break
                continue

            applied = 0
            for block_data in blocks:
                try:
                    h = block_data.get("height")
                    if h is not None and self.db.get_block(h) is not None:
                        applied += 1
                        continue
                    ok, msg = self.chain.accept_block(block_data)
                    if ok: applied += 1
                    else:
                        log.warning(f"accept_block #{h}: {msg}")
                        break
                except Exception as e:
                    log.warning(f"aplicar #{block_data.get('height')}: {e}")
                    break

            self.status.local_height = self._safe_height()
            target = max(1, self.status.target_height)
            self.status.progress = min(100.0, self.status.local_height / target * 100.0)

            if applied == 0:
                break
            if self._stop_sync.wait(timeout=BATCH_DELAY_S):
                break

        if not self._stop_sync.is_set():
            self.status.synced = (self._safe_height() >= peer.height)
        return self.status.synced

    def start_sync_loop(self) -> None:
        if self.sync_lock.locked(): return
        self._stop_sync.clear()
        self.status.syncing = True

        def _loop():
            try:
                while not self._stop_sync.is_set():
                    try:
                        self._refresh_peers_from_p2p()
                        best = self._best_peer_by_work()
                        if best is not None:
                            self.status.active_peer = best.node_id
                            self.sync_with_peer(best)
                        else:
                            self.status.synced = True
                            self.status.local_height = self._safe_height()
                            self.status.target_height = self.status.local_height
                    except Exception as e:
                        log.warning(f"sync loop erro: {e}")
                        self.status.last_error = str(e)
                    if self._stop_sync.wait(timeout=DEFAULT_INTERVAL_S): break
            finally:
                self.status.syncing = False
                self.status.active_peer = None

        self.sync_thread = threading.Thread(target=_loop, daemon=True, name="ChainSync")
        self.sync_thread.start()
        log.info("ChainSyncManager v8.4 iniciado (com HTTP fallback)")

    def stop(self) -> None:
        self._stop_sync.set()
        if self.sync_thread and self.sync_thread.is_alive():
            self.sync_thread.join(timeout=5)

    def get_status(self) -> Dict:
        local = self._safe_height()
        target = local
        active = None
        with self.peer_lock:
            peers = list(self.peers.values())

        for p in peers:
            if p.height > target:
                target = p.height
                active = p.addr

        progress = 100.0 if target <= 0 else min(100.0, local / target * 100.0)
        synced = (local >= target and target >= 0)

        return {
            "mode":                self.status.mode,
            "local_height":        local,
            "target_height":       target,
            "synced":              synced,
            "syncing":             self.status.syncing,
            "progress":            round(progress, 2),
            "active_peer":         active,
            "peers_known":         len(peers),
            "peers_synced":        sum(1 for p in peers if p.height <= local),
            "last_error":          self.status.last_error,
        }
