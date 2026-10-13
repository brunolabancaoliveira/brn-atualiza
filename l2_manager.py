# l2_manager.py — Gerencia ordens L2 BTC->BRN (v2)
# ============================================================
# v2:
#   [FIX] Import de ContractFactory agora é lazy (try/except).
#         Antes quebrava se contracts/factory.py não existisse.
#   [FIX] stats() agora usa SQL puro (GROUP BY) em vez de
#         self.db.l2_escrows.aggregate() — que é sintaxe MongoDB
#         aplicada em SQLite (nunca funcionava).
#   [FIX] btc_config.MIN_BTC_SATS / MAX_BTC_SATS com fallback
#         caso o btc_config antigo não tenha essas constantes.
#   [FIX] create_order() valida endereço BRN com WalletManager
#         (antes só checava startswith('brn1')).
#   [NEW] info() com config resolvida.
# ============================================================
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_DOWN

import btc_config

log = logging.getLogger(__name__)
SAT = Decimal(10**8)


# ============================================================
# IMPORTS OPCIONAIS (lazy)
# ============================================================
_ContractFactory = None
try:
    from contracts.factory import ContractFactory as _ContractFactory
except ImportError:
    log.info("contracts.factory não disponível — usando factory interna")


# ============================================================
# VALIDAÇÕES / LIMITES (com fallback)
# ============================================================
MIN_BTC_SATS = int(getattr(btc_config, "MIN_BTC_SATS", 10_000))
MAX_BTC_SATS = int(getattr(btc_config, "MAX_BTC_SATS", 1_000_000_000))


class L2Error(Exception):
    """Falha de validação / regra de negócio."""


def sats_to_coin(sats: int) -> Decimal:
    return (Decimal(sats) / SAT).quantize(Decimal("0.00000001"))


# ============================================================
# FACTORY INTERNA (fallback)
# ============================================================
class _SimpleEscrow:
    """Escrow mínimo para quando contracts.factory não existe."""

    def __init__(self, escrow_id: str, creator: str, buyer: str,
                 btc_amount_sats: int, brn_amount: int,
                 expires_at: int, l2_hash: str):
        self.escrow_id = escrow_id
        self.creator = creator
        self.buyer = buyer
        self.btc_amount_sats = btc_amount_sats
        self.brn_amount = brn_amount
        self.expires_at = expires_at
        self.l2_hash = l2_hash

    def to_dict(self) -> dict:
        return {
            "escrow_id": self.escrow_id,
            "creator": self.creator,
            "buyer": self.buyer,
            "brn_amount": self.brn_amount,
            "btc_expected_sats": self.btc_amount_sats,
            "btc_address": getattr(btc_config, "BTC_RECEIVE_ADDRESS", ""),
            "btc_txid": None,
            "status": "OPEN",
            "created_at": int(__import__("time").time()),
            "expires_at": self.expires_at,
            "l2_hash": self.l2_hash,
        }


class _SimpleFactory:
    """Factory de fallback quando contracts.factory não existe."""

    def __init__(self, db):
        self.db = db

    def create_btc_to_brn(self, creator_wallet: str,
                          btc_amount_sats: int,
                          buyer_wallet: str):
        import hashlib
        import secrets
        import time as _time

        escrow_id = "esc1" + secrets.token_hex(12)
        expires_at = int(_time.time()) + 24 * 3600
        seed = (
            f"{escrow_id}|{creator_wallet}|{buyer_wallet}|"
            f"{btc_amount_sats}|{_time.time_ns()}"
        ).encode()
        l2_hash = hashlib.sha256(seed).hexdigest()
        brn_amount = int(btc_amount_sats * getattr(btc_config, "BRN_PER_BTC", 1))
        return _SimpleEscrow(
            escrow_id=escrow_id,
            creator=creator_wallet,
            buyer=buyer_wallet,
            btc_amount_sats=btc_amount_sats,
            brn_amount=brn_amount,
            expires_at=expires_at,
            l2_hash=l2_hash,
        )

    def list_by_buyer(self, buyer: str, status: str = None):
        if status:
            escrows = self.db.get_l2_escrows(status=status)
        else:
            escrows = self.db.get_l2_escrows()
        return [e for e in escrows if e.get("buyer") == buyer]


