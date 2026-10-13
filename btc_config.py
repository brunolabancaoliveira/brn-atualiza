# btc_config.py - Configuração do seu endereço BTC e cotação (v2)
# ============================================================
# v2:
#   [NEW] MIN_BTC_SATS / MAX_BTC_SATS (usados pelo l2_manager.py)
#   [NEW] BTC_MIN_CONFIRMATIONS já existia
#   [NEW] Validação de BTC_RECEIVE_ADDRESS placeholder
#   [NEW] Helper is_configured()
# ============================================================

# ============================================================
# ENDEREÇO BTC (ONDE VOCÊ RECEBE)
# ============================================================
# ATENÇÃO: substitua pelo SEU endereço real antes de ativar o L2
# Mainnet: bc1q...
# Testnet: tb1q...
BTC_RECEIVE_ADDRESS = "bc1qSEU_ENDERECO_AQUI_TROQUE_ISSO"

# "mainnet" ou "testnet"
BTC_NETWORK = "mainnet"

# Confirmações mínimas antes de liberar BRN
BTC_MIN_CONFIRMATIONS = 3

# ============================================================
# LIMITES DE VALOR (em satoshis)
# ============================================================
# Usados pelo l2_manager.py para validar ordens
#   MIN_BTC_SATS = 10_000 sats = 0.0001 BTC (~$6 a $100 dependendo do preço)
#   MAX_BTC_SATS = 1_000_000_000 sats = 10 BTC
MIN_BTC_SATS = 10_000
MAX_BTC_SATS = 1_000_000_000

# ============================================================
# COTAÇÃO / TAXA
# ============================================================
# 1 BTC = X BRN (fixo por enquanto)
BRN_PER_BTC = 10_000

# Taxa do onramp em %
FEE_PERCENT = 2.0

# ============================================================
# RPC PÚBLICOS (fallback)
# ============================================================
BTC_RPCS = [
    "https://blockstream.info/api",
    "https://mempool.space/api",
    "https://api.blockcypher.com/v1/btc/main",
]

# ============================================================
# VALIDADOR L2
# ============================================================
VALIDATOR_ID = "main_validator"

# ============================================================
# HELPERS
# ============================================================
def is_configured() -> bool:
    """True se o endereço BTC está preenchido (não é o placeholder)."""
    addr = (BTC_RECEIVE_ADDRESS or "").strip()
    if not addr:
        return False
    if "SEU_ENDERECO" in addr or "AQUI" in addr or "TROQUE" in addr:
        return False
    if BTC_NETWORK == "mainnet" and not addr.startswith("bc1"):
        return False
    if BTC_NETWORK == "testnet" and not addr.startswith("tb1"):
        return False
    return True
