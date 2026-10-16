"""reorg.py — Reorganização de cadeia (Chain Reorganization) BRN
================================================================
Plugado automaticamente em Blockchain por blockchain.py v9.1.8:

    from reorg import reorg_to as _reorg_to, find_fork_point as _find_fork
    Blockchain.reorg_to       = lambda self, nb, md=MAX_REORG_DEPTH, ag=False: \
                                    _reorg_to(self, nb, md, ag).to_tuple()
    Blockchain.find_fork_point = _find_fork

API pública:
    reorg_to(chain, new_blocks, max_depth=100, allow_genesis_reorg=False)
        → ReorgResult  (com .to_tuple() e .to_dict())

    find_fork_point(chain, remote_blocks, max_depth=1000)
        → int (altura do último bloco em comum, ou -1)

    reorg_to_dict(...)  → dict

Semântica Bitcoin:
    1. Achar fork point (último bloco em comum)
    2. Comparar cumulative work local vs remoto
    3. Só reorganiza se work_remoto > work_local
    4. Respeita MAX_REORG_DEPTH
    5. Respeita checkpoints assinados (Ed25519) — não pode
       reescrever bloco coberto por checkpoint
    6. Pré-valida hash/PoW/encadeamento dos blocos novos ANTES
       de tocar no DB
    7. Rollback por bloco via db.rollback_block() (preserva UTXOs)
    8. Em caso de falha no meio: restaura os blocos antigos
    9. Ativa chain._fork_signal durante o reorg (aborta miner)

Sem dependências extras.
================================================================
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("reorg")

# Apenas 1 reorg por vez no processo
_REORG_LOCK = threading.RLock()

DEFAULT_MAX_DEPTH = 100


# ============================================================
# RESULTADO
# ============================================================
class ReorgResult:
    __slots__ = ("ok", "msg", "fork_height", "depth",
                 "blocks_removed", "blocks_added",
                 "work_local", "work_remote", "duration_s")

    def __init__(self):
        self.ok = False
        self.msg = ""
        self.fork_height = -1
        self.depth = 0
        self.blocks_removed = 0
        self.blocks_added = 0
        self.work_local = 0
        self.work_remote = 0
        self.duration_s = 0.0

    def to_tuple(self):
        return (self.ok, self.msg)

    def to_dict(self) -> dict:
        return {
            "ok":             self.ok,
            "msg":            self.msg,
            "fork_height":    self.fork_height,
            "depth":          self.depth,
            "blocks_removed": self.blocks_removed,
            "blocks_added":   self.blocks_added,
            "work_local":     self.work_local,
            "work_remote":    self.work_remote,
            "duration_s":     round(self.duration_s, 3),
        }

    def __bool__(self):
        return self.ok

    def __repr__(self):
        s = "OK" if self.ok else "FAIL"
        return (f"<ReorgResult {s} fork=#{self.fork_height} "
                f"-{self.blocks_removed} +{self.blocks_added} "
                f"work {self.work_local}→{self.work_remote}>")


# ============================================================
# HELPERS
# ============================================================
def _work_from_difficulty(difficulty) -> int:
    try:
        return 16 ** int(difficulty)
    except Exception:
        return 0


def _cumulative_work_of_blocks(blocks: list) -> int:
    total = 0
    for b in blocks:
        total += _work_from_difficulty(b.get("difficulty", 0))
    return total


def _local_work_above(chain, fork_height: int) -> int:
    total = 0
    try:
        for h in range(fork_height + 1, chain.db.height() + 1):
            b = chain.db.get_block(h)
            if b:
                total += _work_from_difficulty(b.get("difficulty", 0))
    except Exception as e:
        log.debug(f"_local_work_above falhou: {e}")
    return total


def _ensure_fork_signal(chain) -> Optional[threading.Event]:
    """Garante que chain._fork_signal existe (Event)."""
    sig = getattr(chain, "_fork_signal", None)
    if sig is None:
        try:
            sig = threading.Event()
            chain._fork_signal = sig
        except Exception:
            return None
    return sig


def _set_fork_signal(chain, active: bool) -> None:
    sig = _ensure_fork_signal(chain)
    if sig is None:
        return
    try:
        if active:
            sig.set()
        else:
            sig.clear()
    except Exception:
        pass


# ============================================================
# FIND FORK POINT
# ============================================================
def find_fork_point(chain, remote_blocks: list,
                    max_depth: int = 1000) -> int:
    """
    Encontra a altura do último bloco em comum entre a cadeia
    local e os blocos remotos fornecidos.

    `remote_blocks` deve conter blocos cobrindo a região do fork
    (tipicamente começa algumas dezenas de blocos antes da ponta
    local). Se não cobrir a região do fork, pode retornar -1.

    Retorna a altura do último bloco em comum, ou -1 se nenhum.
    """
    if not remote_blocks:
        return -1

    try:
        local_h = chain.db.height()
    except Exception:
        return -1

    # Indexa blocos remotos por altura (ignora entradas malformadas)
    remote_by_h: dict[int, dict] = {}
    for b in remote_blocks:
        try:
            remote_by_h[int(b["height"])] = b
        except (KeyError, ValueError, TypeError):
            continue

    if not remote_by_h:
        return -1

    # Começa da altura mais alta possível e desce
    start_h = min(local_h, max(remote_by_h.keys()))
    end_h = max(-1, start_h - max(1, int(max_depth)))

    for h in range(start_h, end_h - 1, -1):
        remote_b = remote_by_h.get(h)
        if remote_b is None:
            continue
        local_b = chain.db.get_block(h)
        if not local_b:
            continue
        if local_b.get("hash") == remote_b.get("hash"):
            return h

    return -1


# ============================================================
# CHECKPOINTS
# ============================================================
def _check_checkpoints_not_violated(fork_height: int,
                                     new_blocks: list) -> tuple:
    """
    Verifica se o reorg violaria algum checkpoint assinado.

    Regra: para qualquer checkpoint em altura H > fork_height,
    o novo ramo PRECISA conter o mesmo hash nessa altura.
    (Você não pode reescrever um bloco já coberto por checkpoint.)

    Retorna (ok, msg).
    """
    try:
        from checkpoints import load_checkpoints
    except ImportError:
        return True, ""

    try:
        cps = load_checkpoints() or {}
    except Exception:
        return True, ""

    if not cps:
        return True, ""

    remote_by_h: dict[int, str] = {}
    for b in new_blocks:
        try:
            remote_by_h[int(b["height"])] = b.get("hash", "")
        except (KeyError, ValueError, TypeError):
            continue

    for h_str, cp in cps.items():
        try:
            h = int(h_str)
        except (ValueError, TypeError):
            continue
        if h <= fork_height:
            continue

        expected = cp.get("hash", "")
        remote_hash = remote_by_h.get(h)

        if remote_hash is None:
            return False, (f"reorg removeria bloco #{h} coberto por "
                           f"checkpoint (novo ramo não inclui essa altura)")
        if remote_hash != expected:
            return False, (f"reorg tentaria trocar bloco #{h} "
                           f"coberto por checkpoint assinado")

    return True, ""


# ============================================================
# PRÉ-VALIDAÇÃO
# ============================================================
def _pre_validate_new_blocks(chain, fork_height: int,
                              new_blocks: list) -> tuple:
    """
    Valida hash/PoW/encadeamento dos blocos novos SEM tocar no DB.
    Retorna (ok, msg).
    """
    if not new_blocks:
        return False, "lista de blocos vazia"

    try:
        from blockchain import block_hash, meets_difficulty
    except ImportError:
        return False, "blockchain.py sem block_hash/meets_difficulty"

    try:
        ordered = sorted(new_blocks, key=lambda b: int(b["height"]))
    except (KeyError, ValueError, TypeError) as e:
        return False, f"blocos com altura inválida: {e}"

    expected_h = fork_height + 1

    if fork_height >= 0:
        prev_blk = chain.db.get_block(fork_height)
        if not prev_blk:
            return False, f"bloco local #{fork_height} ausente"
        prev_hash = prev_blk["hash"]
    else:
        prev_hash = "0" * 64

    for blk in ordered:
        try:
            h = int(blk["height"])
        except (KeyError, ValueError, TypeError):
            return False, "bloco sem 'height'"

        if h != expected_h:
            return False, (f"sequência quebrada: esperado #{expected_h}, "
                           f"recebido #{h}")

        if blk.get("prev_hash") != prev_hash:
            return False, (f"bloco #{h}: prev_hash não encadeia")

        try:
            calc = block_hash(blk["prev_hash"], blk["merkle"],
                              blk["timestamp"], blk["nonce"],
                              blk["difficulty"])
        except Exception as e:
            return False, f"bloco #{h}: erro calculando hash: {e}"

        if calc != blk.get("hash"):
            return False, f"bloco #{h}: hash do header não corresponde"

        if not meets_difficulty(calc, int(blk["difficulty"])):
            return False, f"bloco #{h}: PoW insuficiente"

        prev_hash = calc
        expected_h += 1

    return True, ""


# ============================================================
# ROLLBACK / RESTORE
# ============================================================
def _rollback_to(chain, fork_height: int, current_tip: int) -> int:
    """
    Remove blocos > fork_height via db.rollback_block() (preserva UTXOs).
    Retorna o número de blocos removidos.
    """
    removed = 0
    has_rollback_block = hasattr(chain.db, "rollback_block")
    has_delete_above = hasattr(chain.db, "delete_blocks_above")

    if has_rollback_block:
        for h in range(current_tip, fork_height, -1):
            try:
                chain.db.rollback_block(h)
                removed += 1
            except Exception as e:
                log.warning(f"[REORG] rollback_block(#{h}) falhou: {e}")
                # Fallback em massa
                if has_delete_above:
                    try:
                        chain.db.delete_blocks_above(fork_height)
                        return current_tip - fork_height
                    except Exception:
                        pass
                raise
    elif has_delete_above:
        removed = int(chain.db.delete_blocks_above(fork_height) or 0)
    else:
        raise RuntimeError(
            "ChainDB sem rollback_block() nem delete_blocks_above()")

    return removed


def _restore_blocks(chain, backups: list) -> int:
    """Re-aplica blocos antigos em ordem (best effort)."""
    restored = 0
    for b in backups:
        try:
            ok, _ = chain.accept_block(b)
            if ok:
                restored += 1
        except Exception as e:
            log.error(f"[REORG] restaurar #{b.get('height')}: {e}")
    return restored


# ============================================================
# REORG
# ============================================================
def reorg_to(chain, new_blocks: list,
             max_depth: int = DEFAULT_MAX_DEPTH,
             allow_genesis_reorg: bool = False) -> ReorgResult:
    """
    Executa uma reorganização.

    Retorna ReorgResult. Use .to_tuple() → (bool, str).
    """
    t0 = time.time()
    res = ReorgResult()

    with _REORG_LOCK:
        # ---------- 0. Sanidade ----------
        if not new_blocks:
            res.msg = "lista de blocos vazia"
            res.duration_s = time.time() - t0
            return res

        local_h = chain.db.height()
        if local_h < 0 and not allow_genesis_reorg:
            res.msg = "cadeia local vazia (sem genesis)"
            res.duration_s = time.time() - t0
            return res

        # ---------- 1. Fork point ----------
        fork_h = find_fork_point(chain, new_blocks,
                                  max_depth=max_depth * 2)
        if fork_h < 0:
            if not allow_genesis_reorg:
                res.msg = "nenhum bloco em comum (fork não encontrado)"
                res.duration_s = time.time() - t0
                return res
            fork_h = -1

        res.fork_height = fork_h

        # Filtra blocos acima do fork
        blocks_to_add: list = []
        for b in new_blocks:
            try:
                if int(b["height"]) > fork_h:
                    blocks_to_add.append(b)
            except (KeyError, ValueError, TypeError):
                continue

        if not blocks_to_add:
            res.msg = "nada novo (cadeia já contém esses blocos)"
            res.duration_s = time.time() - t0
            return res

        blocks_to_add.sort(key=lambda b: int(b["height"]))

        # ---------- 2. Work ----------
        work_remote = _cumulative_work_of_blocks(blocks_to_add)
        work_local = _local_work_above(chain, fork_h)
        res.work_remote = work_remote
        res.work_local = work_local

        if work_remote <= work_local:
            res.msg = (f"ramo remoto tem menos/mesmo work "
                       f"({work_remote} <= {work_local})")
            res.duration_s = time.time() - t0
            return res

        # ---------- 3. Depth ----------
        depth = local_h - fork_h
        res.depth = depth
        if depth > max_depth:
            res.msg = (f"reorg muito profundo "
                       f"({depth} blocos > {max_depth})")
            res.duration_s = time.time() - t0
            return res

        # ---------- 4. Checkpoints ----------
        ok_cp, msg_cp = _check_checkpoints_not_violated(
            fork_h, blocks_to_add)
        if not ok_cp:
            res.msg = msg_cp
            res.duration_s = time.time() - t0
            return res

        # ---------- 5. Pré-validação ----------
        ok_pre, msg_pre = _pre_validate_new_blocks(
            chain, fork_h, blocks_to_add)
        if not ok_pre:
            res.msg = f"pré-validação: {msg_pre}"
            res.duration_s = time.time() - t0
            return res

        # ---------- 6. Log + fork signal ----------
        log.warning(
            f"[REORG] fork=#{fork_h} depth={depth} "
            f"novos={len(blocks_to_add)} "
            f"work_local={work_local} work_remote={work_remote}"
        )

        _set_fork_signal(chain, True)
        try:
            # ---------- 7. Backup ----------
            backups: list = []
            for h in range(fork_h + 1, local_h + 1):
                b = chain.db.get_block(h)
                if b:
                    backups.append(b)

            # ---------- 8. Rollback ----------
            try:
                res.blocks_removed = _rollback_to(chain, fork_h, local_h)
            except Exception as e:
                res.msg = f"falha no rollback: {e}"
                res.duration_s = time.time() - t0
                return res

            # ---------- 9. Aplicar novos ----------
            added = 0
            for b in blocks_to_add:
                try:
                    ok_acc, msg_acc = chain.accept_block(b)
                except Exception as e:
                    ok_acc, msg_acc = False, f"exceção: {e}"

                if not ok_acc:
                    log.error(f"[REORG] aplicar #{b.get('height')} "
                              f"falhou: {msg_acc} — restaurando")

                    # Cleanup do parcial
                    try:
                        cur_tip = chain.db.height()
                        if cur_tip > fork_h:
                            _rollback_to(chain, fork_h, cur_tip)
                    except Exception as e2:
                        log.error(f"[REORG] cleanup parcial: {e2}")

                    # Restaura os antigos
                    rest = _restore_blocks(chain, backups)
                    log.warning(f"[REORG] restaurados {rest}/{len(backups)} "
                                f"blocos")

                    res.blocks_added = added
                    res.msg = (f"falha ao aplicar #{b.get('height')}: "
                               f"{msg_acc}")
                    res.duration_s = time.time() - t0
                    return res

                added += 1

            res.blocks_added = added
            res.ok = True
            res.msg = (f"reorg concluído: fork #{fork_h}, "
                       f"-{res.blocks_removed} +{res.blocks_added} "
                       f"(work {work_remote}>{work_local})")
            log.warning(f"[REORG] OK — altura final: {chain.db.height()}")

        finally:
            _set_fork_signal(chain, False)

        res.duration_s = time.time() - t0
        return res


# ============================================================
# API DE MÓDULO
# ============================================================
def reorg_to_dict(chain, new_blocks, max_depth=DEFAULT_MAX_DEPTH,
                  allow_genesis_reorg=False) -> dict:
    return reorg_to(chain, new_blocks, max_depth,
                    allow_genesis_reorg).to_dict()


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import os
    import tempfile

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("=" * 60)
    print(" reorg.py — Auto-teste")
    print("=" * 60)

    os.environ.setdefault("BRN_NETWORK_SECRET",
                          "segredo-de-teste-reorg-1234567")
    os.environ.setdefault("BRN_FORCE_DIFFICULTY", "2")

    with tempfile.TemporaryDirectory() as td:
        from blockchain import Blockchain
        from wallet import Wallet

        path_a = os.path.join(td, "chain_a.db")
        path_b = os.path.join(td, "chain_b.db")

        print("\n[1] Duas cadeias com genesis idêntico:")
        bc_a = Blockchain(path_a, auto_genesis=True)

        # Cria B com o MESMO genesis do A
        bc_b = Blockchain(path_b, auto_genesis=False)
        genesis_a = bc_a.db.get_block(0)
        bc_b.db.add_block(genesis_a)
        bc_b.db.apply_tx(genesis_a["transactions"][0], 0, coinbase=True)
        bc_b.db.set_meta("genesis_hash", genesis_a["hash"])

        print(f"    A altura={bc_a.db.height()} "
              f"tip={bc_a.db.tip_hash()[:16]}...")
        print(f"    B altura={bc_b.db.height()} "
              f"tip={bc_b.db.tip_hash()[:16]}...")
        assert bc_a.db.tip_hash() == bc_b.db.tip_hash()

        print("\n[2] A minera 3 blocos, B minera 1:")
        w_a = Wallet()
        w_b = Wallet()

        for _ in range(3):
            blk = bc_a.mine_block(w_a.address, w_a.pub_hex)
            assert blk is not None
        blk_b = bc_b.mine_block(w_b.address, w_b.pub_hex)
        assert blk_b is not None

        print(f"    A altura={bc_a.db.height()} "
              f"work={bc_a.cumulative_work()}")
        print(f"    B altura={bc_b.db.height()} "
              f"work={bc_b.cumulative_work()}")
        assert bc_a.db.height() == 3
        assert bc_b.db.height() == 1

        print("\n[3] Reorg B ← A:")
        a_blocks = [bc_a.db.get_block(h)
                    for h in range(0, bc_a.db.height() + 1)]
        a_blocks = [b for b in a_blocks if b]

        res = reorg_to(bc_b, a_blocks, max_depth=100)
        print(f"    {res}")
        assert res.ok, f"reorg falhou: {res.msg}"
        assert bc_b.db.height() == 3
        assert bc_b.db.tip_hash() == bc_a.db.tip_hash()
        print(f"    B agora altura={bc_b.db.height()} "
              f"tip={bc_b.db.tip_hash()[:16]}...")
        print(f"    {res.to_dict()}")

        print("\n[4] Reorg no-op:")
        res2 = reorg_to(bc_b, a_blocks, max_depth=100)
        print(f"    {res2}")
        assert not res2.ok

        print("\n[5] Reorg com work menor (rejeita):")
        res3 = reorg_to(bc_a, [genesis_a], max_depth=100)
        print(f"    {res3}")
        assert not res3.ok

        print("\n[6] Reorg muito profundo (max_depth=1):")
        res4 = reorg_to(bc_b, a_blocks, max_depth=1)
        print(f"    {res4}")
        assert not res4.ok

        print("\n[7] find_fork_point direto:")
        fp = find_fork_point(bc_b, a_blocks, max_depth=100)
        print(f"    fork point = #{fp}")
        assert fp == bc_b.db.height()   # já sincronizados

        print("\n[8] Fork signal:")
        sig = getattr(bc_b, "_fork_signal", None)
        print(f"    _fork_signal existe? {sig is not None}")
        print(f"    set? {sig.is_set() if sig else 'N/A'}")
        assert sig is not None and not sig.is_set()

        print("\n[9] Lista vazia:")
        res5 = reorg_to(bc_b, [], max_depth=100)
        print(f"    {res5}")
        assert not res5.ok

        bc_a.db.close()
        bc_b.db.close()

    print("\n" + "=" * 60)
    print(" reorg.py OK.")
    print("=" * 60)
