# btc_config.py - Configuração do seu endereço BTC e cotação
# EDITE AQUI

# Seu endereço BTC onde vai receber (use testnet no início!)
# Mainnet: bc1q... / Testnet: tb1q...
BTC_RECEIVE_ADDRESS = "bc1qSEU_ENDERECO_AQUI_TROQUE_ISSO"
BTC_NETWORK = "mainnet"  # "mainnet" ou "testnet"

# Cotação fixa inicial: 1 BTC = X BRN
BRN_PER_BTC = 10000
FEE_PERCENT = 2.0

BTC_MIN_CONFIRMATIONS = 3

BTC_RPCS = [
    "https://blockstream.info/api",
    "https://mempool.space/api",
    "https://api.blockcypher.com/v1/btc/main"
]

VALIDATOR_ID = "main_validator"