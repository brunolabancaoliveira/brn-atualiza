@echo off
REM ============================================================
REM   iniciar22.bat - BRN Node CLIENTE (Windows) - v16
REM ============================================================
REM   v16:
REM     [FIX] IP do peer corrigido para 100.111.253.8
REM     [NEW] Detecta automaticamente o peer Linux via Tailscale
REM     [NEW] Atualiza todos os arquivos de config com o IP correto
REM   v15:
REM     [NEW] Deteccao automatica do hostname
REM ============================================================

setlocal EnableDelayedExpansion

cd /d "%~dp0"
if errorlevel 1 (
    echo [X] Nao consegui cd para %~dp0
    pause
    exit /b 1
)

REM --- IP do peer Ubuntu (fallback) ---
set "FIXED_PEER_IP=100.111.253.8"
set "FIXED_PEER_PORT=6001"

REM --- Detecta hostname ---
for /f "tokens=*" %%H in ('hostname') do set "TS_HOSTNAME=%%H"
if "%TS_HOSTNAME%"=="" set "TS_HOSTNAME=%COMPUTERNAME%"
if "%TS_HOSTNAME%"=="" set "TS_HOSTNAME=brn-node"

set "SKIP_TAILSCALE=0"
set "FORCE_GUI=0"
set "FORCE_NO_GUI=0"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--skip-tailscale" set "SKIP_TAILSCALE=1"
if /i "%~1"=="--gui"            set "FORCE_GUI=1"
if /i "%~1"=="--no-gui"         set "FORCE_NO_GUI=1"
if /i "%~1"=="-h"               goto show_help
if /i "%~1"=="--help"           goto show_help
shift
goto parse_args

:show_help
echo Uso: iniciar22.bat [--skip-tailscale] [--gui] [--no-gui]
exit /b 0

:args_done

echo ============================================================
echo   BRN Node - CLIENTE - v16
echo   Hostname  : %TS_HOSTNAME%
echo   Peer padrao: %FIXED_PEER_IP%:%FIXED_PEER_PORT%
echo ============================================================
echo.

REM ============================================================
REM  0) LIMPA ARQUIVOS DE PEER ANTIGOS
REM ============================================================
echo [*] Preparando arquivos de peer...

echo %FIXED_PEER_IP%:%FIXED_PEER_PORT%> brn_tailscale_peer.txt
echo [ok] brn_tailscale_peer.txt: %FIXED_PEER_IP%:%FIXED_PEER_PORT%

(
    echo [
    echo   "%FIXED_PEER_IP%:%FIXED_PEER_PORT%"
    echo ]
) > bootstrap_peers.json
echo [ok] bootstrap_peers.json: ["%FIXED_PEER_IP%:%FIXED_PEER_PORT%"]
echo.

REM ============================================================
REM  1) PYTHON
REM ============================================================
set "PY="
where python >nul 2>&1
if not errorlevel 1 set "PY=python"
if "%PY%"=="" (
    where py >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if "%PY%"=="" (
    echo [X] Python 3 nao encontrado
    pause
    exit /b 1
)

set "PYVER=?"
for /f "tokens=*" %%V in ('%PY% -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "PYVER=%%V"
echo [ok] Python %PYVER%
echo.

REM ============================================================
REM  2) CARREGA brn_network.env
REM ============================================================
if not exist "brn_network.env" (
    echo [X] brn_network.env nao encontrado!
    pause
    exit /b 1
)

echo [*] Carregando brn_network.env...
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("brn_network.env") do (
    set "K=%%A"
    set "V=%%B"
    if not "!K!"=="" (
        if "!V:~0,1!"=="""" set "V=!V:~1!"
        if "!V:~-1!"=="""" set "V=!V:~0,-1!"
        if "!V:~0,1!"=="'" set "V=!V:~1!"
        if "!V:~-1!"=="'" set "V=!V:~0,-1!"
        for /f "tokens=* delims= " %%X in ("!K!") do set "K=%%X"
        if not "!K!"=="" set "!K!=!V!"
    )
)
echo [ok] brn_network.env carregado
echo.

REM ============================================================
REM  3) DEFAULTS
REM ============================================================
if "%BRN_NODE_PASSWORD%"==""       set "BRN_NODE_PASSWORD=senha-no-temp-123"
if "%BRN_WEB_PASS%"==""            set "BRN_WEB_PASS=senha-temp-123"
if "%BRN_WALLET_SESSION_PASS%"=="" set "BRN_WALLET_SESSION_PASS=senha-forte-da-carteira-2026"
if "%BRN_WEB_PORT%"==""            set "BRN_WEB_PORT=5000"
if "%BRN_EXPLORER_PORT%"==""       set "BRN_EXPLORER_PORT=8080"
if "%BRN_P2P_PORT%"==""            set "BRN_P2P_PORT=6001"
if "%BRN_LOG_LEVEL%"==""           set "BRN_LOG_LEVEL=INFO"
set "PYTHONUNBUFFERED=1"

