"""mining_pool.py — Pool de mineração BRN
================================================================
Servidor do pool. Roda dentro do nó origem (main.py) e é
injetado no server.py para expor endpoints HTTP.

Conceitos:
  - Job      : template de bloco (prev_hash, merkle, timestamp, difficulty)
               com coinbase multi-output proporcional aos shares atuais
  - Share    : nonce que produz hash < share_target (mais fácil que block)
               Prova de trabalho parcial, usada para contabilizar hashrate
  - Fullblock: share que também bate block_target → bloco real!

Protocolo (endpoints implementados no server.py):
  POST /api/pool/register   {miner_id, address, pubkey}
  POST /api/pool/work       {miner_id} → devolve job atual
  POST /api/pool/submit     {miner_id, nonce, hash}
  POST /api/pool/heartbeat  {miner_id, hashrate, hashes_total}
  GET  /api/pool/stats      → estatísticas globais
  GET  /api/pool/miners     → lista mineradores ativos
================================================================
"""
from __future__ import annotations

import time
import uuid
import threading
import logging
from typing import Optional

from blockchain import (
    block_hash, compute_merkle_root, meets_difficulty, txid as calc_txid,
    MIN_RELAY_FEE,
)


log = logging.getLogger("pool")


# ============================================================
# CONFIG
# ============================================================
SHARE_DIFF_OFFSET   = 3      # share_diff = max(2, block_diff - OFFSET)
MINER_TIMEOUT_S     = 60     # minerador sem heartbeat por 60s é removido
JOB_REFRESH_S       = 20     # recria o job a cada 20s (para atualizar distribuição)
MAX_SHARES_PER_JOB  = 10000  # cap para evitar flood
MAX_MINERS          = 50


# ============================================================
# ESTRUTURAS
# ============================================================
class MinerState:
    __slots__ = ("miner_id", "address", "pubkey", "shares_period",
                 "shares_total", "hashes_total", "hashrate",
                 "last_seen", "connected_at", "mining")

    def __init__(self, miner_id, address, pubkey):
        self.miner_id = miner_id
        self.address = address
        self.pubkey = pubkey
        self.shares_period = 0     # shares do período atual (reset no bloco)
        self.shares_total = 0
        self.hashes_total = 0
        self.hashrate = 0.0
        self.last_seen = time.time()
        self.connected_at = time.time()
        self.mining = False


class Job:
    __slots__ = ("job_id", "height", "prev_hash", "merkle", "timestamp",
                 "difficulty", "share_difficulty", "transactions",
                 "distribution", "created_at")

    def __init__(self, height, prev_hash, merkle, timestamp, difficulty,
                 share_difficulty, transactions, distribution):
        self.job_id = uuid.uuid4().hex[:16]
        self.height = height
        self.prev_hash = prev_hash
        self.merkle = merkle
        self.timestamp = timestamp
        self.difficulty = difficulty
        self.share_difficulty = share_difficulty
        self.transactions = transactions
        self.distribution = distribution  # {address: {"pubkey", "weight"}}
        self.created_at = time.time()


# ============================================================
# HELPERS
# ============================================================
def _target(diff: int) -> int:
    return int("0" * int(diff) + "f" * (64 - int(diff)), 16)


def _meets(h_hex: str, diff: int) -> bool:
    try:
        return int(h_hex, 16) <= _target(diff)
    except Exception:
        return False


