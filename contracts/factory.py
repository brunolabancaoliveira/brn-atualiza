"""contracts/factory.py — Fábrica de escrows L2 BTC→BRN
================================================================
Cria ordens de câmbio BTC→BRN com:
  - Validação de limites (MIN/MAX BTC)
  - Cálculo de taxa (FEE_PERCENT)
  - Cotação fixa (BRN_PER_BTC)
  - Geração de escrow_id único
  - Persistência no DB
  - Cálculo de l2_hash

API usada por l2_manager.py:
    f = ContractFactory(db)
    e = f.create_btc_to_brn(creator_wallet, btc_amount_sats, buyer_wallet)
    lista = f.list_by_buyer(buyer, status="OPEN")

Requer:
    contracts/escrow_btc_brn.py
    btc_config.py (opcional — usa defaults se ausente)
================================================================
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time

from .escrow_btc_brn import (
    EscrowBTCtoBRN,
    create_new,
    generate_escrow_id,
    STATUS_OPEN,
    STATUS_BTC_DETECTED,
    STATUS_RELEASED,
    STATUS_EXPIRED,
    STATUS_CANCELLED,
    VALID_STATUSES,
)

log = logging.getLogger("contracts.factory")


# ============================================================
# DEFAULTS (usados se btc_config.py não existir)
# ============================================================
DEFAULT_BRN_PER_BTC   = 10_000
DEFAULT_FEE_PERCENT   = 2.0
DEFAULT_MIN_BTC_SATS  = 10_000          # 0.0001 BTC
DEFAULT_MAX_BTC_SATS  = 100_000_000     # 1 BTC
DEFAULT_BTC_ADDRESS   = ""
DEFAULT_ESCROW_TTL_S  = 24 * 3600       # 24h


# ============================================================
# HELPERS — lê btc_config.py com fallback
# ============================================================
def _cfg(name: str, default):
    """Lê atributo de btc_config.py, ou retorna default."""
    try:
        import btc_config
        return getattr(btc_config, name, default)
    except ImportError:
        return default
    except Exception:
        return default


def _rate() -> float:
    v = _cfg("BRN_PER_BTC", DEFAULT_BRN_PER_BTC)
    try:
        return float(v)
    except (ValueError, TypeError):
        return float(DEFAULT_BRN_PER_BTC)


def _fee_pct() -> float:
    v = _cfg("FEE_PERCENT", DEFAULT_FEE_PERCENT)
    try:
        return float(v)
    except (ValueError, TypeError):
        return float(DEFAULT_FEE_PERCENT)


def _min_sats() -> int:
    v = _cfg("MIN_BTC_SATS", DEFAULT_MIN_BTC_SATS)
    try:
        return int(v)
    except (ValueError, TypeError):
        return DEFAULT_MIN_BTC_SATS


def _max_sats() -> int:
    v = _cfg("MAX_BTC_SATS", DEFAULT_MAX_BTC_SATS)
    try:
        return int(v)
    except (ValueError, TypeError):
        return DEFAULT_MAX_BTC_SATS


def _btc_address() -> str:
    return str(_cfg("BTC_RECEIVE_ADDRESS", DEFAULT_BTC_ADDRESS) or "")


# ============================================================
# CÁLCULO DE COTAÇÃO
# ============================================================
def compute_brn_amounts(btc_sats: int) -> dict:
    """
    Calcula valores da ordem:
        brn_gross = btc_sats * rate
        fee       = brn_gross * fee_pct / 100
        brn_net   = brn_gross - fee

    Retorna: {"brn_gross": int, "fee": int, "brn_net": int,
              "rate": float, "fee_pct": float}
    """
    rate = _rate()
    fee_pct = _fee_pct()
    brn_gross = int(btc_sats * rate)
    fee = int(brn_gross * fee_pct / 100)
    brn_net = brn_gross - fee
    return {
        "brn_gross": brn_gross,
        "fee":       fee,
        "brn_net":   brn_net,
        "rate":      rate,
        "fee_pct":   fee_pct,
    }


# ============================================================
# FACTORY
# ============================================================
class ContractFactory:
    """Cria e persiste escrows BTC→BRN."""

    def __init__(self, db):
        self.db = db

    # ============================================================
    # Validações
    # ============================================================
    def _validate_buyer(self, buyer: str) -> None:
        if not buyer or not isinstance(buyer, str):
            raise ValueError("buyer obrigatorio")
        if not buyer.startswith("brn1"):
            raise ValueError("buyer deve ser endereco brn1...")
        if len(buyer) < 20 or len(buyer) > 128:
            raise ValueError("buyer com tamanho invalido")

    def _validate_btc_amount(self, sats: int) -> int:
        if not isinstance(sats, int) or sats <= 0:
            raise ValueError("btc_amount_sats deve ser int > 0")
        min_s = _min_sats()
        max_s = _max_sats()
        if sats < min_s:
            raise ValueError(
                f"BTC abaixo do minimo ({sats} < {min_s} sats)")
        if sats > max_s:
            raise ValueError(
                f"BTC acima do maximo ({sats} > {max_s} sats)")
        return sats

    # ============================================================
    # create_btc_to_brn
    # ============================================================
    def create_btc_to_brn(self,
                          creator_wallet: str,
                          btc_amount_sats: int,
                          buyer_wallet: str,
                          ttl_s: int = DEFAULT_ESCROW_TTL_S) -> EscrowBTCtoBRN:
        """
        Cria ordem BTC→BRN e persiste no DB.

        Parâmetros:
            creator_wallet  : quem criou (tipicamente "BRN_TREASURY")
            btc_amount_sats : quantidade de BTC esperada (satoshis)
            buyer_wallet    : endereço BRN que receberá os BRN
            ttl_s           : tempo de vida em segundos (default 24h)

        Returns:
            EscrowBTCtoBRN já persistido no DB.

        Raises:
            ValueError se qualquer validação falhar.
        """
        # ---- validações ----
        self._validate_buyer(buyer_wallet)
        self._validate_btc_amount(btc_amount_sats)

        btc_address = _btc_address()
        if not btc_address or btc_address.startswith("bc1qSEU_"):
            log.warning("BTC_RECEIVE_ADDRESS nao configurado em btc_config.py")

        # ---- cálculo ----
        amounts = compute_brn_amounts(btc_amount_sats)
        brn_net = amounts["brn_net"]

        if brn_net <= 0:
            raise ValueError("brn_amount calculado <= 0")

        # ---- cria escrow ----
        now = int(time.time())
        escrow_id = generate_escrow_id()
        expires_at = now + int(ttl_s)

        # l2_hash determinístico
        l2_hash = hashlib.sha256(
            f"{escrow_id}:{buyer_wallet}:{brn_net}:{btc_amount_sats}".encode()
        ).hexdigest()

        escrow = EscrowBTCtoBRN(
            escrow_id=escrow_id,
            creator=creator_wallet or "BRN_TREASURY",
            buyer=buyer_wallet,
            brn_amount=brn_net,
            btc_expected_sats=btc_amount_sats,
            btc_address=btc_address,
            created_at=now,
            expires_at=expires_at,
            l2_hash=l2_hash,
            status=STATUS_OPEN,
        )

        # ---- persiste ----
        try:
            self.db.save_l2_escrow(escrow.to_dict())
        except Exception as e:
            log.error(f"falha ao salvar escrow: {e}")
            raise RuntimeError(f"falha ao salvar escrow: {e}") from e

        log.info(
            f"[FACTORY] escrow {escrow_id} criado — "
            f"btc={btc_amount_sats/1e8:.8f} brn={brn_net/1e8:.8f} "
            f"buyer={buyer_wallet[:16]}...")

        return escrow

    # ============================================================
    # Consultas
    # ============================================================
    def list_by_buyer(self, buyer: str, status: str | None = "OPEN") -> list:
        """
        Lista escrows de um buyer.
        status=None → todos os status.
        status="OPEN" → apenas OPEN.
        """
        if not buyer or not buyer.startswith("brn1"):
            return []
        try:
            if status and status != "ALL":
                rows = self.db.get_l2_escrows(status=status)
            else:
                rows = self.db.get_l2_escrows()
        except Exception as e:
            log.warning(f"list_by_buyer: {e}")
            return []
        return [r for r in rows if r.get("buyer") == buyer]

    def list_by_creator(self, creator: str,
                        status: str | None = None) -> list:
        if not creator:
            return []
        try:
            if status and status != "ALL":
                rows = self.db.get_l2_escrows(status=status)
            else:
                rows = self.db.get_l2_escrows()
        except Exception as e:
            log.warning(f"list_by_creator: {e}")
            return []
        return [r for r in rows if r.get("creator") == creator]

    def get(self, escrow_id: str) -> EscrowBTCtoBRN | None:
        try:
            d = self.db.get_l2_escrow_by_id(escrow_id)
            if not d:
                return None
            return EscrowBTCtoBRN.from_dict(d)
        except Exception as e:
            log.warning(f"get({escrow_id}): {e}")
            return None

    # ============================================================
    # Mudanças de estado
    # ============================================================
    def cancel(self, escrow_id: str) -> dict:
        """Cancela um escrow em estado OPEN."""
        e = self.get(escrow_id)
        if not e:
            return {"ok": False, "msg": "escrow nao existe"}
        if not e.can_cancel():
            return {"ok": False,
                    "msg": f"nao pode cancelar em estado {e.status}"}
        try:
            self.db.update_l2_escrow_status(escrow_id, STATUS_CANCELLED)
            return {"ok": True, "escrow_id": escrow_id,
                    "status": STATUS_CANCELLED}
        except Exception as err:
            return {"ok": False, "msg": str(err)}

    def expire_overdue(self, limit: int = 500) -> int:
        """
        Marca como EXPIRED os escrows OPEN vencidos.
        Retorna quantos foram atualizados.
        """
        try:
            rows = self.db.get_l2_escrows(status=STATUS_OPEN)
        except Exception:
            return 0

        now = int(time.time())
        n = 0
        for r in rows[:limit]:
            try:
                if int(r.get("expires_at", 0)) < now:
                    self.db.update_l2_escrow_status(
                        r["escrow_id"], STATUS_EXPIRED)
                    n += 1
            except Exception:
                continue
        if n:
            log.info(f"[FACTORY] {n} escrow(s) expirado(s)")
        return n

    # ============================================================
    # Estatísticas
    # ============================================================
    def stats(self) -> dict:
        out = {
            "open":         0,
            "btc_detected": 0,
            "released":     0,
            "expired":      0,
            "cancelled":    0,
            "total":        0,
        }
        try:
            for st in VALID_STATUSES:
                rows = self.db.get_l2_escrows(status=st)
                n = len(rows)
                out[st.lower() if st != "BTC_DETECTED" else "btc_detected"] = n
                out["total"] += n
        except Exception as e:
            log.warning(f"stats: {e}")
        out["rate"] = _rate()
        out["fee_pct"] = _fee_pct()
        out["btc_address"] = _btc_address()
        return out


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import sys
    import os

    # mock mínimo de DB
    class FakeDB:
        def __init__(self):
            self.rows = {}
        def save_l2_escrow(self, d):
            self.rows[d["escrow_id"]] = d
        def get_l2_escrow_by_id(self, eid):
            return self.rows.get(eid)
        def get_l2_escrows(self, status=None):
            vals = list(self.rows.values())
            if status:
                return [r for r in vals if r.get("status") == status]
            return vals
        def update_l2_escrow_status(self, eid, status):
            if eid in self.rows:
                self.rows[eid]["status"] = status

    print("Testando ContractFactory...")
    db = FakeDB()
    f = ContractFactory(db)

    # 1. Cotação
    q = compute_brn_amounts(100_000)  # 0.001 BTC
    print(f"  [info] 0.001 BTC → gross={q['brn_gross']} "
          f"fee={q['fee']} net={q['brn_net']} (rate={q['rate']})")
    assert q["brn_gross"] > q["brn_net"]
    assert q["fee"] > 0

    # 2. Cria escrow
    buyer = "brn1qtestaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    e = f.create_btc_to_brn("BRN_TREASURY", 100_000, buyer)
    assert e.status == "OPEN"
    assert e.buyer == buyer
    assert db.get_l2_escrow_by_id(e.escrow_id) is not None
    print(f"  [ok] create_btc_to_brn: {e.escrow_id}")

    # 3. Lista
    lista = f.list_by_buyer(buyer, status="OPEN")
    assert len(lista) == 1
    print(f"  [ok] list_by_buyer: {len(lista)} escrow(s)")

    # 4. Validações
    for bad in [
        ("BRN_TREASURY", 100_000, "not_brn"),
        ("BRN_TREASURY", -1, buyer),
        ("BRN_TREASURY", 0, buyer),
    ]:
        try:
            f.create_btc_to_brn(*bad)
            raise AssertionError(f"deveria rejeitar {bad}")
        except ValueError:
            pass
    print("  [ok] validações de entrada")

    # 5. Cancelamento
    r = f.cancel(e.escrow_id)
    assert r["ok"]
    assert db.get_l2_escrow_by_id(e.escrow_id)["status"] == "CANCELLED"
    print("  [ok] cancel")

    # 6. Estatísticas
    s = f.stats()
    print(f"  [info] stats: {s}")
    assert s["cancelled"] == 1

    # 7. Expiração
    e2 = f.create_btc_to_brn("BRN_TREASURY", 100_000, buyer, ttl_s=-1)
    n = f.expire_overdue()
    assert n >= 1
    assert db.get_l2_escrow_by_id(e2.escrow_id)["status"] == "EXPIRED"
    print(f"  [ok] expire_overdue: {n} expirado(s)")

    print("ContractFactory OK.")