REM --- Forca o peer correto ---
set "BRN_BOOTSTRAP_PEERS=%FIXED_PEER_IP%:%FIXED_PEER_PORT%"

REM ============================================================
REM  4) ARQUIVOS ESSENCIAIS
REM ============================================================
set "FALTA="
for %%F in (
    main.py server.py explorer.py
    blockchain.py db.py crypto.py wallet.py bech32.py
    chain_validator.py checkpoints.py
    brn_config.py brn_logger.py version.py
    security.py secure_store.py
    p2p_unified.py p2p_auth.py p2p_secure.py p2p_cipher.py p2p_relay.py
    discovery_v2.py rendezvous_discovery.py rendezvous_mongo.py
    miner_loop.py sync_manager.py
) do (
    if not exist "%%F" set "FALTA=!FALTA! %%F"
)
if not "%FALTA%"=="" (
    echo [X] Arquivos faltando:%FALTA%
    pause
    exit /b 1
)
echo [ok] Arquivos essenciais OK
echo.

REM ============================================================
REM  5) TAILSCALE
REM ============================================================
set "MY_TS_IP=N/A"
set "TS_PEER_IP=%FIXED_PEER_IP%"

if "%SKIP_TAILSCALE%"=="1" (
    echo [*] Tailscale: pulado
    goto tailscale_done
)

REM ---- 5.1: Instalado? ----
where tailscale >nul 2>&1
if errorlevel 1 (
    echo [X] Tailscale nao instalado!
    pause
    exit /b 1
)
echo [ok] Tailscale instalado

REM ---- 5.2: Logado? ----
tailscale status >nul 2>&1
if errorlevel 1 (
    echo [!] Tailscale NAO esta logado.
    echo.
    echo ============================================================
    echo   LOGIN MANUAL NECESSARIO
    echo ============================================================
    echo.
    echo   Rode em OUTRO CMD:
    echo     tailscale up --accept-dns=false --hostname=%TS_HOSTNAME% --unattended
    echo.
    echo   Autentique no navegador e VOLTE AQUI.
    echo.
    pause
    tailscale status >nul 2>&1
    if errorlevel 1 (
        echo [X] Ainda nao logado. Abortando.
        pause
        exit /b 1
    )
)
echo [ok] Tailscale logado

REM ---- 5.3: Meu IP ----
set "MY_TS_IP="
for /f "tokens=*" %%I in ('tailscale ip -4 2^>nul') do (
    if "!MY_TS_IP!"=="" set "MY_TS_IP=%%I"
)
if "%MY_TS_IP%"=="" (
    echo [X] Nao consegui obter meu IP Tailscale.
    pause
    exit /b 1
)
echo [ok] Meu IP Tailscale: %MY_TS_IP%

REM ---- 5.4: Detecta peer Linux automaticamente ----
echo.
echo [*] Procurando peer Linux via Tailscale...

set "TS_PEER_IP="
set "TS_PEER_NAME="

for /f "tokens=1,2,3,4" %%A in ('tailscale status 2^>nul') do (
    if not "%%A"=="%MY_TS_IP%" (
        if /i "%%D"=="linux" (
            if "!TS_PEER_IP!"=="" (
                set "TS_PEER_IP=%%A"
                set "TS_PEER_NAME=%%B"
            )
        )
    )
)