def _make_pool_coinbase(height: int, reward: int,
                        distribution: dict) -> dict:
    """
    Cria um coinbase com N outputs proporcionais aos pesos da
    distribuição. Fecha o "resto" no primeiro output.

    distribution: {"brn1...": {"pubkey": "02...", "weight": 5.0}, ...}
    """
    if not distribution:
        # Fallback: um único output placeholder (não devia acontecer)
        return {
            "txid": "", "inputs": [{"txid": "0" * 64, "vout": 0xFFFFFFFF,
                                    "pubkey": "", "signature": ""}],
            "outputs": [{"address": "brn1poolfallbackxxxxxxxxxxxxxxxxxxx",
                         "amount": reward, "pubkey": "00" * 33}],
            "timestamp": int(time.time()), "locktime": 0,
            "height": height, "nonce": 0,
        }

    total_weight = sum(d["weight"] for d in distribution.values()) or 1.0

    outputs = []
    for addr, info in distribution.items():
        amt = int(reward * (info["weight"] / total_weight))
        if amt <= 0:
            continue
        outputs.append({
            "address": addr,
            "amount": amt,
            "pubkey": info["pubkey"] or ("00" * 33),
        })

    if not outputs:
        # Fallback para o mais pesado
        first = max(distribution.items(), key=lambda kv: kv[1]["weight"])
        outputs = [{"address": first[0], "amount": reward,
                    "pubkey": first[1]["pubkey"]}]

    # Ajusta resto (perda por arredondamento)
    dif = reward - sum(o["amount"] for o in outputs)
    if dif > 0:
        outputs[0]["amount"] += dif

    cb = {
        "txid": "",
        "inputs": [{"txid": "0" * 64, "vout": 0xFFFFFFFF,
                    "pubkey": "", "signature": ""}],
        "outputs": outputs,
        "timestamp": int(time.time()),
        "locktime": 0,
        "height": int(height),
        "nonce": 0,
    }
    cb["txid"] = calc_txid(cb)
    return cb


