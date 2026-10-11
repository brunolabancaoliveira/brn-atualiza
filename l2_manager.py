# l2_manager.py — Gerencia ordens L2 BTC->BRN
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_DOWN
import btc_config
from contracts.factory import ContractFactory

log = logging.getLogger(__name__)
SAT = Decimal(10**8)


class L2Error(Exception):
    """Falha de validação / regra de negócio."""


def sats_to_coin(sats: int) -> Decimal:
    return (Decimal(sats) / SAT).quantize(Decimal("0.00000001"))


class L2Manager:
    def __init__(self, db, blockchain=None):
        self.db = db
        self.blockchain = blockchain
        self.factory = ContractFactory(db)

    def create_order(self, btc_amount_sats: int, buyer_brn_address: str,
                     creator_wallet: str = "BRN_TREASURY") -> dict:
        if not isinstance(btc_amount_sats, int):
            raise L2Error("btc_amount_sats deve ser int (satoshi)")
        if btc_amount_sats < btc_config.MIN_BTC_SATS:
            raise L2Error(f"BTC mínimo: {sats_to_coin(btc_config.MIN_BTC_SATS)} BTC")
        if btc_amount_sats > btc_config.MAX_BTC_SATS:
            raise L2Error(f"BTC máximo: {sats_to_coin(btc_config.MAX_BTC_SATS)} BTC")
        if not buyer_brn_address.startswith("brn1"):
            raise L2Error("Endereço BRN inválido, tem que começar com brn1")

        escrow = self.factory.create_btc_to_brn(
            creator_wallet=creator_wallet,
            btc_amount_sats=btc_amount_sats,
            buyer_wallet=buyer_brn_address,
        )
        return {
            "escrow_id":       escrow.escrow_id,
            "btc_address":     btc_config.BTC_RECEIVE_ADDRESS,
            "btc_amount":      str(sats_to_coin(btc_amount_sats)),
            "btc_amount_sats": btc_amount_sats,
            "brn_amount":      str(sats_to_coin(escrow.brn_amount)),
            "brn_amount_sats": escrow.brn_amount,
            "buyer":           buyer_brn_address,
            "status":          "OPEN",
            "expires_at":      escrow.expires_at,
            "l2_hash":         escrow.l2_hash,
            "instructions":    (f"Envie {sats_to_coin(btc_amount_sats)} BTC para "
                                f"{btc_config.BTC_RECEIVE_ADDRESS} em até 24h"),
        }

    def get_order(self, escrow_id: str):
        escrow = self.db.get_l2_escrow_by_id(escrow_id)
        if not escrow:
            return None
        return {
            "escrow_id":   escrow["escrow_id"],
            "status":      escrow["status"],
            "btc_txid":    escrow.get("btc_txid"),
            "btc_amount":  str(sats_to_coin(escrow["btc_expected_sats"])),
            "brn_amount":  str(sats_to_coin(escrow["brn_amount"])),
            "buyer":       escrow["buyer"],
            "btc_address": escrow["btc_address"],
            "created_at":  escrow["created_at"],
            "expires_at":  escrow["expires_at"],
            "released":    escrow["status"] == "RELEASED",
        }

    def list_orders(self, buyer: str | None = None, status: str | None = "OPEN"):
        if buyer:
            return self.factory.list_by_buyer(buyer, status=status)
        return self.db.get_l2_escrows(status=status)

    def quote(self, btc_amount_sats: int) -> dict:
        if not isinstance(btc_amount_sats, int) or btc_amount_sats <= 0:
            raise L2Error("btc_amount_sats deve ser int > 0")
        brn_gross = int(
            (Decimal(btc_amount_sats) * Decimal(str(btc_config.BRN_PER_BTC)))
            .quantize(Decimal("1"), rounding=ROUND_DOWN)
        )
        fee = int(
            (Decimal(brn_gross) * Decimal(str(btc_config.FEE_PERCENT)) / 100)
            .quantize(Decimal("1"), rounding=ROUND_DOWN)
        )
        net = brn_gross - fee
        return {
            "btc_amount":  str(sats_to_coin(btc_amount_sats)),
            "brn_gross":   str(sats_to_coin(brn_gross)),
            "fee_percent": btc_config.FEE_PERCENT,
            "fee_brn":     str(sats_to_coin(fee)),
            "brn_net":     str(sats_to_coin(net)),
            "rate":        f"1 BTC = {btc_config.BRN_PER_BTC} BRN",
        }

    def stats(self) -> dict:
        try:
            pipeline = [{"$group": {"_id": "$status", "n": {"$sum": 1}}}]
            counts = {d["_id"]: d["n"] for d in self.db.l2_escrows.aggregate(pipeline)}
            return {
                "open":         counts.get("OPEN", 0),
                "btc_detected": counts.get("BTC_DETECTED", 0),
                "released":     counts.get("RELEASED", 0),
                "expired":      counts.get("EXPIRED", 0),
                "total":        sum(counts.values()),
                "btc_address":  btc_config.BTC_RECEIVE_ADDRESS,
                "rate":         btc_config.BRN_PER_BTC,
            }
        except Exception as e:
            log.exception("stats() failed")
            return {"error": str(e)}