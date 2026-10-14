"""sync_manager.py — Sincronização de blocos entre nós BRN (v7)
================================================================
Gerencia a sincronização incremental com peers:
  - Seleciona o peer com maior cumulative_work
  - Baixa blocos em lotes (SYNC_BATCH_SIZE)
  - Pré-valida cada bloco antes de aplicar
  - Aplica via chain.accept_block()
  - Atualiza progresso, velocidade e ETA em tempo real

API usada por main.py v12.5:
    sync = ChainSyncManager(chain, chain.db, p2p)
    sync.register_peer(node_id, ip, port, height, work)
    sync.set_sync_mode("full")
    sync.start_sync_loop()
    ...
    sync.get_status()   # usado por server.py /api/sync/status
    sync.stop()

Campos do get_status() (compatível com server.py v9.5):
    synced, syncing, mode, progress,
    local_height, target_height, blocos_faltando,
    blocos_por_segundo, velocidade_media,
    eta_segundos, active_peer,
    peers_known, peers_synced,
    checkpoint_violations, checkpoints_conhecidos,
    last_error

Env:
    BRN_SYNC_BATCH          (default 50)
    BRN_SYNC_INTERVAL       (default 5s)
    BRN_SYNC_BATCH_DELAY    (default 0.06s)
    BRN_SYNC_MAX_RETRY      (default 3)
================================================================
"""
from __future__ import annotations

import logging
import os
import threading
import time

from p2p_unified import P2PClient


log = logging.getLogger("sync")


# ============================================================
# CONFIG
# ============================================================
SYNC_BATCH_SIZE   = int(os.environ.get("BRN_SYNC_BATCH", "50"))
SYNC_INTERVAL     = float(os.environ.get("BRN_SYNC_INTERVAL", "5"))
SYNC_BATCH_DELAY  = float(os.environ.get("BRN_SYNC_BATCH_DELAY", "0.06"))
SYNC_MAX_RETRY    = int(os.environ.get("BRN_SYNC_MAX_RETRY", "3"))