# ============================================================
# POOL MANAGER
# ============================================================
class PoolManager:
    """
    Gestor do pool de mineração. Thread-safe.
    """

    def __init__(self, chain):
        self.chain = chain
        self.lock = threading.RLock()

        self.miners: dict = {}       # miner_id → MinerState
        self.current_job: Optional[Job] = None
        self.last_tip_hash: str = ""

        # métricas globais
        self.started_at = time.time()
        self.blocks_found = 0
        self.total_reward_paid = 0
        self.last_block_at = 0.0
        self.last_block_height = -1
        self.rejected_shares = 0
        self.accepted_shares = 0

        # thread de manutenção
        self._stop = threading.Event()
        threading.Thread(target=self._maintenance_loop, daemon=True,
                         name="Pool-Maint").start()

        log.info("PoolManager iniciado")

    # ========================================================
    # REGISTO / HEARTBEAT
    # ========================================================
    def register(self, address: str, pubkey: str,
                 miner_id: Optional[str] = None) -> dict:
        """Registra (ou reconecta) um minerador."""
        if not address or not address.startswith("brn1"):
            return {"ok": False, "msg": "address invalido"}
        if not pubkey:
            return {"ok": False, "msg": "pubkey obrigatoria"}

        with self.lock:
            if len(self.miners) >= MAX_MINERS and miner_id not in self.miners:
                return {"ok": False, "msg": "pool cheio"}

            if not miner_id:
                miner_id = uuid.uuid4().hex[:12]

            m = self.miners.get(miner_id)
            if m is None:
                m = MinerState(miner_id, address, pubkey)
                self.miners[miner_id] = m
                log.info("pool: %s registado (addr=%s)",
                         miner_id, address[:16])
            else:
                m.address = address
                m.pubkey = pubkey
                m.last_seen = time.time()

        return {"ok": True, "miner_id": miner_id}

    def heartbeat(self, miner_id: str, hashrate: float = 0.0,
                  hashes_total: int = 0) -> dict:
        with self.lock:
            m = self.miners.get(miner_id)
            if m is None:
                return {"ok": False, "msg": "miner desconhecido"}
            m.last_seen = time.time()
            m.hashrate = float(hashrate or 0.0)
            if hashes_total > m.hashes_total:
                m.hashes_total = int(hashes_total)
        return {"ok": True}

    def disconnect(self, miner_id: str) -> dict:
        with self.lock:
            self.miners.pop(miner_id, None)
        return {"ok": True}

    # ========================================================
    # JOB
    # ========================================================
    def get_work(self, miner_id: str) -> dict:
        """Devolve o job atual (recria se necessário)."""
        with self.lock:
            m = self.miners.get(miner_id)
            if m is None:
                return {"ok": False, "msg": "miner desconhecido"}
            m.last_seen = time.time()
            m.mining = True

            job = self.current_job
            if job is None or self._job_is_stale(job):
                job = self._build_job_locked()

        return {
            "ok": True,
            "job": {
                "job_id":           job.job_id,
                "height":           job.height,
                "prev_hash":        job.prev_hash,
                "merkle":           job.merkle,
                "timestamp":        job.timestamp,
                "difficulty":       job.difficulty,
                "share_difficulty": job.share_difficulty,
                "coinbase_txid":    job.transactions[0]["txid"],
                "n_txs":            len(job.transactions),
                "created_at":       job.created_at,
            },
        }

    def _job_is_stale(self, job: Job) -> bool:
        if time.time() - job.created_at > JOB_REFRESH_S:
            return True
        try:
            tip = self.chain.db.tip_hash()
            if tip != job.prev_hash:
                return True
        except Exception:
            return True
        return False

    def _build_job_locked(self) -> Job:
        """Cria um novo job. Deve ser chamado com o lock."""
        h = self.chain.db.height() + 1
        reward = self.chain.current_reward(h)
        diff = self.chain.current_difficulty()
        share_diff = max(2, diff - SHARE_DIFF_OFFSET)
        prev_hash = self.chain.db.tip_hash()

        # Distribuição: só mineradores com pubkey válida e conectados
        distribution = {}
        for mid, m in self.miners.items():
            if (time.time() - m.last_seen) > MINER_TIMEOUT_S:
                continue
            if not m.address or not m.pubkey:
                continue
            # peso = shares do período + 1 (para evitar zero)
            w = float(m.shares_period) + 1.0
            distribution[m.address] = {
                "pubkey": m.pubkey,
                "weight": w,
            }

        # Coinbase proporcional
        cb = _make_pool_coinbase(h, reward, distribution)

        # Mempool (limite)
        try:
            mempool_txs = self.chain.db.all_mempool(limit=200)
        except Exception:
            mempool_txs = []

        transactions = [cb] + mempool_txs
        merkle = compute_merkle_root([t["txid"] for t in transactions])

        job = Job(
            height=h,
            prev_hash=prev_hash,
            merkle=merkle,
            timestamp=int(time.time()),
            difficulty=diff,
            share_difficulty=share_diff,
            transactions=transactions,
            distribution=distribution,
        )
        self.current_job = job
        log.info("pool: novo job %s (h=%d diff=%d share_diff=%d miners=%d)",
                 job.job_id, h, diff, share_diff, len(distribution))
        return job

    # ========================================================
    # SHARE
    # ========================================================
    def submit_share(self, miner_id: str, nonce: int,
                     hash_hex: str = "") -> dict:
        """
        Valida um share. Se também bater a block_difficulty,
        minera o bloco de verdade.
        """
        with self.lock:
            m = self.miners.get(miner_id)
            if m is None:
                return {"ok": False, "msg": "miner desconhecido"}
            m.last_seen = time.time()

            job = self.current_job
            if job is None:
                return {"ok": False, "msg": "sem job ativo"}

            # Recalcula o hash
            try:
                h = block_hash(job.prev_hash, job.merkle,
                               job.timestamp, int(nonce),
                               job.difficulty)
            except Exception as e:
                return {"ok": False, "msg": f"nonce invalido: {e}"}

            if hash_hex and hash_hex != h:
                self.rejected_shares += 1
                return {"ok": False, "msg": "hash nao corresponde ao nonce"}

            # Verifica share_difficulty
            if not _meets(h, job.share_difficulty):
                self.rejected_shares += 1
                return {"ok": False,
                        "msg": "share nao bate share_difficulty"}

            # Conta share
            m.shares_period += 1
            m.shares_total += 1
            self.accepted_shares += 1

            block_found = False
            new_height = None

            # Verifica se também bate block_difficulty
            if _meets(h, job.difficulty):
                log.warning("pool: FULL BLOCK encontrado por %s (nonce=%s, h=%d)",
                            miner_id, nonce, job.height)
                try:
                    ok = self._assemble_and_accept_block(job, int(nonce), h)
                    if ok:
                        block_found = True
                        new_height = job.height
                        self.blocks_found += 1
                        self.last_block_at = time.time()
                        self.last_block_height = job.height
                        # Reset contadores do período
                        for mm in self.miners.values():
                            mm.shares_period = 0
                        self.current_job = None
                    else:
                        log.warning("pool: accept_block falhou")
                except Exception as e:
                    log.exception("pool: erro aceitando bloco: %s", e)

        return {
            "ok": True,
            "share_id": uuid.uuid4().hex[:12],
            "block_found": block_found,
            "height": new_height,
            "job_id": job.job_id,
            "share_difficulty": job.share_difficulty,
        }

    def _assemble_and_accept_block(self, job: Job, nonce: int,
                                   hash_hex: str) -> bool:
        """Monta o bloco final e chama chain.accept_block."""
        block = {
            "height":       job.height,
            "hash":         hash_hex,
            "prev_hash":    job.prev_hash,
            "timestamp":    job.timestamp,
            "nonce":        int(nonce),
            "merkle":       job.merkle,
            "difficulty":   job.difficulty,
            "transactions": job.transactions,
        }
        # attach auth_tag se necessário
        try:
            self.chain._attach_auth_tag(block)
        except Exception:
            pass

        ok, msg = self.chain.accept_block(block)
        if not ok:
            log.warning("pool: accept_block: %s", msg)
            return False

        # Soma total de recompensa paga (aproximação)
        try:
            cb = block["transactions"][0]
            reward = sum(o["amount"] for o in cb.get("outputs", []))
            self.total_reward_paid += int(reward)
        except Exception:
            pass

        # Broadcast P2P — o main.py injeta um callback
        cb = getattr(self, "on_block_mined", None)
        if callable(cb):
            try:
                cb(block)
            except Exception as e:
                log.debug("on_block_mined: %s", e)

        return True

    # ========================================================
    # ESTATÍSTICAS
    # ========================================================
    def stats(self) -> dict:
        with self.lock:
            now = time.time()
            active = [m for m in self.miners.values()
                      if now - m.last_seen < MINER_TIMEOUT_S]
            total_hashrate = sum(m.hashrate for m in active)
            total_shares = sum(m.shares_period for m in active)
            current_job = self.current_job

            return {
                "ok": True,
                "uptime_s":            int(now - self.started_at),
                "miners_active":       len(active),
                "miners_total":        len(self.miners),
                "hashrate_total":      round(total_hashrate, 2),
                "shares_period":       int(total_shares),
                "shares_accepted":     self.accepted_shares,
                "shares_rejected":     self.rejected_shares,
                "blocks_found":        self.blocks_found,
                "total_reward_paid":   self.total_reward_paid,
                "last_block_height":   self.last_block_height,
                "last_block_ago_s":    (int(now - self.last_block_at)
                                        if self.last_block_at else None),
                "job_active":          current_job is not None,
                "job_id":              (current_job.job_id
                                        if current_job else None),
                "job_height":          (current_job.height
                                        if current_job else None),
                "job_difficulty":      (current_job.difficulty
                                        if current_job else None),
                "job_share_difficulty": (current_job.share_difficulty
                                         if current_job else None),
                "job_created_ago_s":   (int(now - current_job.created_at)
                                        if current_job else None),
            }

    def miners_list(self) -> list:
        with self.lock:
            now = time.time()
            out = []
            for m in self.miners.values():
                out.append({
                    "miner_id":      m.miner_id,
                    "address":       m.address,
                    "shares_period": m.shares_period,
                    "shares_total":  m.shares_total,
                    "hashrate":      round(m.hashrate, 2),
                    "connected_s":   int(now - m.connected_at),
                    "last_seen_s":   int(now - m.last_seen),
                    "mining":        m.mining,
                    "active":        (now - m.last_seen) < MINER_TIMEOUT_S,
                })
            out.sort(key=lambda x: x["hashrate"], reverse=True)
            return out

    def miner_status(self, miner_id: str) -> dict:
        with self.lock:
            m = self.miners.get(miner_id)
            if m is None:
                return {"ok": False, "msg": "miner desconhecido"}

            # Estima recompensa pendente
            total_shares = sum(x.shares_period for x in self.miners.values())
            reward = self.chain.current_reward(self.chain.db.height() + 1)
            est_reward = 0
            if total_shares > 0:
                est_reward = int(reward * (m.shares_period / total_shares))

            return {
                "ok": True,
                "miner_id":         m.miner_id,
                "address":          m.address,
                "hashrate":         round(m.hashrate, 2),
                "shares_period":    m.shares_period,
                "shares_total":     m.shares_total,
                "hashes_total":     m.hashes_total,
                "connected_s":      int(time.time() - m.connected_at),
                "last_seen_s":      int(time.time() - m.last_seen),
                "mining":           m.mining,
                "est_reward_sats":  est_reward,
                "pool_total_shares": int(total_shares),
            }

    # ========================================================
    # MANUTENÇÃO
    # ========================================================
    def _maintenance_loop(self):
        while not self._stop.is_set():
            try:
                time.sleep(5)
                with self.lock:
                    now = time.time()
                    # Remove miners inativos
                    gone = [mid for mid, m in self.miners.items()
                            if now - m.last_seen > MINER_TIMEOUT_S * 3]
                    for mid in gone:
                        self.miners.pop(mid, None)
                        log.info("pool: miner %s removido (timeout)", mid)

                    # Recria job se estiver stale
                    if (self.current_job is None
                            or self._job_is_stale(self.current_job)):
                        if self.miners:
                            self._build_job_locked()
            except Exception as e:
                log.warning("pool maint: %s", e)

    def stop(self):
        self._stop.set()


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import tempfile
    import os

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("Testando PoolManager...")

    os.environ.setdefault("BRN_NETWORK_SECRET",
                          "segredo-de-teste-pool-1234567")

    with tempfile.TemporaryDirectory() as td:
        from blockchain import Blockchain
        bc = Blockchain(os.path.join(td, "test.db"), auto_genesis=True)
        pool = PoolManager(bc)

        # Registo
        r = pool.register("brn1testaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                          "02" + "aa" * 32)
        print(f"  register: {r}")
        assert r["ok"]

        mid = r["miner_id"]

        # Work
        w = pool.get_work(mid)
        print(f"  work: h={w['job']['height']} diff={w['job']['difficulty']} "
              f"share_diff={w['job']['share_difficulty']}")
        assert w["ok"]

        # Heartbeat
        pool.heartbeat(mid, hashrate=12345.0, hashes_total=100000)
        st = pool.stats()
        print(f"  stats: miners={st['miners_active']} "
              f"hashrate={st['hashrate_total']}")

        # Status do miner
        ms = pool.miner_status(mid)
        print(f"  miner status: shares={ms['shares_period']} "
              f"hashrate={ms['hashrate']}")

        # Simula um share válido
        # (encontra nonce manualmente com share_difficulty baixa)
        from blockchain import block_hash
        job = pool.current_job
        found = None
        for n in range(500000):
            h = block_hash(job.prev_hash, job.merkle,
                           job.timestamp, n, job.difficulty)
            if _meets(h, job.share_difficulty):
                found = (n, h)
                break
        assert found, "nao encontrou share em 500k tentativas"
        nonce, h = found
        print(f"  share candidato: nonce={nonce} hash={h[:24]}...")

        r = pool.submit_share(mid, nonce, h)
        print(f"  submit: {r}")
        assert r["ok"]

        st = pool.stats()
        print(f"  stats pos-share: accepted={st['shares_accepted']} "
              f"rejected={st['shares_rejected']}")

        pool.stop()
        bc.db.close()

    print("PoolManager OK.")
