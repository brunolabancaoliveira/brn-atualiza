"""reorg.py — Reorganização de cadeia (v2.0)
================================================================
v2.0:
  [NEW] Finality guard: chama chain.validate_reorg() antes de
        aplicar. Bloqueia reorgs que violem FINALITY_DEPTH ou
        checkpoint assinado.
  [NEW] Comparação de chainwork real (2^256/target) via
        work_from_difficulty do blockchain.py.
  [NEW] Rollback atômico: se aplicar a nova cadeia falhar no meio,
        restaura os blocos originais automaticamente.
  [NEW] Suporte a allow_genesis_reorg (para bootstrap).
  [KEEP] API pública:
           reorg_to(chain, new_blocks, max_depth, allow_genesis_reorg)
             → ReorgResult
           find_fork_point(chain, remote_blocks, max_depth)
             → int (altura do ancestral comum, ou -1)

Como o blockchain.py pluga este módulo (final do arquivo):

    from reorg import reorg_to as _reorg_to, find_fork_point as _find_fork

    def _reorg_to_method(self, new_blocks, max_depth=MAX_REORG_DEPTH,
                         allow_genesis_reorg=False):
        res = _reorg_to(self, new_blocks, max_depth, allow_genesis_reorg)
        return res.to_tuple()

    Blockchain.reorg_to = _reorg_to_method
    Blockchain.find_fork_point = _find_fork_method
================================================================
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("reorg")


# ============================================================
# ReorgResult — retorno padronizado
# ============================================================
@dataclass
class ReorgResult:
    """
    Resultado de um reorg.

    Atributos públicos:
        ok               : True se o reorg foi aplicado
        msg              : mensagem (sucesso ou motivo da falha)
        fork_height      : altura do ancestral comum (-1 se genesis)
        old_height       : altura local antes do reorg
        new_height       : altura local após o reorg
        blocks_removed   : quantos blocos foram descartados
        blocks_added     : quantos blocos foram aplicados
    """
    ok: bool
    msg: str
    fork_height: int = -1
    old_height: int = -1
    new_height: int = -1
    blocks_removed: int = 0
    blocks_added: int = 0

    def to_tuple(self) -> tuple:
        """Compatível com `ok, msg = chain.reorg_to(...)`."""
        return (self.ok, self.msg)

    @classmethod
    def success(cls, msg: str, **kwargs) -> "ReorgResult":
        return cls(ok=True, msg=msg, **kwargs)

    @classmethod
    def failure(cls, msg: str, **kwargs) -> "ReorgResult":
        return cls(ok=False, msg=msg, **kwargs)

    def __repr__(self) -> str:
        status = "OK" if self.ok else "FAIL"
        return (f"<ReorgResult {status} fork=#{self.fork_height} "
                f"{self.old_height}→{self.new_height} "
                f"-{self.blocks_removed}/+{self.blocks_added}: {self.msg}>")


# ============================================================
# Helpers internos
# ============================================================
def _normalize_blocks(blocks: list) -> list:
    """
    Filtra blocos com 'height' e 'hash' válidos.
    Ordena por altura ascendente.
    """
    if not blocks:
        return []
    valid = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        try:
            h = int(b.get("height", -1))
            if h < 0:
                continue
            if not b.get("hash"):
                continue
            valid.append(b)
        except (ValueError, TypeError):
            continue
    valid.sort(key=lambda b: int(b["height"]))
    return valid


def _work_of_blocks(blocks: list) -> int:
    """
    Soma do chainwork dos blocos.
    Usa work_from_difficulty do blockchain.py (2^256/target).
    """
    try:
        from blockchain import work_from_difficulty
    except ImportError:
        # Fallback (não deveria acontecer em runtime)
        return sum(16 ** int(b.get("difficulty", 0)) for b in blocks)

    total = 0
    for b in blocks:
        try:
            total += work_from_difficulty(int(b.get("difficulty", 0)))
        except (ValueError, TypeError):
            continue
    return total


def _local_work_after(chain, fork_height: int) -> int:
    """
    Chainwork dos blocos locais > fork_height.
    Usa `cumulative_work` gravado em cada bloco (v9.2.0).
    """
    local_h = chain.db.height()
    if local_h < 0:
        return 0

    tip = chain.db.get_block(local_h)
    if not tip:
        return 0
    tip_cw = int(tip.get("cumulative_work", 0))

    if fork_height < 0:
        return tip_cw

    fork_blk = chain.db.get_block(fork_height)
    if not fork_blk:
        return tip_cw
    fork_cw = int(fork_blk.get("cumulative_work", 0))

    return max(0, tip_cw - fork_cw)


# ============================================================
# find_fork_point — ancestral comum
# ============================================================
def find_fork_point(chain, remote_blocks: list,
                    max_depth: int = 1000) -> int:
    """
    Encontra a MAIOR altura `h` tal que:
      - bloco local em h existe
      - bloco remoto em h existe
      - os hashes são iguais

    Retorna -1 se nenhum ponto em comum for encontrado
    (dentro do limite max_depth).

    Parâmetros:
        chain        : instância de Blockchain
        remote_blocks: lista de blocos (dicts) da cadeia remota
        max_depth    : quantos blocos olhar para trás
    """
    if not remote_blocks:
        return -1

    # Indexa blocos remotos por altura
    remote_by_h = {}
    for b in remote_blocks:
        if not isinstance(b, dict):
            continue
        try:
            h = int(b.get("height", -1))
            if h < 0:
                continue
            remote_by_h[h] = str(b.get("hash", ""))
        except (ValueError, TypeError):
            continue

    if not remote_by_h:
        return -1

    # Itera das alturas mais altas para as mais baixas
    local_h = chain.db.height()
    if local_h < 0:
        return -1

    # Limite inferior: não olhar mais de max_depth para trás
    lower_bound = max(0, local_h - max_depth)

    # Candidatos: alturas remotas <= local_h, ordenadas desc
    candidates = sorted(
        (h for h in remote_by_h.keys()
         if lower_bound <= h <= local_h),
        reverse=True
    )

    for h in candidates:
        local_blk = chain.db.get_block(h)
        if not local_blk:
            continue
        if str(local_blk.get("hash", "")) == remote_by_h[h]:
            return h

    return -1


# ============================================================
# reorg_to — reorganização completa
# ============================================================
def reorg_to(chain, new_blocks: list, max_depth: int = 100,
             allow_genesis_reorg: bool = False) -> ReorgResult:
    """
    Aplica uma reorganização para a cadeia representada por
    `new_blocks`.

    Fluxo:
      1. Normaliza e ordena `new_blocks`
      2. Encontra o ancestral comum (find_fork_point)
      3. Verifica max_depth
      4. [v2.0] Chama chain.validate_reorg() — finality guard
      5. Compara chainwork (nova cadeia deve ter MAIS work)
      6. Backup dos blocos locais que serão descartados
      7. Delete blocos acima do fork
      8. Aplica `new_blocks` acima do fork
      9. Em caso de falha no meio → rollback para os blocos backup

    Retorna: ReorgResult
    """
    # ─── 1. Sanidade ──────────────────────────────────────────
    if not new_blocks:
        return ReorgResult.failure("lista de blocos vazia")

    valid_blocks = _normalize_blocks(new_blocks)
    if not valid_blocks:
        return ReorgResult.failure("nenhum bloco válido na lista")

    local_h = chain.db.height()
    old_height = local_h

    # ─── 2. Ancestral comum ───────────────────────────────────
    fork_height = find_fork_point(chain, valid_blocks, max_depth)

    if fork_height < 0:
        # Nenhum ponto em comum
        if not allow_genesis_reorg:
            return ReorgResult.failure(
                "sem ancestral em comum (allow_genesis_reorg=False)"
            )
        # Reorg a partir do genesis
        fork_height = -1
        log.warning("[REORG] allow_genesis_reorg=True — reorg até genesis")

    # ─── 3. Depth check ───────────────────────────────────────
    depth = (local_h - fork_height) if local_h >= 0 else 0
    if depth > max_depth:
        return ReorgResult.failure(
            f"reorg muito profundo ({depth} > {max_depth})"
        )

    # ─── 4. [v2.0] FINALITY GUARD ─────────────────────────────
    if hasattr(chain, "validate_reorg"):
        try:
            ok_fin, msg_fin = chain.validate_reorg(fork_height, valid_blocks)
        except Exception as e:
            log.exception("[REORG] validate_reorg exception")
            return ReorgResult.failure(f"finality check erro: {e}")

        if not ok_fin:
            log.warning(f"[REORG] bloqueado por finality: {msg_fin}")
            return ReorgResult.failure(f"finality: {msg_fin}")

    # ─── 5. Chainwork ─────────────────────────────────────────
    # Blocos que serão aplicados
    blocks_to_apply = [
        b for b in valid_blocks
        if int(b["height"]) > fork_height
    ]
    if not blocks_to_apply:
        return ReorgResult.failure("nenhum bloco novo para aplicar")

    new_work = _work_of_blocks(blocks_to_apply)
    local_work = _local_work_after(chain, fork_height)

    if new_work <= local_work:
        return ReorgResult.failure(
            f"nova cadeia tem menos work "
            f"(new={new_work}, local={local_work})"
        )

    log.warning(
        f"[REORG] aplicando: fork=#{fork_height}, "
        f"new_blocks={len(blocks_to_apply)}, "
        f"new_work={new_work} > local_work={local_work}"
    )

    # ─── 6. Backup dos blocos locais a descartar ──────────────
    backup_blocks = []
    if local_h > fork_height:
        for h in range(fork_height + 1, local_h + 1):
            b = chain.db.get_block(h)
            if b:
                backup_blocks.append(b)

    blocks_removed = len(backup_blocks)

    # ─── 7. Delete blocos acima do fork ───────────────────────
    try:
        chain.db.delete_blocks_above(fork_height)
    except Exception as e:
        log.exception("[REORG] falha ao deletar blocos")
        return ReorgResult.failure(f"falha ao deletar: {e}")

    # ─── 8. Aplica nova cadeia ────────────────────────────────
    applied = 0
    try:
        for b in blocks_to_apply:
            h = int(b.get("height", -1))
            ok, msg = chain.accept_block(b)
            if not ok:
                raise ValueError(f"bloco #{h} rejeitado: {msg}")
            applied += 1
    except Exception as e:
        log.error(f"[REORG] falha ao aplicar nova cadeia: {e}")
        log.warning("[REORG] iniciando rollback...")

        # Rollback: apaga tudo acima do fork
        try:
            chain.db.delete_blocks_above(fork_height)
        except Exception as re_err:
            log.exception(f"[REORG] rollback falhou ao deletar: {re_err}")

        # Reaplica os blocos originais
        restored = 0
        for backup in backup_blocks:
            try:
                ok_r, msg_r = chain.accept_block(backup)
                if ok_r:
                    restored += 1
                else:
                    log.error(
                        f"[REORG] restore #{backup['height']} falhou: {msg_r}"
                    )
            except Exception as rest_err:
                log.error(
                    f"[REORG] restore #{backup.get('height')} exception: "
                    f"{rest_err}"
                )

        log.warning(
            f"[REORG] rollback: restaurados {restored}/{blocks_removed} blocos"
        )

        return ReorgResult.failure(
            f"falha ao aplicar (aplicados {applied}/{len(blocks_to_apply)}): "
            f"{e}. Rollback: {restored}/{blocks_removed} restaurados."
        )

    # ─── 9. Sucesso ───────────────────────────────────────────
    new_height = chain.db.height()
    log.warning(
        f"[REORG] OK: #{fork_height} → #{new_height} "
        f"(+{applied} blocos, -{blocks_removed} descartados)"
    )

    return ReorgResult.success(
        f"reorg OK: fork #{fork_height}, +{applied} blocos",
        fork_height=fork_height,
        old_height=old_height,
        new_height=new_height,
        blocks_removed=blocks_removed,
        blocks_added=applied,
    )


# ============================================================
# Auto-teste
# ============================================================
if __name__ == "__main__":
    import os
    import tempfile
    from blockchain import Blockchain

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    os.environ.setdefault("BRN_NETWORK_SECRET",
                          "segredo-de-teste-reorg-1234567")

    print("=" * 60)
    print(" reorg.py — Auto-teste")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "test.db")
        bc = Blockchain(path, auto_genesis=True)
        addr = "brn1qtestaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        pub = "02" + "aa" * 32

        # Mina 5 blocos
        print("\n[1] Minerando 5 blocos...")
        for _ in range(5):
            bc.mine_block(addr, pub)
        print(f"    altura: {bc.db.height()}")
        assert bc.db.height() == 5

        # Testa find_fork_point com a própria cadeia
        print("\n[2] find_fork_point (própria cadeia):")
        all_blocks = [bc.db.get_block(h) for h in range(0, 6)]
        fp = find_fork_point(bc, all_blocks, max_depth=100)
        print(f"    fork point: #{fp}")
        assert fp == 5

        # Testa reorg_to com lista vazia
        print("\n[3] reorg_to com lista vazia:")
        r = reorg_to(bc, [])
        print(f"    {r}")
        assert not r.ok
        assert r.to_tuple() == (False, r.msg)

        # Testa reorg_to sem ancestral comum (block inventado)
        print("\n[4] reorg_to sem ancestral comum:")
        fake_block = {
            "height": 10,
            "hash": "f" * 64,
            "prev_hash": "f" * 64,
            "merkle": "f" * 64,
            "timestamp": int(__import__("time").time()),
            "nonce": 0,
            "difficulty": 1,
            "transactions": [],
        }
        r = reorg_to(bc, [fake_block], max_depth=100)
        print(f"    {r}")
        assert not r.ok

        # Testa finality: reorg para bloco antigo deve ser bloqueado
        print("\n[5] reorg bloqueado por finality (depth 100):")
        # Tenta reorgar para o bloco #2 (que está abaixo de 100 do tip)
        # Como FINALITY_DEPTH=100 e altura=5, ainda não é final
        # Então este teste só passa se finality está ativa
        from blockchain import FINALITY_DEPTH
        print(f"    FINALITY_DEPTH = {FINALITY_DEPTH}")
        print(f"    is_final(#0) = {bc.is_final(0)}")
        print(f"    is_final(#5) = {bc.is_final(5)}")

        # Um bloco é final se height <= current - FINALITY_DEPTH
        # current = 5, FINALITY_DEPTH = 100 → nada é final ainda
        assert not bc.is_final(0)

        # Testa validate_reorg diretamente
        print("\n[6] validate_reorg(0, [fake]):")
        ok, msg = bc.validate_reorg(0, [fake_block])
        print(f"    ok={ok}, msg={msg}")
        assert ok  # Não é final ainda, checkpoint vazio

        bc.db.close()

    print("\n" + "=" * 60)
    print(" reorg.py OK.")
    print("=" * 60)