# ============================================================
# ChainSyncManager
# ============================================================
class ChainSyncManager:
    """
    Sincronizador de cadeia entre pares.

    Uso típico:
        sm = ChainSyncManager(chain, chain.db, p2p_manager)
        sm.register_peer("uuid123", "10.0.0.1", 6001, height=2046, work=1)
        sm.set_sync_mode("full")
        sm.start_sync_loop()
    """

    def __init__(self, blockchain, db, p2p_manager):
        self.chain = blockchain
        self.db = db
        self.p2p = p2p_manager
        self.batch_size = SYNC_BATCH_SIZE

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

        # Estado dos peers
        self._peers: dict = {}   # node_id -> dict(ip, port, height, work, ...)

        # Estado do sync
        self._sync_state = {
            "mode":                  "full",
            "local_height":          0,
            "target_height":         0,
            "synced":                False,
            "syncing":               False,
            "progress":              0.0,
            "active_peer":           None,
            "peers_known":           0,
            "peers_synced":          0,
            "blocos_faltando":       0,
            "blocos_por_segundo":    0.0,
            "velocidade_media":      0.0,
            "eta_segundos":          None,
            "checkpoint_violations": 0,
            "checkpoints_conhecidos": 0,
            "last_error":            "",
        }

        # Estatísticas internas
        self._sync_start_ts = 0.0
        self._sync_start_height = 0
        self._window_speed = []

    # ============================================================
    # REGISTRO DE PEERS
    # ============================================================
    def register_peer(self, node_id: str, ip: str, port: int,
                      height: int = 0, cumulative_work: int = 0) -> None:
        """Registra/atualiza um peer conhecido."""
        if not node_id:
            node_id = f"{ip}:{port}"
        try:
            port = int(port)
            height = int(height or 0)
            cumulative_work = int(cumulative_work or 0)
        except (TypeError, ValueError):
            return

        with self._lock:
            self._peers[node_id] = {
                "node_id": node_id,
                "ip":      ip,
                "port":    port,
                "height":  height,
                "work":    cumulative_work,
                "last_seen": time.time(),
            }
            self._sync_state["peers_known"] = len(self._peers)

    def _best_peer(self):
        """Retorna o peer com maior cumulative_work (ou height se work=0)."""
        with self._lock:
            peers = list(self._peers.values())
        if not peers:
            return None

        # filtra peers com altura maior que a local
        local_h = self.chain.db.height()
        candidates = [p for p in peers if p["height"] > local_h]
        if not candidates:
            return None

        return max(candidates, key=lambda p: (p["work"], p["height"]))

    # ============================================================
    # CONFIGURAÇÃO
    # ============================================================
    def set_sync_mode(self, mode: str = "full") -> None:
        if mode not in ("full", "spv"):
            raise ValueError("modo deve ser 'full' ou 'spv'")
        with self._lock:
            self._sync_state["mode"] = mode

    # ============================================================
    # LOOP DE SYNC
    # ============================================================
    def start_sync_loop(self) -> None:
        """Inicia a thread de sincronização contínua."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="ChainSync")
        self._thread.start()
        log.info("ChainSyncManager: loop iniciado (batch=%d)", self.batch_size)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        log.info("ChainSyncManager encerrado")

    # ============================================================
    # LOOP PRINCIPAL
    # ============================================================
    def _loop(self):
        # Delay inicial para o boot terminar
        if self._stop.wait(2):
            return

        while not self._stop.is_set():
            try:
                peer = self._best_peer()
                if peer is not None:
                    self._sync_with_peer(peer)
                else:
                    # nada para sincronizar
                    local = self.chain.db.height()
                    with self._lock:
                        self._sync_state["local_height"] = local
                        self._sync_state["target_height"] = local
                        self._sync_state["synced"] = True
                        self._sync_state["syncing"] = False
                        self._sync_state["progress"] = 100.0
                        self._sync_state["blocos_faltando"] = 0
                        self._sync_state["active_peer"] = None
                        self._sync_state["eta_segundos"] = None
            except Exception as e:
                log.warning(f"sync loop: {e}")
                with self._lock:
                    self._sync_state["last_error"] = str(e)[:200]

            if self._stop.wait(SYNC_INTERVAL):
                break

    # ============================================================
    # SINCRONIZAÇÃO COM UM PEER
    # ============================================================
    def _sync_with_peer(self, peer: dict) -> bool:
        """Baixa blocos do peer até alcançar a altura dele."""
        local_h = self.chain.db.height()
        target_h = int(peer["height"])
        node_id = peer["node_id"]

        with self._lock:
            self._sync_state["active_peer"] = node_id
            self._sync_state["syncing"] = True
            self._sync_state["local_height"] = local_h
            self._sync_state["target_height"] = target_h
            self._sync_state["blocos_faltando"] = max(0, target_h - local_h)
            self._sync_state["synced"] = (local_h >= target_h)
            self._sync_state["progress"] = (
                100.0 if target_h <= 0 else
                min(100.0, (local_h / target_h) * 100)
            )

        if local_h >= target_h:
            with self._lock:
                self._sync_state["syncing"] = False
                self._sync_state["active_peer"] = None
            return True

        # Marca início do sync (para cálculo de velocidade média)
        self._sync_start_ts = time.time()
        self._sync_start_height = local_h
        self._window_speed = []

        log.info(f"[Sync] iniciando {node_id[:12]} | {local_h} → {target_h}")

        # Backoff local para tentar batches
        backoff = 0.5
        consecutive_fails = 0

        while not self._stop.is_set() and local_h < target_h:
            # Recalcula target (peer pode ter minerado mais)
            with self._lock:
                p = self._peers.get(node_id)
            if p:
                target_h = max(target_h, int(p["height"]))

            cursor = local_h + 1
            end = min(cursor + self.batch_size, target_h + 1)
            count = end - cursor

            t0 = time.time()
            resp, latency = self._fetch_range(peer, cursor, end)
            if not resp or "blocks" not in resp:
                consecutive_fails += 1
                if consecutive_fails >= SYNC_MAX_RETRY:
                    log.warning(f"[Sync] {node_id[:12]}: desistindo após "
                                f"{SYNC_MAX_RETRY} falhas")
                    with self._lock:
                        self._sync_state["last_error"] = \
                            "falha ao buscar blocos"
                        self._sync_state["syncing"] = False
                    return False
                time.sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue

            lote = resp.get("blocks") or []
            if not lote:
                # peer não tem mais blocos (reduziu altura?)
                break

            # Reset backoff ao ter sucesso
            backoff = 0.5
            consecutive_fails = 0

            # Aplica bloco a bloco
            applied = 0
            for blk in lote:
                if self._stop.is_set():
                    break

                expected_h = self.chain.db.height() + 1
                expected_prev = self.chain.db.tip_hash()

                if int(blk.get("height", -1)) != expected_h:
                    log.warning(f"[Sync] bloco fora de ordem: "
                                f"{blk.get('height')} != {expected_h}")
                    with self._lock:
                        self._sync_state["last_error"] = \
                            f"altura fora de ordem: {blk.get('height')}"
                    break

                if blk.get("prev_hash") != expected_prev:
                    log.warning(f"[Sync] prev_hash diverge em #{expected_h}")
                    with self._lock:
                        self._sync_state["last_error"] = \
                            f"prev_hash diverge em #{expected_h}"
                    break

                try:
                    ok, msg = self.chain.accept_block(blk)
                except Exception as e:
                    log.warning(f"[Sync] accept_block #{expected_h}: {e}")
                    with self._lock:
                        self._sync_state["last_error"] = str(e)[:200]
                    break

                if not ok:
                    log.warning(f"[Sync] rejeitado #{expected_h}: {msg}")
                    with self._lock:
                        self._sync_state["last_error"] = str(msg)[:200]
                    break

                applied += 1

            local_h = self.chain.db.height()

            # Atualiza estado
            with self._lock:
                self._sync_state["local_height"] = local_h
                self._sync_state["target_height"] = target_h
                faltam = max(0, target_h - local_h)
                self._sync_state["blocos_faltando"] = faltam
                self._sync_state["progress"] = (
                    100.0 if target_h <= 0 else
                    min(100.0, (local_h / target_h) * 100)
                )

            # Velocidade instantânea
            dt = max(0.001, time.time() - t0)
            speed = applied / dt
            self._window_speed.append((time.time(), speed))
            # mantém últimos 10s
            cutoff = time.time() - 10
            self._window_speed = [s for s in self._window_speed
                                  if s[0] >= cutoff]

            with self._lock:
                self._sync_state["blocos_por_segundo"] = round(speed, 2)

                # Velocidade média desde início do sync
                total_dt = max(0.001, time.time() - self._sync_start_ts)
                total_done = local_h - self._sync_start_height
                media = total_done / total_dt if total_dt > 0 else 0.0
                self._sync_state["velocidade_media"] = round(media, 2)

                # ETA
                if media > 0 and faltam > 0:
                    eta = faltam / media
                    self._sync_state["eta_segundos"] = round(eta, 1)
                else:
                    self._sync_state["eta_segundos"] = None

            if applied == 0:
                # Nada avançou — provável fork ou peer mudou
                log.warning(f"[Sync] sem progresso, aguardando...")
                time.sleep(1)

            # Delay entre lotes para não martelar o peer
            if SYNC_BATCH_DELAY > 0:
                time.sleep(SYNC_BATCH_DELAY)

        # Sincronizado?
        with self._lock:
            self._sync_state["syncing"] = False
            self._sync_state["active_peer"] = None
            self._sync_state["synced"] = (
                self.chain.db.height() >= target_h
            )
            self._sync_state["progress"] = (
                100.0 if target_h <= 0 else
                min(100.0, (self.chain.db.height() / target_h) * 100)
            )

        log.info(f"[Sync] concluído | altura={self.chain.db.height()}")
        return True

    # ============================================================
    # FETCH
    # ============================================================
    def _fetch_range(self, peer: dict, start: int, end: int):
        """Busca um lote de blocos do peer."""
        try:
            resp, latency = P2PClient.get_blocks_range(
                peer["ip"], int(peer["port"]), start, end
            )
            return resp, latency
        except Exception as e:
            log.debug(f"fetch_range: {e}")
            return None, 0.0

    # ============================================================
    # SYNC PONTUAL (chamado pelo P2P quando descobre peer melhor)
    # ============================================================
    def sync_with_peer(self, ip: str, port: int) -> bool:
        """Sincronização pontual com um peer específico."""
        resp, _ = P2PClient.get_chain_height(ip, int(port))
        if not resp:
            return False
        peer = {
            "node_id": resp.get("_uuid", f"{ip}:{port}"),
            "ip":      ip,
            "port":    int(port),
            "height":  int(resp.get("height", 0)),
            "work":    int(resp.get("work", 0)),
            "last_seen": time.time(),
        }
        self.register_peer(**{k: peer[k] for k in
                              ("node_id", "ip", "port", "height", "work")})
        return self._sync_with_peer(peer)

    # ============================================================
    # GET_STATUS (usado por /api/sync/status)
    # ============================================================
    def get_status(self) -> dict:
        with self._lock:
            st = dict(self._sync_state)

            # Atualiza sempre valores vivos
            try:
                st["local_height"] = self.chain.db.height()
            except Exception:
                pass

            st["peers_known"] = len(self._peers)
            st["peers_synced"] = sum(
                1 for p in self._peers.values()
                if int(p.get("height", 0)) <= st["local_height"]
            )

        # Contagem de checkpoints (fora do lock)
        try:
            from checkpoints import load_checkpoints
            st["checkpoints_conhecidos"] = len(load_checkpoints())
        except Exception:
            st["checkpoints_conhecidos"] = 0

        return st

    # ============================================================
    # UTILITÁRIOS
    # ============================================================
    def peer_count(self) -> int:
        with self._lock:
            return len(self._peers)

    def list_peers(self) -> list:
        with self._lock:
            return list(self._peers.values())

    def is_syncing(self) -> bool:
        with self._lock:
            return self._sync_state.get("syncing", False)


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("Testando ChainSyncManager...")

    # ---- mock de blockchain ----
    class FakeDB:
        def __init__(self):
            self._h = 0
        def height(self):
            return self._h
        def tip_hash(self):
            return "0" * 64

    class FakeChain:
        def __init__(self):
            self.db = FakeDB()
        def accept_block(self, blk):
            self.db._h += 1
            return True, "ok"

    class FakeP2P:
        def __init__(self):
            self.node_uuid = "abc"
            self._peers = {}
            self._peers_lock = threading.RLock()

    chain = FakeChain()
    p2p = FakeP2P()

    sm = ChainSyncManager(chain, chain.db, p2p)
    print(f"  [info] batch_size: {sm.batch_size}")
    print(f"  [info] mode inicial: {sm.get_status()['mode']}")

    # ---- 1. register_peer ----
    sm.register_peer("peer1", "10.0.0.1", 6001, height=100, cumulative_work=500)
    sm.register_peer("peer2", "10.0.0.2", 6001, height=50, cumulative_work=200)
    st = sm.get_status()
    print(f"  [ok] peers registrados: {st['peers_known']}")
    assert st["peers_known"] == 2

    # ---- 2. best_peer escolhe o com mais work ----
    best = sm._best_peer()
    assert best is not None
    assert best["node_id"] == "peer1", f"esperado peer1, obteve {best['node_id']}"
    print(f"  [ok] _best_peer: {best['node_id']} (work={best['work']})")

    # ---- 3. set_sync_mode ----
    sm.set_sync_mode("spv")
    assert sm.get_status()["mode"] == "spv"
    sm.set_sync_mode("full")
    assert sm.get_status()["mode"] == "full"
    print("  [ok] set_sync_mode")

    # ---- 4. Sem progresso (peer inalcancavel) ----
    sm.register_peer("peer_dead", "192.0.2.1", 6001, height=999, work=9999)
    st = sm.get_status()
    print(f"  [info] best agora: {sm._best_peer()['node_id']}")

    # ---- 5. Start/stop do loop ----
    sm.start_sync_loop()
    time.sleep(2)
    sm.stop()
    print("  [ok] start_sync_loop + stop")

    # ---- 6. get_status contém os campos esperados ----
    st = sm.get_status()
    expected_keys = [
        "synced", "syncing", "mode", "progress",
        "local_height", "target_height", "blocos_faltando",
        "blocos_por_segundo", "velocidade_media",
        "eta_segundos", "active_peer",
        "peers_known", "peers_synced",
        "checkpoint_violations", "checkpoints_conhecidos",
        "last_error",
    ]
    faltando = [k for k in expected_keys if k not in st]
    assert not faltando, f"faltando: {faltando}"
    print(f"  [ok] get_status tem todos os {len(expected_keys)} campos")

    # ---- 7. Imprime status final ----
    print(f"  [info] status final:")
    print(json.dumps(st, indent=4, ensure_ascii=False))

    print("ChainSyncManager OK.")