"""reorg.py — Reorganização de cadeia do BRN
================================================================
Extrai a lógica de reorg do blockchain.py para um módulo independente.

Responsabilidades:
  1. Detectar fork (comparar cadeias locais vs remotas)
  2. Encontrar o fork point (último bloco em comum)
  3. Verificar se a cadeia alternativa tem mais cumulative_work
  4. Validar que nenhum checkpoint seria violado
  5. Aplicar reorg com rollback automático em caso de falha

API pública:
    reorg_to(chain, new_blocks, max_depth=None) -> ReorgResult
        Executa reorg completo.
        chain        = instância de Blockchain
        new_blocks   = lista de blocos da cadeia alternativa
        max_depth    = profundidade máxima (default: MAX_REORG_DEPTH)

    find_fork_point(chain, remote_blocks) -> int
        Retorna altura do último bloco em comum, ou -1.

    compare_work(chain, remote_blocks) -> tuple
        Retorna (work_local, work_remote, should_reorg: bool).

    can_reorg(chain, remote_blocks) -> tuple
        Verifica se reorg é possível sem aplicar.
        Retorna (True, "") ou (False, "motivo").

    apply_reorg_if_needed(chain, remote_blocks) -> ReorgResult
        Wrapper: só reorga se valer a pena.

Plugar no blockchain.py (adicionar no FIM do arquivo):

    from reorg import reorg_to as _reorg_to, find_fork_point as _find_fork
    Blockchain.reorg_to = lambda self, b: _reorg_to(self, b).to_tuple()
    Blockchain.find_fork_point = lambda self, b: _find_fork(self, b)

Requer:
    blockchain.py v9.1.7+ (não importa diretamente — usa duck typing)
================================================================
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional


log = logging.getLogger("reorg")


# ============================================================
# CONSTANTES
# ============================================================
DEFAULT_MAX_REORG_DEPTH = 100
DEFAULT_MIN_WORK_ADVANTAGE = 1     # work_remote deve ser >= work_local + 1


# ============================================================
# RESULTADO ESTRUTURADO
# ============================================================
@dataclass
class ReorgResult:
    """Resultado de uma tentativa de reorg."""
    ok: bool = False
    reason: str = ""
    fork_point: int = -1
    blocks_removed: int = 0
    blocks_added: int = 0
    work_local: int = 0
    work_remote: int = 0
    duration_s: float = 0.0
    checkpoint_violated: int = -1
    error: Optional[str] = None

    def to_tuple(self) -> tuple:
        """Compat: (ok: bool, msg: str)."""
        if self.ok:
            return True, (f"reorg ok — fork #{self.fork_point}, "
                          f"-{self.blocks_removed} +{self.blocks_added} "
                          f"({self.duration_s:.2f}s)")
        return False, self.reason or self.error or "reorg falhou"

    def to_dict(self) -> dict:
        return {
            "ok":                   self.ok,
            "reason":               self.reason,
            "fork_point":           self.fork_point,
            "blocks_removed":       self.blocks_removed,
            "blocks_added":         self.blocks_added,
            "work_local":           self.work_local,
            "work_remote":          self.work_remote,
            "duration_s":           round(self.duration_s, 3),
            "checkpoint_violated":  self.checkpoint_violated,
            "error":                self.error,
        }


# ============================================================
# HELPERS INTERNOS — work
# ============================================================
def _work_fn():
    """Importa work_from_difficulty do blockchain (ou fallback)."""
    try:
        from blockchain import work_from_difficulty
        return work_from_difficulty
    except ImportError:
        def _fallback(d):
            return 16 ** int(d)
        return _fallback


def _cumulative_work_of_chain(blocks: list) -> int:
    """Soma do work de todos os blocos de uma lista."""
    if not blocks:
        return 0
    work_fn = _work_fn()
    total = 0
    for b in blocks:
        try:
            total += work_fn(int(b.get("difficulty", 0)))
        except Exception:
            continue
    return total


def _local_work_above(chain, fork_height: int) -> int:
    """Work acumulado dos blocos locais acima do fork."""
    if fork_height < 0:
        return 0
    work_fn = _work_fn()
    total = 0
    top = chain.db.height()
    for h in range(fork_height + 1, top + 1):
        b = chain.db.get_block(h)
        if b:
            try:
                total += work_fn(int(b.get("difficulty", 0)))
            except Exception:
                continue
    return total


# ============================================================
# HELPERS INTERNOS — validação de checkpoint
# ============================================================
def _check_checkpoints(chain, alt_blocks: list, fork_height: int) -> tuple:
    """
    Verifica se a cadeia alternativa viola algum checkpoint conhecido.

    Retorna (ok: bool, violated_height: int, msg: str).
    """
    try:
        from checkpoints import load_checkpoints
        cps = load_checkpoints()
    except ImportError:
        return True, -1, ""

    if not cps:
        return True, -1, ""

    # Indexa alt_blocks por altura para lookup O(1)
    alt_by_h = {}
    for b in alt_blocks:
        try:
            alt_by_h[int(b["height"])] = b
        except (KeyError, ValueError, TypeError):
            continue

    for h_str, cp in cps.items():
        try:
            h_int = int(h_str)
        except (ValueError, TypeError):
            continue

        # checkpoints abaixo do fork não são afetados
        if h_int <= fork_height:
            continue

        # Verifica se o checkpoint está na cadeia alternativa
        alt_block = alt_by_h.get(h_int)
        if alt_block is None:
            # O bloco do checkpoint não está na alternativa — viola
            return False, h_int, f"checkpoint #{h_int} nao esta na nova cadeia"

        if alt_block.get("hash") != cp.get("hash"):
            return False, h_int, (
                f"checkpoint #{h_int} hash diverge "
                f"(esperado {str(cp.get('hash'))[:16]}..., "
                f"alt {str(alt_block.get('hash'))[:16]}...)"
            )

        # Também valida a assinatura do checkpoint
        try:
            from checkpoints import verify_checkpoint
            if not verify_checkpoint(cp):
                return False, h_int, f"checkpoint #{h_int} assinatura invalida"
        except ImportError:
            pass
        except Exception as e:
            return False, h_int, f"checkpoint #{h_int} verify falhou: {e}"

    return True, -1, ""


# ============================================================
# find_fork_point
# ============================================================
def find_fork_point(chain, remote_blocks: list,
                    max_depth: int = 1000) -> int:
    """
    Encontra a altura do último bloco em comum entre a cadeia local
    e a lista `remote_blocks`.

    Retorna:
        >= 0 → altura do bloco comum
        -1   → nenhum bloco em comum (nem genesis bate)
    """
    if not remote_blocks:
        return -1

    local_h = chain.db.height()
    if local_h < 0:
        return -1

    # Indexa remote por altura
    remote_by_h = {}
    for b in remote_blocks:
        try:
            remote_by_h[int(b["height"])] = b
        except (KeyError, ValueError, TypeError):
            continue

    # Procura de trás para frente
    stop = max(-1, local_h - max_depth)
    for h in range(local_h, stop, -1):
        local_blk = chain.db.get_block(h)
        if not local_blk:
            continue
        remote_blk = remote_by_h.get(h)
        if remote_blk and remote_blk.get("hash") == local_blk.get("hash"):
            return h
    return -1


# ============================================================
# compare_work
# ============================================================
def compare_work(chain, remote_blocks: list,
                 fork_height: int) -> tuple:
    """
    Compara cumulative_work local vs remoto acima do fork.

    Retorna (work_local: int, work_remote: int, should_reorg: bool).
    """
    work_local = _local_work_above(chain, fork_height)
    alt_above = [b for b in remote_blocks
                 if int(b.get("height", -1)) > fork_height]
    work_remote = _cumulative_work_of_chain(alt_above)

    should_reorg = work_remote >= (work_local + DEFAULT_MIN_WORK_ADVANTAGE)
    return work_local, work_remote, should_reorg


# ============================================================
# can_reorg (dry-run)
# ============================================================
def can_reorg(chain, remote_blocks: list,
              max_depth: int = DEFAULT_MAX_REORG_DEPTH) -> tuple:
    """
    Verifica se um reorg é possível SEM aplicar nada.

    Retorna (True, ReorgResult vazio) se pode;
    (False, ReorgResult com reason) se não pode.
    """
    res = ReorgResult()

    if not remote_blocks:
        res.reason = "lista de blocos vazia"
        return False, res

    # 1. Fork point
    fork = find_fork_point(chain, remote_blocks)
    res.fork_point = fork
    if fork < 0:
        res.reason = "nenhum ponto em comum com a cadeia remota"
        return False, res

    # 2. Blocos a adicionar
    to_add = [b for b in remote_blocks
              if int(b.get("height", -1)) > fork]
    if not to_add:
        res.reason = "nada novo acima do fork"
        return False, res

    # 3. Profundidade
    depth = chain.db.height() - fork
    if depth > max_depth:
        res.reason = f"reorg muito profundo ({depth} > {max_depth})"
        return False, res

    # 4. Checkpoints
    ok_cp, violated, msg_cp = _check_checkpoints(chain, remote_blocks, fork)
    if not ok_cp:
        res.reason = msg_cp
        res.checkpoint_violated = violated
        return False, res

    # 5. Work
    work_local, work_remote, should = compare_work(
        chain, remote_blocks, fork)
    res.work_local = work_local
    res.work_remote = work_remote
    if not should:
        res.reason = (f"cadeia local tem mais/igual trabalho "
                      f"({work_local} vs {work_remote})")
        return False, res

    # 6. Pré-validação de cada bloco da alternativa
    prev = chain.db.get_block(fork) if fork >= 0 else None
    for b in to_add:
        try:
            ok, msg = chain.validate_block(b, prev)
        except Exception as e:
            res.reason = f"erro validando #{b.get('height')}: {e}"
            return False, res
        if not ok:
            res.reason = f"bloco #{b.get('height')} invalido: {msg}"
            return False, res
        prev = b

    res.reason = "ok"
    return True, res


# ============================================================
# REORG — função principal
# ============================================================
def reorg_to(chain, new_blocks: list,
             max_depth: int = DEFAULT_MAX_REORG_DEPTH) -> ReorgResult:
    """
    Executa o reorg da cadeia local para a cadeia alternativa.

    Passos:
      1. can_reorg() — dry-run completo
      2. Backup dos blocos locais acima do fork
      3. delete_blocks_above(fork)
      4. accept_block() para cada bloco da alternativa
      5. Em caso de falha: rollback (reaplica os backups)
      6. Atualiza métricas e retorna ReorgResult

    Retorna ReorgResult com todos os detalhes.
    """
    t0 = time.time()
    res = ReorgResult()

    log.info("[REORG] iniciando — %d bloco(s) recebidos", len(new_blocks))

    # ---------- 1. Dry-run ----------
    ok, dry = can_reorg(chain, new_blocks, max_depth)
    if not ok:
        log.info("[REORG] recusado: %s", dry.reason)
        dry.duration_s = time.time() - t0
        return dry

    fork = dry.fork_point
    res.fork_point = fork
    res.work_local = dry.work_local
    res.work_remote = dry.work_remote

    to_add = [b for b in new_blocks if int(b.get("height", -1)) > fork]

    # ---------- 2. Backup ----------
    backups = []
    top = chain.db.height()
    for h in range(fork + 1, top + 1):
        b = chain.db.get_block(h)
        if b:
            backups.append(b)
    res.blocks_removed = len(backups)

    log.info("[REORG] fork em #%d — removendo %d, adicionando %d",
             fork, len(backups), len(to_add))

    # ---------- 3. Deletar ----------
    try:
        chain.db.delete_blocks_above(fork)
    except Exception as e:
        res.reason = f"falha ao deletar blocos: {e}"
        res.error = str(e)
        res.duration_s = time.time() - t0
        log.error("[REORG] %s", res.reason)
        return res

    # ---------- 4. Aplicar alternativa ----------
    applied = 0
    for b in to_add:
        try:
            ok_blk, msg_blk = chain.accept_block(b)
        except Exception as e:
            ok_blk, msg_blk = False, f"excecao: {e}"

        if not ok_blk:
            # ---------- 5. Rollback ----------
            log.error("[REORG] falha em #%d: %s — restaurando",
                      b.get("height"), msg_blk)
            _rollback(chain, fork, backups)
            res.reason = f"falha ao aplicar #{b.get('height')}: {msg_blk}"
            res.error = msg_blk
            res.blocks_added = applied
            res.duration_s = time.time() - t0
            return res

        applied += 1

    # ---------- 6. Sucesso ----------
    res.ok = True
    res.blocks_added = applied
    res.reason = "ok"
    res.duration_s = time.time() - t0

    log.warning("[REORG] concluido! fork=#%d -%d +%d "
                "(work: %d → %d) em %.2fs",
                fork, res.blocks_removed, res.blocks_added,
                res.work_local, res.work_remote, res.duration_s)

    return res


# ============================================================
# Rollback interno
# ============================================================
def _rollback(chain, fork: int, backups: list) -> None:
    """Restaura a cadeia ao estado anterior ao reorg."""
    try:
        chain.db.delete_blocks_above(fork)
    except Exception as e:
        log.error("[REORG] rollback: falha ao limpar: %s", e)

    for b in backups:
        try:
            chain.accept_block(b)
        except Exception as e:
            log.error("[REORG] rollback: falha reaplicando #%d: %s",
                      b.get("height"), e)

    log.info("[REORG] rollback concluido — altura restaurada: %d",
             chain.db.height())


# ============================================================
# apply_reorg_if_needed — wrapper de alto nível
# ============================================================
def apply_reorg_if_needed(chain, remote_blocks: list,
                          max_depth: int = DEFAULT_MAX_REORG_DEPTH) -> ReorgResult:
    """
    Só executa reorg se valer a pena (mais work). Caso contrário,
    retorna ReorgResult com ok=False e reason explicando.

    Use este em produção. Chame reorg_to() só quando quiser forçar.
    """
    ok, dry = can_reorg(chain, remote_blocks, max_depth)
    if not ok:
        dry.duration_s = 0.0
        return dry
    return reorg_to(chain, remote_blocks, max_depth)


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import tempfile

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("Testando reorg.py (com blockchain mock)...")

    # ------------------------------------------------------------
    # Mock mínimo de ChainDB e Blockchain
    # ------------------------------------------------------------
    class MockDB:
        def __init__(self):
            self._blocks = {}

        def height(self):
            if not self._blocks:
                return -1
            return max(self._blocks.keys())

        def get_block(self, h):
            return self._blocks.get(h)

        def add_block(self, b):
            self._blocks[b["height"]] = dict(b)

        def delete_blocks_above(self, h):
            for k in [k for k in self._blocks.keys() if k > h]:
                del self._blocks[k]

    class MockChain:
        def __init__(self):
            self.db = MockDB()

        def validate_block(self, b, prev):
            if prev is None and b["height"] != 0:
                return False, "sem prev"
            if prev and b.get("prev_hash") != prev.get("hash"):
                return False, "prev_hash"
            return True, "ok"

        def accept_block(self, b):
            # rejeita se altura não for sequencial
            expected = self.db.height() + 1
            if b["height"] != expected:
                return False, f"altura {b['height']} != {expected}"
            self.db.add_block(b)
            return True, "ok"

    # ------------------------------------------------------------
    # Helper para criar blocos
    # ------------------------------------------------------------
    def mk_block(h, prev_hash, diff=4, seed=""):
        import hashlib
        raw = f"{h}:{prev_hash}:{diff}:{seed}".encode()
        h_hash = hashlib.sha256(raw).hexdigest()
        return {
            "height": h,
            "hash": h_hash,
            "prev_hash": prev_hash,
            "merkle": "0" * 64,
            "timestamp": 1700000000 + h,
            "nonce": 0,
            "difficulty": diff,
            "transactions": [],
        }

    # ------------------------------------------------------------
    # 1. Setup — cadeia local com 10 blocos
    # ------------------------------------------------------------
    chain = MockChain()

    # Cadeia local (canônica até altura 10)
    prev = "0" * 64
    for h in range(0, 11):
        b = mk_block(h, prev, diff=4, seed=f"local-{h}")
        chain.db.add_block(b)
        prev = b["hash"]

    print(f"  altura local: {chain.db.height()}")
    assert chain.db.height() == 10

    # ------------------------------------------------------------
    # 2. Fork em #5, alternativa com 15 blocos (mais longa)
    # ------------------------------------------------------------
    fork_h = 5
    fork_hash = chain.db.get_block(fork_h)["hash"]

    alt = []
    prev_alt = fork_hash
    for h in range(fork_h + 1, 16):
        b = mk_block(h, prev_alt, diff=4, seed=f"alt-{h}")
        alt.append(b)
        prev_alt = b["hash"]

    # ------------------------------------------------------------
    # 3. find_fork_point
    # ------------------------------------------------------------
    fp = find_fork_point(chain, alt)
    print(f"  find_fork_point: #{fp}")
    assert fp == fork_h, f"esperado {fork_h}, obteve {fp}"

    # ------------------------------------------------------------
    # 4. compare_work
    # ------------------------------------------------------------
    wl, wr, should = compare_work(chain, alt, fp)
    print(f"  work local={wl}  remote={wr}  should_reorg={should}")
    assert should is True

    # ------------------------------------------------------------
    # 5. can_reorg (dry-run)
    # ------------------------------------------------------------
    ok, dry = can_reorg(chain, alt)
    print(f"  can_reorg: {ok} — {dry.reason}")
    assert ok
    assert dry.fork_point == fork_h

    # ------------------------------------------------------------
    # 6. reorg_to (aplica)
    # ------------------------------------------------------------
    res = reorg_to(chain, alt)
    print(f"  reorg_to: ok={res.ok} fork=#{res.fork_point} "
          f"-{res.blocks_removed} +{res.blocks_added} "
          f"({res.duration_s:.3f}s)")
    assert res.ok, f"reorg falhou: {res.reason}"
    assert res.fork_point == 5
    assert res.blocks_removed == 5   # #6..#10
    assert res.blocks_added == 10    # #6..#15

    # Confere que a nova tip é a da alternativa
    new_tip = chain.db.get_block(chain.db.height())
    print(f"  nova tip: #{new_tip['height']} hash={new_tip['hash'][:16]}...")
    assert new_tip["height"] == 15
    assert new_tip["hash"] == alt[-1]["hash"]

    # ------------------------------------------------------------
    # 7. Reorg com menos work (deve ser recusado)
    # ------------------------------------------------------------
    alt_fraca = [
        mk_block(11, chain.db.get_block(10)["hash"], diff=2, seed="fraca-11"),
        mk_block(12, chain.db.get_block(11)["hash"] if False else "x",
                 diff=2, seed="fraca-12"),
    ]
    # reencadeia
    alt_fraca[1]["prev_hash"] = alt_fraca[0]["hash"]

    ok2, dry2 = can_reorg(chain, alt_fraca)
    print(f"  reorg fraca: ok={ok2} — {dry2.reason}")
    assert not ok2

    # ------------------------------------------------------------
    # 8. Reorg vazio
    # ------------------------------------------------------------
    ok3, dry3 = can_reorg(chain, [])
    assert not ok3
    print(f"  reorg vazio: ok={ok3} — {dry3.reason}")

    # ------------------------------------------------------------
    # 9. Reorg com profundidade excessiva
    # ------------------------------------------------------------
    ok4, dry4 = can_reorg(chain, alt, max_depth=1)
    assert not ok4
    print(f"  reorg fundo: ok={ok4} — {dry4.reason}")

    # ------------------------------------------------------------
    # 10. apply_reorg_if_needed — wrapper
    # ------------------------------------------------------------
    # (aplica com sucesso)
    r2 = apply_reorg_if_needed(chain, alt)
    print(f"  apply_reorg_if_needed (mesma cadeia): ok={r2.ok}")

    print("reorg.py OK.")