if "%TS_PEER_IP%"=="" (
    echo [!] Nenhum peer Linux. Usando IP fixo: %FIXED_PEER_IP%
    set "TS_PEER_IP=%FIXED_PEER_IP%"
    set "TS_PEER_NAME=ubuntu-origin"
) else (
    echo [ok] Peer Linux detectado: %TS_PEER_IP% ^(%TS_PEER_NAME%^)
)

REM ---- 5.5: Testa o peer ----
echo [*] Testando peer %TS_PEER_IP%...
tailscale ping -c 1 "%TS_PEER_IP%" >nul 2>&1
if errorlevel 1 (
    echo [!] Peer NAO respondeu ao ping via Tailscale.
) else (
    echo [ok] Peer alcancavel via Tailscale
)

REM ---- 5.6: Atualiza arquivos com o IP final ----
echo.
echo [*] Atualizando arquivos de config com IP: %TS_PEER_IP%

echo %TS_PEER_IP%:%FIXED_PEER_PORT%> brn_tailscale_peer.txt
echo [ok] brn_tailscale_peer.txt atualizado

(
    echo [
    echo   "%TS_PEER_IP%:%FIXED_PEER_PORT%"
    echo ]
) > bootstrap_peers.json
echo [ok] bootstrap_peers.json atualizado

set "BRN_BOOTSTRAP_PEERS=%TS_PEER_IP%:%FIXED_PEER_PORT%"

:tailscale_done
echo.

REM ============================================================
REM  6) WAITRESS
REM ============================================================
%PY% -c "import waitress" >nul 2>&1
if errorlevel 1 (
    echo [!] Waitress ausente - HTTP usara Flask dev server
) else (
    for /f "tokens=*" %%V in ('%PY% -c "import waitress;print(waitress.__version__)" 2^>nul') do set "WV=%%V"
    echo [ok] Waitress v!WV!
)
echo.

REM ============================================================
REM  7) PAPEL: CLIENTE
REM ============================================================
> "brn_role.txt" echo client
echo [ok] Papel: cliente
echo.

REM ============================================================
REM  8) PYWEBVIEW
REM ============================================================
set "HEADLESS_FLAG="

if "%FORCE_NO_GUI%"=="1" (
    set "HEADLESS_FLAG=--headless"
    goto gui_done
)
if "%FORCE_GUI%"=="1" goto gui_done

%PY% -c "import webview" >nul 2>&1
if errorlevel 1 (
    set "HEADLESS_FLAG=--headless"
    echo [!] pywebview ausente - HEADLESS
    goto gui_done
)
echo [ok] pywebview OK

:gui_done
set "GUI_STATUS=GUI"
if "%HEADLESS_FLAG%"=="--headless" set "GUI_STATUS=HEADLESS"
echo.

REM ============================================================
REM  9) RESUMO
REM ============================================================
set "NODE_VERSION=?"
for /f "tokens=*" %%V in ('%PY% main.py --version 2^>nul') do set "NODE_VERSION=%%V"

echo ============================================================
echo   CONFIGURACAO FINAL (CLIENTE)
echo ============================================================
echo   Hostname   : %TS_HOSTNAME%
echo   Meu IP TS  : %MY_TS_IP%
echo   Peer TS    : %TS_PEER_IP%
echo   Peer nome  : %TS_PEER_NAME%
echo   Bootstrap  : %BRN_BOOTSTRAP_PEERS%
echo   P2P porta  : %BRN_P2P_PORT%
echo   HTTP       : http://0.0.0.0:%BRN_WEB_PORT%
echo   Explorer   : http://0.0.0.0:%BRN_EXPLORER_PORT%
echo   Python     : %PYVER%
echo   pywebview  : %GUI_STATUS%
echo   Versao     : %NODE_VERSION%
echo ============================================================
echo.

REM ============================================================
REM 10) INICIA O NO
REM ============================================================
echo [*] Iniciando CLIENTE BRN... ^(Ctrl+C para encerrar^)
echo.
%PY% main.py --client-mode %HEADLESS_FLAG%
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo [X] O no saiu com codigo %EXITCODE%
    echo.
    echo   Se o erro foi 'TIMEOUT esperando genesis':
    echo     - Ubuntu ligada e minerando?
    echo     - tailscale ping %TS_PEER_IP%
    echo     - Segredos batem?
    echo.
) else (
    echo [ok] No encerrado normalmente.
)

echo.
pause
exit /b %EXITCODE%