"""l2_manager.py — Gerencia ordens L2 BTC→BRN
================================================================
Fachada de alto nível para o subsistema L2 (BTC → BRN).

Responsabilidades:
  - Criar ordens (via ContractFactory)
  - Consultar ordens
  - Cotação (quote)
  - Estatísticas
  - Cancelar / expirar ordens

API usada por server.py v9.5:
    L2Manager(db, blockchain)
    .create_order(btc_sats, buyer_brn_address, creator_wallet)
    .get_order(escrow_id) -> dict
    .list_orders(buyer=None, status="OPEN") -> list
    .quote(btc_sats) -> dict
    .stats() -> dict

Requer:
    contracts/ (factory + escrow_btc_brn + vm)
    btc_config.py (opcional — usa defaults se ausente)
================================================================
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal, ROUND_DOWN

from contracts.factory import (
    ContractFactory,
    compute_brn_amounts,
    _rate, _fee_pct, _min_sats, _max_sats, _btc_address,
)
from contracts.escrow_btc_brn import (
    STATUS_OPEN, STATUS_BTC_DETECTED, STATUS_RELEASED,
    STATUS_EXPIRED, STATUS_CANCELLED, VALID_STATUSES,
)

log = logging.getLogger("l2_manager")

SAT = Decimal(10 ** 8)

STATUS_DESCRIPTIONS = {
    STATUS_OPEN:         "Aguardando BTC no endereco",
    STATUS_BTC_DETECTED: "BTC confirmado, aguardando mineracao",
    STATUS_RELEASED:     "Minerado — BRN creditado",
    STATUS_EXPIRED:      "Expirado (24h)",
    STATUS_CANCELLED:    "Cancelado pelo creator",
}


# ============================================================
# ERROS
# ============================================================
class L2Error(Exception):
    """Erro de validação / regra de negócio do L2."""
    pass


# ============================================================
# HELPERS
# ============================================================
def sats_to_coin(sats: int) -> Decimal:
    """Converte satoshis → valor decimal (8 casas)."""
    return (Decimal(int(sats)) / SAT).quantize(Decimal("0.00000001"))


def coin_to_sats(amount) -> int:
    """Converte valor decimal → satoshis (int)."""
    try:
        return int((Decimal(str(amount)) * SAT).quantize(Decimal("1"),
                                                          rounding=ROUND_DOWN))
    except Exception:
        return 0


def _default_btc_address() -> str:
    a = _btc_address()
    if not a or a.startswith("bc1qSEU_"):
        return ""
    return a


# ============================================================
# L2Manager
# ============================================================
class L2Manager:

    def __init__(self, db, blockchain=None):
        self.db = db
        self.blockchain = blockchain
        self.factory = ContractFactory(db)

    # ============================================================
    # CRIAÇÃO DE ORDEM
    # ============================================================
    def create_order(self,
                     btc_amount_sats: int,
                     buyer_brn_address: str,
                     creator_wallet: str = "BRN_TREASURY") -> dict:
        """
        Cria uma ordem BTC → BRN.

        Retorna dict com:
            escrow_id, btc_address, btc_amount, btc_amount_sats,
            brn_amount, brn_amount_sats, buyer, status, expires_at,
            l2_hash, instructions
        """
        if not isinstance(btc_amount_sats, int):
            raise L2Error("btc_amount_sats deve ser int (satoshis)")
        if btc_amount_sats <= 0:
            raise L2Error("btc_amount_sats deve ser > 0")
        if not buyer_brn_address or not buyer_brn_address.startswith("brn1"):
            raise L2Error("Endereco BRN invalido (deve comecar com brn1)")

        # cria via factory (valida min/max internamente)
        try:
            escrow = self.factory.create_btc_to_brn(
                creator_wallet=creator_wallet,
                btc_amount_sats=btc_amount_sats,
                buyer_wallet=buyer_brn_address,
            )
        except ValueError as e:
            raise L2Error(str(e))
        except Exception as e:
            log.exception("create_btc_to_brn falhou")
            raise L2Error(f"falha ao criar ordem: {e}")

        # monta resposta
        btc_addr = escrow.btc_address or _default_btc_address()
        instructions = (
            f"Envie exatamente {sats_to_coin(btc_amount_sats)} BTC para "
            f"{btc_addr} em ate 24h. Apos {_confirmations_required()} "
            f"confirmacoes, o BRN sera creditado automaticamente."
        )

        return {
            "escrow_id":         escrow.escrow_id,
            "btc_address":       btc_addr,
            "btc_amount":        str(sats_to_coin(btc_amount_sats)),
            "btc_amount_sats":   btc_amount_sats,
            "brn_amount":        str(sats_to_coin(escrow.brn_amount)),
            "brn_amount_sats":   escrow.brn_amount,
            "buyer":             buyer_brn_address,
            "creator":           creator_wallet,
            "status":            escrow.status,
            "status_desc":       STATUS_DESCRIPTIONS.get(escrow.status,
                                                          escrow.status),
            "created_at":        escrow.created_at,
            "expires_at":        escrow.expires_at,
            "l2_hash":           escrow.l2_hash,
            "instructions":      instructions,
        }

    # ============================================================
    # CONSULTAS
    # ============================================================
    def get_order(self, escrow_id: str) -> dict | None:
        if not escrow_id:
            return None
        try:
            row = self.db.get_l2_escrow_by_id(escrow_id)
        except Exception as e:
            log.warning(f"get_l2_escrow_by_id({escrow_id}): {e}")
            return None
        if not row:
            return None

        return self._format_order(row)

    def list_orders(self,
                    buyer: str | None = None,
                    status: str | None = "OPEN",
                    limit: int = 100) -> list:
        """
        Lista ordens. Filtra por buyer e/ou status.
        status="ALL" ou None → todos os status.
        """
        try:
            if status and status != "ALL":
                rows = self.db.get_l2_escrows(status=status)
            else:
                rows = self.db.get_l2_escrows()
        except Exception as e:
            log.warning(f"get_l2_escrows: {e}")
            return []

        out = []
        for r in rows:
            if buyer and r.get("buyer") != buyer:
                continue
            out.append(self._format_order(r))
            if len(out) >= limit:
                break
        return out

    # ============================================================
    # COTAÇÃO
    # ============================================================
    def quote(self, btc_amount_sats: int) -> dict:
        """
        Retorna cotação para um valor em sats.
        Não persiste nada.
        """
        if not isinstance(btc_amount_sats, int) or btc_amount_sats <= 0:
            raise L2Error("btc_amount_sats deve ser int > 0")

        min_s = _min_sats()
        max_s = _max_sats()
        if btc_amount_sats < min_s:
            raise L2Error(
                f"BTC minimo: {sats_to_coin(min_s)} BTC")
        if btc_amount_sats > max_s:
            raise L2Error(
                f"BTC maximo: {sats_to_coin(max_s)} BTC")

        amounts = compute_brn_amounts(btc_amount_sats)
        return {
            "btc_amount":  str(sats_to_coin(btc_amount_sats)),
            "btc_sats":    btc_amount_sats,
            "brn_gross":   str(sats_to_coin(amounts["brn_gross"])),
            "fee_percent": amounts["fee_pct"],
            "fee_brn":     str(sats_to_coin(amounts["fee"])),
            "brn_net":     str(sats_to_coin(amounts["brn_net"])),
            "brn_net_sats": amounts["brn_net"],
            "rate":        f"1 BTC = {amounts['rate']} BRN",
            "min_btc":     str(sats_to_coin(min_s)),
            "max_btc":     str(sats_to_coin(max_s)),
            "btc_address": _default_btc_address(),
            "min_confirmations": _confirmations_required(),
        }

    # ============================================================
    # ESTATÍSTICAS
    # ============================================================
    def stats(self) -> dict:
        out = {
            "open":         0,
            "btc_detected": 0,
            "released":     0,
            "expired":      0,
            "cancelled":    0,
            "total":        0,
            "btc_address":  _default_btc_address(),
            "rate":         _rate(),
            "fee_percent":  _fee_pct(),
        }
        try:
            for st in VALID_STATUSES:
                rows = self.db.get_l2_escrows(status=st)
                n = len(rows) if rows else 0
                if st == STATUS_OPEN:
                    out["open"] = n
                elif st == STATUS_BTC_DETECTED:
                    out["btc_detected"] = n
                elif st == STATUS_RELEASED:
                    out["released"] = n
                elif st == STATUS_EXPIRED:
                    out["expired"] = n
                elif st == STATUS_CANCELLED:
                    out["cancelled"] = n
                out["total"] += n
        except Exception as e:
            log.warning(f"stats: {e}")
            out["error"] = str(e)
        return out

    # ============================================================
    # AÇÕES
    # ============================================================
    def cancel_order(self, escrow_id: str) -> dict:
        """Cancela uma ordem OPEN."""
        return self.factory.cancel(escrow_id)

    def expire_overdue(self, limit: int = 500) -> int:
        """Marca como EXPIRED os escrows vencidos. Retorna quantos."""
        return self.factory.expire_overdue(limit=limit)

    # ============================================================
    # APLICAÇÃO DE DEPÓSITO BTC (chamado pelo btc_watcher)
    # ============================================================
    def apply_btc_deposit(self,
                          escrow_id: str,
                          btc_txid: str,
                          btc_amount_sats: int) -> dict:
        """
        Aplica um depósito BTC detectado.

        Fluxo:
          1. Valida escrow (existe, OPEN, não expirado, valor suficiente)
          2. Chama blockchain.add_l2_transaction()
             → blockchain vai marcar btc_txid_used
             → blockchain vai criar coinbase L2 e jogar na mempool
             → blockchain vai aguardar mineração

        Retorna {"ok": bool, "txid": str, "msg": str}.
        """
        if self.blockchain is None:
            return {"ok": False, "msg": "blockchain nao injetada"}

        escrow = self.get_order(escrow_id)
        if not escrow:
            return {"ok": False, "msg": "escrow nao existe"}

        if escrow["status"] != STATUS_OPEN:
            return {"ok": False,
                    "msg": f"escrow em estado {escrow['status']}"}

        if int(escrow["expires_at"]) < int(time.time()):
            self.db.update_l2_escrow_status(escrow_id, STATUS_EXPIRED)
            return {"ok": False, "msg": "escrow expirado"}

        expected = int(escrow["btc_expected_sats"])
        # tolerância de 1% (fee)
        if int(btc_amount_sats) < int(expected * 0.99):
            return {"ok": False,
                    "msg": f"BTC insuficiente ({btc_amount_sats} < {expected})"}

        # anti-replay
        try:
            if self.db.is_btc_txid_used(btc_txid):
                return {"ok": False, "msg": "btc_txid ja utilizado"}
        except Exception:
            pass

        # monta l2_tx para blockchain.add_l2_transaction
        l2_tx = {
            "escrow_id":   escrow_id,
            "buyer":       escrow["buyer"],
            "brn_amount":  int(escrow["brn_amount"]),
            "btc_txid":    btc_txid,
            "btc_address": escrow.get("btc_address", ""),
            "l2_hash":     escrow.get("l2_hash", ""),
            "timestamp":   int(time.time()),
            "pubkey":      "00" * 33,
        }

        try:
            txid = self.blockchain.add_l2_transaction(l2_tx)
        except Exception as e:
            log.exception("add_l2_transaction falhou")
            return {"ok": False, "msg": str(e)}

        return {
            "ok":   True,
            "txid": txid,
            "msg":  f"L2 tx enviada para mempool (txid={txid[:16]}...)",
        }

    # ============================================================
    # FORMAT
    # ============================================================
    @staticmethod
    def _format_order(row: dict) -> dict:
        status = row.get("status", "")
        btc_sats = int(row.get("btc_expected_sats", 0) or 0)
        brn_sats = int(row.get("brn_amount", 0) or 0)
        return {
            "escrow_id":         row.get("escrow_id", ""),
            "creator":           row.get("creator", ""),
            "buyer":             row.get("buyer", ""),
            "status":            status,
            "status_desc":       STATUS_DESCRIPTIONS.get(status, status),
            "btc_address":       row.get("btc_address", ""),
            "btc_txid":          row.get("btc_txid", ""),
            "btc_amount":        str(sats_to_coin(btc_sats)),
            "btc_amount_sats":   btc_sats,
            "brn_amount":        str(sats_to_coin(brn_sats)),
            "brn_amount_sats":   brn_sats,
            "created_at":        int(row.get("created_at", 0) or 0),
            "expires_at":        int(row.get("expires_at", 0) or 0),
            "l2_hash":           row.get("l2_hash", ""),
            "released":          status == STATUS_RELEASED,
        }


# ============================================================
# HELPERS INTERNOS
# ============================================================
def _confirmations_required() -> int:
    try:
        import btc_config
        return int(getattr(btc_config, "BTC_MIN_CONFIRMATIONS", 3))
    except Exception:
        return 3


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    class FakeDB:
        def __init__(self):
            self.rows = {}
            self.used_txids = set()
        def save_l2_escrow(self, d):
            self.rows[d["escrow_id"]] = d
        def get_l2_escrow_by_id(self, eid):
            return self.rows.get(eid)
        def get_l2_escrows(self, status=None):
            vals = list(self.rows.values())
            if status:
                return [r for r in vals if r.get("status") == status]
            return vals
        def update_l2_escrow_status(self, eid, status, btc_txid=None):
            if eid in self.rows:
                self.rows[eid]["status"] = status
                if btc_txid:
                    self.rows[eid]["btc_txid"] = btc_txid
        def is_btc_txid_used(self, txid):
            return txid in self.used_txids

    print("Testando L2Manager...")

    db = FakeDB()
    l2 = L2Manager(db, blockchain=None)

    # 1. Cotação
    q = l2.quote(100_000)
    print(f"  [info] quote: {q['btc_amount']} BTC → "
          f"{q['brn_net']} BRN (fee {q['fee_percent']}%)")
    assert Decimal(q["brn_net"]) > 0

    # 2. Cria ordem
    buyer = "brn1qtestaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    o = l2.create_order(100_000, buyer)
    assert o["status"] == STATUS_OPEN
    assert o["buyer"] == buyer
    assert o["escrow_id"].startswith("esc_")
    print(f"  [ok] create_order: {o['escrow_id']}")

    # 3. Consulta
    o2 = l2.get_order(o["escrow_id"])
    assert o2 is not None
    assert o2["escrow_id"] == o["escrow_id"]
    print("  [ok] get_order")

    # 4. Lista
    lst = l2.list_orders(buyer=buyer, status="OPEN")
    assert len(lst) == 1
    print(f"  [ok] list_orders: {len(lst)} aberta(s)")

    # 5. Estatísticas
    s = l2.stats()
    print(f"  [info] stats: open={s['open']} total={s['total']} rate={s['rate']}")
    assert s["open"] == 1

    # 6. Cancelamento
    r = l2.cancel_order(o["escrow_id"])
    assert r["ok"]
    print("  [ok] cancel_order")

    # 7. Validações
    for bad_btc, bad_buyer in [(-1, buyer), (100_000, "not_brn")]:
        try:
            l2.create_order(bad_btc, bad_buyer)
            raise AssertionError("deveria rejeitar")
        except L2Error:
            pass
    print("  [ok] validações de entrada")

    # 8. Depósito sem blockchain
    o3 = l2.create_order(100_000, buyer)
    r = l2.apply_btc_deposit(o3["escrow_id"], "txid_fake", 100_000)
    assert not r["ok"]   # blockchain=None
    print("  [ok] apply_btc_deposit rejeita sem blockchain")

    print("L2Manager OK.")