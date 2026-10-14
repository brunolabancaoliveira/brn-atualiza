"""contracts/__init__.py — Pacote de contratos do BRN
================================================================
Reexporta a API pública usada por blockchain.py v9.1.7 e server.py v9.5:

    from contracts import ContractVM
    from contracts import deploy, call

E mantém os tipos usados pelo L2:

    from contracts.factory import ContractFactory
    from contracts.escrow_btc_brn import EscrowBTCtoBRN

Estrutura:
    contracts/
    ├── __init__.py          ← este arquivo
    ├── vm.py                ← ContractVM, deploy(), call()
    ├── escrow_btc_brn.py    ← EscrowBTCtoBRN (L2)
    └── factory.py           ← ContractFactory (L2)
================================================================
"""
from __future__ import annotations

# --- VM de contratos (usado pelo blockchain v9.1.7) ---
from .vm import ContractVM, deploy, call

# --- L2 (BTC->BRN) ---
from .escrow_btc_brn import EscrowBTCtoBRN
from .factory import ContractFactory


__all__ = [
    "ContractVM",
    "deploy",
    "call",
    "EscrowBTCtoBRN",
    "ContractFactory",
]


# ============================================================
# INFO — só para debug
# ============================================================
def info() -> dict:
    return {
        "version": "1.0",
        "modules": ["vm", "escrow_btc_brn", "factory"],
        "exports": list(__all__),
    }