# ============================================================
# L2 MANAGER
# ============================================================
class L2Manager:
    def __init__(self, db, blockchain=None):
        self.db = db
        self.blockchain = blockchain
        if _ContractFactory is not None:
            try:
                self.factory = _ContractFactory(db)
            except Exception as e:
                log.warning(f"ContractFactory falhou ({e}) — usando fallback")
                self.factory = _SimpleFactory(db)
        else:
            self.factory = _SimpleFactory(db)

    # --------------------------------------------------------
    # CREATE ORDER
    # --------------------------------------------------------
    def create_order(self, btc_amount_sats: int, buyer_brn_address: str,
                     creator_wallet: str = "BRN_TREASURY") -> dict:
        if not isinstance(btc_amount_sats, int):
            raise L2Error("btc_amount_sats deve ser int (satoshis)")
        if btc_amount_sats < MIN_BTC_SATS:
            raise L2Error(
                f"BTC mínimo: {sats_to_coin(MIN_BTC_SATS)} BTC "
                f"({MIN_BTC_SATS} sats)"
            )
        if btc_amount_sats > MAX_BTC_SATS:
            raise L2Error(
                f"BTC máximo: {sats_to_coin(MAX_BTC_SATS)} BTC "
                f"({MAX_BTC_SATS} sats)"
            )

        # Validação de endereço BRN (usa WalletManager se disponível)
        buyer = (buyer_brn_address or "").strip()
        if not buyer:
            raise L2Error("Endereço BRN vazio")
        try:
            from wallet import WalletManager
            if not WalletManager.validate_address(buyer):
                raise L2Error(f"Endereço BRN inválido: {buyer[:16]}...")
        except ImportError:
            # Fallback simples se wallet.py não estiver acessível
            if not buyer.startswith("brn1"):
                raise L2Error("Endereço BRN deve começar com 'brn1'")

        # Verifica se o endereço BTC de recebimento está configurado
        btc_addr = getattr(btc_config, "BTC_RECEIVE_ADDRESS", "") or ""
        if not btc_addr or "SEU_ENDERECO" in btc_addr:
            raise L2Error(
                "BTC_RECEIVE_ADDRESS não configurado. "
                "Edite btc_config.py antes de criar ordens."
            )

        try:
            escrow = self.factory.create_btc_to_brn(
                creator_wallet=creator_wallet,
                btc_amount_sats=btc_amount_sats,
                buyer_wallet=buyer,
            )
        except Exception as e:
            log.exception("create_btc_to_brn falhou")
            raise L2Error(f"Falha ao criar escrow: {e}")

        return {
            "escrow_id":       escrow.escrow_id,
            "btc_address":     btc_addr,
            "btc_amount":      str(sats_to_coin(btc_amount_sats)),
            "btc_amount_sats": btc_amount_sats,
            "brn_amount":      str(sats_to_coin(escrow.brn_amount)),
            "brn_amount_sats": escrow.brn_amount,
            "buyer":           buyer,
            "status":          "OPEN",
            "expires_at":      escrow.expires_at,
            "l2_hash":         escrow.l2_hash,
            "instructions":    (
                f"Envie {sats_to_coin(btc_amount_sats)} BTC para "
                f"{btc_addr} em até 24h"
            ),
        }

    # --------------------------------------------------------
    # GET / LIST
    # --------------------------------------------------------
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

    def list_orders(self, buyer: str | None = None,
                    status: str | None = "OPEN"):
        if buyer:
            return self.factory.list_by_buyer(buyer, status=status)
        return self.db.get_l2_escrows(status=status)

    # --------------------------------------------------------
    # QUOTE
    # --------------------------------------------------------
    def quote(self, btc_amount_sats: int) -> dict:
        if not isinstance(btc_amount_sats, int) or btc_amount_sats <= 0:
            raise L2Error("btc_amount_sats deve ser int > 0")

        brn_per_btc = Decimal(str(getattr(btc_config, "BRN_PER_BTC", 0)))
        fee_pct = Decimal(str(getattr(btc_config, "FEE_PERCENT", 0)))

        brn_gross = int(
            (Decimal(btc_amount_sats) * brn_per_btc)
            .quantize(Decimal("1"), rounding=ROUND_DOWN)
        )
        fee = int(
            (Decimal(brn_gross) * fee_pct / Decimal(100))
            .quantize(Decimal("1"), rounding=ROUND_DOWN)
        )
        net = brn_gross - fee

        return {
            "btc_amount":  str(sats_to_coin(btc_amount_sats)),
            "brn_gross":   str(sats_to_coin(brn_gross)),
            "fee_percent": float(fee_pct),
            "fee_brn":     str(sats_to_coin(fee)),
            "brn_net":     str(sats_to_coin(net)),
            "rate":        f"1 BTC = {brn_per_btc} BRN",
        }

    # --------------------------------------------------------
    # STATS (SQL puro, não MongoDB)
    # --------------------------------------------------------
    def stats(self) -> dict:
        """
        v2: usa GROUP BY do SQLite. Antes tentava aggregate()
        (que é MongoDB) em SQLite e nunca funcionava.
        """
        counts = {
            "OPEN": 0,
            "BTC_DETECTED": 0,
            "RELEASED": 0,
            "EXPIRED": 0,
            "CANCELLED": 0,
        }
        try:
            rows = self.db.conn.execute(
                "SELECT status, COUNT(*) AS n FROM l2_escrows GROUP BY status"
            ).fetchall()
            for r in rows:
                counts[r["status"]] = r["n"]
        except Exception as e:
            log.debug(f"stats SQL falhou: {e}")

        total = sum(counts.values())

        return {
            "open":         counts.get("OPEN", 0),
            "btc_detected": counts.get("BTC_DETECTED", 0),
            "released":     counts.get("RELEASED", 0),
            "expired":      counts.get("EXPIRED", 0),
            "cancelled":    counts.get("CANCELLED", 0),
            "total":        total,
            "btc_address":  getattr(btc_config, "BTC_RECEIVE_ADDRESS", ""),
            "rate":         getattr(btc_config, "BRN_PER_BTC", 0),
            "network":      getattr(btc_config, "BTC_NETWORK", ""),
        }

    # --------------------------------------------------------
    # INFO
    # --------------------------------------------------------
    def info(self) -> dict:
        return {
            "min_btc_sats":     MIN_BTC_SATS,
            "max_btc_sats":     MAX_BTC_SATS,
            "brn_per_btc":      getattr(btc_config, "BRN_PER_BTC", 0),
            "fee_percent":      getattr(btc_config, "FEE_PERCENT", 0),
            "min_confirmations": getattr(btc_config, "BTC_MIN_CONFIRMATIONS", 0),
            "network":          getattr(btc_config, "BTC_NETWORK", ""),
            "btc_address":      getattr(btc_config, "BTC_RECEIVE_ADDRESS", ""),
            "factory":          type(self.factory).__name__,
        }
