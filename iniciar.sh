#!/usr/bin/env bash
# ============================================================
#   iniciar.sh - BRN Node CLIENTE (sincroniza da Maquina A) - v2
# ============================================================
#   Equivalente ao iniciar.bat do Windows.
#   100% ASCII, sem acentos.
#
#   v2:
#     [FIX] carrega brn_network.env ANTES do tailscale
#     [FIX] passa --no-tailscale pro main.py (evita setup 2x)
#     [FIX] checa TODOS os modulos obrigatorios
#     [FIX] parser de env remove aspas simples/duplas
#     [FIX] fingerprint via stdin (nao interpola a senha no shell)
#     [FIX] bootstrap_peers.json sem IP hardcoded
#     [NEW] --skip-tailscale / --gui / --no-gui
#     [NEW] mostra versao do main.py antes de subir
# ============================================================

set -o pipefail

# -------- Cores --------
if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
    C_BLU=$'\033[34m'; C_RST=$'\033[0m'; C_BLD=$'\033[1m'
else
    C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_RST=""; C_BLD=""
fi

info()  { printf '%s[*]%s %s\n' "$C_BLU" "$C_RST" "$*"; }
ok()    { printf '%s[ok]%s %s\n' "$C_GRN" "$C_RST" "$*"; }
warn()  { printf '%s[!]%s %s\n' "$C_YEL" "$C_RST" "$*"; }
err()   { printf '%s[X]%s %s\n' "$C_RED" "$C_RST" "$*" >&2; }

# -------- Argumentos --------
SKIP_TAILSCALE=0
FORCE_GUI=0
FORCE_NO_GUI=0
for arg in "$@"; do
    case "$arg" in
        --skip-tailscale) SKIP_TAILSCALE=1 ;;
        --gui)            FORCE_GUI=1 ;;
        --no-gui)         FORCE_NO_GUI=1 ;;
        -h|--help)
            echo "Uso: $0 [--skip-tailscale] [--gui] [--no-gui]"
            exit 0
            ;;
    esac
done

# -------- Diretorio do script --------
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "$SCRIPT_PATH")"
cd "$SCRIPT_DIR" || { err "Nao consegui cd para $SCRIPT_DIR"; exit 1; }

echo "============================================================"
echo "  BRN Node - CLIENTE (sincroniza da Maquina A) - v2"
echo "============================================================"
echo

# ============================================================
#  0) PYTHON
# ============================================================
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    err "Python 3 nao encontrado"
    echo "     Instale: sudo apt install -y python3 python3-venv python3-pip"
    exit 1
fi

PYVER=$($PY -c "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>/dev/null)
ok "Python $PYVER ($(command -v $PY))"
echo

# ============================================================
#  1) CARREGA brn_network.env  (ANTES do tailscale)
# ============================================================
if [ ! -f "brn_network.env" ]; then
    err "brn_network.env NAO existe"
    echo "     Copie da Maquina A antes de continuar."
    exit 1
fi

info "Carregando brn_network.env..."

# Exporta variaveis. Remove CR, espaços em volta, aspas simples/duplas.
set -a
while IFS='=' read -r k v || [ -n "$k" ]; do
    # Normaliza
    k="$(printf '%s' "$k" | tr -d '\r\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    v="$(printf '%s' "$v" | tr -d '\r\n')"

    # Ignora comentarios e linhas vazias
    [ -z "$k" ] && continue
    case "$k" in \#*) continue ;; esac
    case "$k" in *[!A-Za-z0-9_]*) continue ;; esac  # nome invalido

    # Strip de aspas (uma camada)
    case "$v" in
        \"*\") v="${v#\"}"; v="${v%\"}" ;;
        \'*\') v="${v#\'}"; v="${v%\'}" ;;
    esac

    export "$k=$v"
done < <(sed 's/\r$//' brn_network.env)
set +a

ok "brn_network.env carregado"
echo

# ============================================================
#  2) SENHAS LOCAIS (defaults se env nao definiu)
# ============================================================
: "${BRN_NODE_PASSWORD:=senha-no-local-2026}"
: "${BRN_WEB_PASS:=senha-carteira-2026}"
: "${BRN_WALLET_SESSION_PASS:=$BRN_WEB_PASS}"
export BRN_NODE_PASSWORD
export BRN_WEB_PASS
export BRN_WALLET_SESSION_PASS

# ============================================================
#  3) PARAMETROS OPERACIONAIS
# ============================================================
# --- Lidos pelo main.py / miner_loop / network_guard ---
export BRN_MINER_AUTO="${BRN_MINER_AUTO:-0}"
export BRN_MINER_INTERVAL="${BRN_MINER_INTERVAL:-30}"
export BRN_ALLOW_SOLO_MINING="${BRN_ALLOW_SOLO_MINING:-0}"
export BRN_MIN_PEER_STABLE="${BRN_MIN_PEER_STABLE:-1}"
export BRN_UPNP="${BRN_UPNP:-1}"
export BRN_P2P_AUTH="${BRN_P2P_AUTH:-optional}"
export BRN_WEB_PORT="${BRN_WEB_PORT:-5000}"
export BRN_EXPLORER_PORT="${BRN_EXPLORER_PORT:-8080}"
export BRN_P2P_PORT="${BRN_P2P_PORT:-6001}"
export BRN_LOG_LEVEL="${BRN_LOG_LEVEL:-INFO}"
export PYTHONUNBUFFERED=1

# --- Reservadas (nao lidas por nenhum modulo revisado ainda) ---
# Descomente se voce tiver codigo que leia:
# export BRN_NODE_AUTORESET=1
# export BRN_SYNC_BATCH=500
# export BRN_SYNC_PARALELO_MIN=500
# export BRN_SYNC_PARALELO_WORKERS=4
# export BRN_SYNC_RETRY_MAX=5
# export BRN_TCP_TIMEOUT=30.0

# ============================================================
#  4) ARQUIVOS ESSENCIAIS (mesma lista do main.py)
# ============================================================
FALTA=""
for f in \
    main.py server.py explorer.py \
    blockchain.py db.py crypto.py wallet.py \
    chain_validator.py checkpoints.py \
    brn_config.py brn_logger.py version.py \
    security.py secure_store.py \
    p2p_unified.py p2p_auth.py p2p_secure.py p2p_cipher.py p2p_relay.py \
    discovery_v2.py rendezvous_discovery.py network_guard.py \
    miner_loop.py update_peers.py bech32.py
do
    [ -f "$f" ] || FALTA="$FALTA $f"
done
if [ -n "$FALTA" ]; then
    err "Arquivos faltando:$FALTA"
    exit 1
fi
ok "Arquivos essenciais OK"
echo

# ============================================================
#  5) PAPEL: CLIENTE
# ============================================================
printf 'client\n' > brn_role.txt
ok "Papel: cliente (nao origina genesis)"
echo

# ============================================================
#  6) TAILSCALE (opcional - DEPOIS do env)
# ============================================================
TAILSCALE_FLAG="--no-tailscale"

if [ "$SKIP_TAILSCALE" -eq 1 ]; then
    info "Tailscale: pulado (--skip-tailscale)"
elif [ ! -f "brn_tailscale_setup.py" ]; then
    info "brn_tailscale_setup.py nao encontrado. Pulando."
elif [ -z "${TS_AUTH_KEY:-}" ]; then
    info "TS_AUTH_KEY vazia. Pulando Tailscale."
else
    info "Configurando Tailscale..."
    if $PY brn_tailscale_setup.py --auto 2>/dev/null; then
        ok "Tailscale OK"
    else
        warn "Tailscale nao esta pronto."
        echo "         Para configurar agora: $PY brn_tailscale_setup.py"
        echo "         Continuando sem Tailscale..."
    fi
    # Se voce configurou aqui, nao precisa rodar de novo no main.py
    TAILSCALE_FLAG="--no-tailscale"
fi
echo

# ============================================================
#  7) BOOTSTRAP PEERS
# ============================================================
# Fonte de verdade: BRN_BOOTSTRAP_PEERS (env)
if [ -n "${BRN_BOOTSTRAP_PEERS:-}" ]; then
    ok "BRN_BOOTSTRAP_PEERS = $BRN_BOOTSTRAP_PEERS"
else
    warn "BRN_BOOTSTRAP_PEERS nao definido em brn_network.env"
fi

# bootstrap_peers.json so e criado se voce usar esse formato
if [ -n "${BRN_BOOTSTRAP_FILE:-}" ] && [ ! -f "$BRN_BOOTSTRAP_FILE" ]; then
    echo '[]' > "$BRN_BOOTSTRAP_FILE"
    ok "$BRN_BOOTSTRAP_FILE criado (vazio)"
fi
echo

# ============================================================
#  8) FINGERPRINT DO SEGREDO (via stdin — sem interpolar no shell)
# ============================================================
if [ -n "${BRN_NETWORK_SECRET:-}" ]; then
    SEC_FP="$(printf '%s' "$BRN_NETWORK_SECRET" | $PY -c 'import hashlib,sys;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:16])' 2>/dev/null)"
else
    SEC_FP="(vazio)"
fi

# ============================================================
#  9) PYWEBVIEW (GUI ou headless)
# ============================================================
HEADLESS_FLAG=""

if [ "$FORCE_NO_GUI" -eq 1 ]; then
    HEADLESS_FLAG="--headless"
    info "GUI desabilitada por --no-gui"
elif [ "$FORCE_GUI" -eq 1 ]; then
    ok "GUI forcada por --gui"
else
    if ! $PY -c "import webview" >/dev/null 2>&1; then
        HEADLESS_FLAG="--headless"
        warn "pywebview ausente - modo HEADLESS"
        echo "         Para instalar GUI: $PY -m pip install pywebview"
    elif ! $PY -c "import gi; gi.require_version('Gtk','3.0')" >/dev/null 2>&1; then
        HEADLESS_FLAG="--headless"
        warn "pywebview instalado mas faltam libs GTK - modo HEADLESS"
        echo "         Para instalar: sudo apt install -y python3-gi gir1.2-webkit2-4.0"
    elif [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then
        HEADLESS_FLAG="--headless"
        warn "Sem display grafico - modo HEADLESS"
    else
        ok "pywebview OK - carteira desktop vai abrir"
    fi
fi
echo

# ============================================================
# 10) VERSAO DO main.py
# ============================================================
NODE_VERSION="$($PY main.py --version 2>/dev/null || echo '?')"
ok "main.py versao: $NODE_VERSION"
echo

# ============================================================
# 11) RESUMO
# ============================================================
echo "============================================================"
echo "  CONFIGURACAO ATUAL (CLIENTE)"
echo "============================================================"
echo "  Modo       : CLIENTE (sincroniza da rede)"
echo "  Versao     : $NODE_VERSION"
echo "  Auth FP    : $SEC_FP...  <- deve ser IGUAL ao da Maquina A"
echo "  Tracker    : ${BRN_TRACKER:-(nao definido)}"
echo "  Bootstrap  : ${BRN_BOOTSTRAP_PEERS:-(nao definido)}"
echo "  Mineracao  : $BRN_MINER_AUTO (pode ativar na carteira)"
echo "  P2P porta  : $BRN_P2P_PORT"
echo "  HTTP       : http://127.0.0.1:$BRN_WEB_PORT"
echo "  Explorer   : http://127.0.0.1:$BRN_EXPLORER_PORT"
echo "  Python     : $PYVER"
echo "  pywebview  : $([ -z "$HEADLESS_FLAG" ] && echo GUI || echo HEADLESS)"
echo "  Tailscale  : $([ "$TAILSCALE_FLAG" = "--no-tailscale" ] && echo 'ja feito / pulado' || echo 'sera feito pelo main.py')"
echo "============================================================"
echo
echo "  ATENCAO: deixe este terminal ABERTO enquanto o cliente roda."
echo

# ============================================================
# 12) INICIA O NO EM MODO CLIENTE
# ============================================================
info "Iniciando CLIENTE BRN... (Ctrl+C para encerrar)"
echo

cleanup() {
    echo
    warn "Encerrando no..."
    exit 130
}
trap cleanup INT TERM

# shellcheck disable=SC2086
$PY main.py --client-mode $TAILSCALE_FLAG $HEADLESS_FLAG
EXITCODE=$?

echo
if [ "$EXITCODE" -ne 0 ]; then
    err "O no saiu com codigo $EXITCODE"
    echo
    echo "  Se o erro foi 'Falha ao decifrar node_identity.enc':"
    echo "    rm node_identity.enc && ./iniciar.sh"
    echo
    echo "  Se o erro foi 'TIMEOUT esperando genesis':"
    echo "    - Verifique se a Maquina A esta rodando"
    echo "    - Verifique se as duas estao na mesma rede/Tailscale"
    echo "    - Verifique se os segredos batem: cat brn_network.env"
    echo
    echo "  Se o erro foi 'Modulos obrigatorios faltando':"
    echo "    Rode: python3 verify.py"
    echo
else
    ok "No encerrado normalmente."
fi

# Se chamado por clique duplo, espera antes de fechar
if [ -t 0 ]; then
    read -r -p "Pressione ENTER para fechar..." _
fi

exit "$EXITCODE"