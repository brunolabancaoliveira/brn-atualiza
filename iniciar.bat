@echo off
REM ============================================================
REM   iniciar_completo.bat - BRN Node CLIENTE (Windows) - v13
REM ============================================================
REM   v13:
REM     [NEW] Deteccao AUTOMATICA do hostname (sem hardcode)
REM     [NEW] Atualiza brn_network.env, bootstrap_peers.json e
REM           brn_tailscale_peer.txt automaticamente
REM     [NEW] Comando tailscale up com hostname detectado
REM     [NEW] Auto-detecta peer via tailscale status
REM     [NEW] Se nao estiver logado, orienta login manual
REM     [FIX] Usa TS_AUTH_KEY do brn_network.env (nao hardcoded)
REM ============================================================

setlocal EnableDelayedExpansion

cd /d "%~dp0"
if errorlevel 1 (
    echo [X] Nao consegui cd para %~dp0
    pause
    exit /b 1
)

REM --- Detecta hostname da maquina automaticamente ---
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
echo Uso: iniciar_completo.bat [--skip-tailscale] [--gui] [--no-gui]
exit /b 0

:args_done

echo ============================================================
echo   BRN Node - CLIENTE - v13
echo   Hostname: %TS_HOSTNAME%
echo ============================================================
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
    echo     Instale: https://www.python.org/downloads/
    echo     Marque "Add Python to PATH"
    pause
    exit /b 1
)

set "PYVER=?"
for /f "tokens=*" %%V in ('%PY% -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "PYVER=%%V"
echo [ok] Python %PYVER% ^(%PY%^)
echo.

REM ============================================================
REM  2) CARREGA brn_network.env
REM ============================================================
if not exist "brn_network.env" (
    echo [X] brn_network.env nao encontrado!
    echo     Coloque o arquivo na mesma pasta deste .bat.
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

if "%TS_AUTH_KEY%"=="" (
    echo [!] TS_AUTH_KEY vazia em brn_network.env
) else (
    echo [ok] TS_AUTH_KEY carregada ^(prefixo: !TS_AUTH_KEY:~0,20!...^)
)

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
    l2_manager.py btc_watcher.py btc_config.py
    contracts\__init__.py contracts\vm.py contracts\escrow_btc_brn.py contracts\factory.py
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
set "TS_PEER_IP=N/A"

if "%SKIP_TAILSCALE%"=="1" (
    echo [*] Tailscale: pulado ^(--skip-tailscale^)
    goto tailscale_done
)

REM ---- 5.1: Tailscale instalado? ----
where tailscale >nul 2>&1
if errorlevel 1 (
    echo [!] Tailscale NAO instalado. Tentando instalar via winget...
    where winget >nul 2>&1
    if errorlevel 1 (
        echo [X] winget indisponivel. Instale manualmente:
        echo     https://tailscale.com/download/windows
        set "SKIP_TAILSCALE=1"
        goto tailscale_done
    )
    winget install --id Tailscale.Tailscale -e --silent ^
        --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo [X] Instalacao falhou.
        set "SKIP_TAILSCALE=1"
        goto tailscale_done
    )
    echo [ok] Tailscale instalado.
    set "PATH=%PATH%;C:\Program Files\Tailscale"
    timeout /t 5 /nobreak >nul
) else (
    echo [ok] Tailscale instalado
)

REM ---- 5.2: Servico Windows ----
sc query Tailscale >nul 2>&1
if not errorlevel 1 (
    sc query Tailscale | findstr /C:"RUNNING" >nul
    if errorlevel 1 (
        echo [*] Servico Tailscale parado - arrancando...
        net start Tailscale >nul 2>&1
        timeout /t 3 /nobreak >nul
    )
)

REM ---- 5.3: Reset se estado corrompido ----
set "NEED_RESET=0"
for /f "delims=" %%S in ('tailscale status 2^>^&1 ^| findstr /C:"NoState"') do set "NEED_RESET=1"
for /f "delims=" %%S in ('tailscale status 2^>^&1 ^| findstr /C:"starting"') do set "NEED_RESET=1"

if "%NEED_RESET%"=="1" (
    echo [!] Tailscale com estado corrompido - reset...
    net stop Tailscale >nul 2>&1
    taskkill /F /IM tailscaled.exe >nul 2>&1
    taskkill /F /IM tailscale-ipn.exe >nul 2>&1
    if exist "C:\ProgramData\Tailscale" (
        if exist "C:\ProgramData\Tailscale.bak" rmdir /s /q "C:\ProgramData\Tailscale.bak" >nul 2>&1
        ren "C:\ProgramData\Tailscale" "Tailscale.bak" >nul 2>&1
    )
    net start Tailscale >nul 2>&1
    timeout /t 5 /nobreak >nul
)

REM ---- 5.4: Verifica se esta logado ----
tailscale status >nul 2>&1
if errorlevel 1 (
    echo [!] Tailscale NAO esta logado.
    echo.

    if not "%TS_AUTH_KEY%"=="" (
        echo [*] Tentando autenticar com TS_AUTH_KEY do brn_network.env...
        tailscale up ^
            --auth-key="%TS_AUTH_KEY%" ^
            --accept-dns=false ^
            --hostname=%TS_HOSTNAME% ^
            --unattended ^
            --accept-routes ^
            --reset
        if errorlevel 1 (
            echo [!] Falhou com a chave - caindo para login manual...
            goto tailscale_manual_login
        )
        echo [ok] Autenticado com sucesso via chave.
        goto tailscale_logged
    )

    :tailscale_manual_login
    echo.
    echo ============================================================
    echo   LOGIN MANUAL NO TAILSCALE
    echo ============================================================
    echo.
    echo   Estamos rodando:
    echo     tailscale up --accept-dns=false --hostname=%TS_HOSTNAME% --unattended
    echo.
    echo   Um link de autenticacao vai aparecer abaixo.
    echo   Copie ele e abra no navegador.
    echo   Faca login com a MESMA conta do outro PC.
    echo.

    tailscale up ^
        --accept-dns=false ^
        --hostname=%TS_HOSTNAME% ^
        --unattended ^
        --accept-routes

    if errorlevel 1 (
        echo.
        echo [X] Ainda nao logado. Continuando sem Tailscale...
        set "SKIP_TAILSCALE=1"
        goto tailscale_done
    )
    echo [ok] Login concluido.
) else (
    echo [ok] Tailscale ja estava logado
    echo [*] Reconfigurando com hostname %TS_HOSTNAME%...
    tailscale up ^
        --accept-dns=false ^
        --hostname=%TS_HOSTNAME% ^
        --unattended ^
        --accept-routes >nul 2>&1
    if errorlevel 1 (
        echo [!] Reconfiguracao falhou - estado atual mantido.
    ) else (
        echo [ok] Hostname configurado: %TS_HOSTNAME%
    )
)

:tailscale_logged
timeout /t 3 /nobreak >nul

REM ---- 5.5: Deteta IP ----
set "MY_TS_IP="
for /f "tokens=*" %%I in ('tailscale ip -4 2^>nul') do (
    if "!MY_TS_IP!"=="" set "MY_TS_IP=%%I"
)
if "%MY_TS_IP%"=="" (
    echo [!] Tailscale sem IP - continuando sem.
    set "SKIP_TAILSCALE=1"
    set "MY_TS_IP=N/A"
    goto tailscale_done
)
echo [ok] Meu IP Tailscale: %MY_TS_IP%

REM ============================================================
REM  6) AUTO-DETECTA PEER
REM ============================================================
set "TS_PEER_IP="

REM --- 6.1: brn_tailscale_peer.txt ---
if exist "brn_tailscale_peer.txt" (
    for /f "usebackq tokens=* eol=#" %%L in ("brn_tailscale_peer.txt") do (
        set "LINE=%%L"
        if not "!LINE!"=="" (
            for /f "tokens=1 delims=:" %%I in ("!LINE!") do (
                if "!TS_PEER_IP!"=="" set "TS_PEER_IP=%%I"
            )
        )
    )
)

REM --- 6.2: BRN_BOOTSTRAP_PEERS ---
if "%TS_PEER_IP%"=="" (
    if not "%BRN_BOOTSTRAP_PEERS%"=="" (
        for /f "tokens=1 delims=,:" %%I in ("%BRN_BOOTSTRAP_PEERS%") do set "TS_PEER_IP=%%I"
    )
)

REM --- 6.3: tailscale status ---
if "%TS_PEER_IP%"=="" (
    echo [*] Auto-detectando peer via Tailscale...
    for /f "tokens=1" %%I in ('tailscale status 2^>nul ^| findstr /R "^100\."') do (
        if not "%%I"=="%MY_TS_IP%" (
            if "!TS_PEER_IP!"=="" set "TS_PEER_IP=%%I"
        )
    )
)

REM --- 6.4: Resultado ---
if "%TS_PEER_IP%"=="" (
    echo [!] Nenhum peer encontrado. Continuando sem.
    set "TS_PEER_IP=N/A"
) else (
    echo [ok] Peer detectado: %TS_PEER_IP%
    set "BRN_BOOTSTRAP_PEERS=%TS_PEER_IP%:6001"

    (
        echo %TS_PEER_IP%:6001
    ) > brn_tailscale_peer.txt
    echo [ok] brn_tailscale_peer.txt atualizado

    echo [*] Testando peer %TS_PEER_IP%...
    tailscale ping -c 1 "%TS_PEER_IP%" >nul 2>&1
    if errorlevel 1 (
        echo [!] Peer NAO respondeu ao ping.
    ) else (
        echo [ok] Peer alcancavel
    )
)

:tailscale_done
echo.

REM ============================================================
REM  7) WAITRESS
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
REM  8) PAPEL: CLIENTE
REM ============================================================
> "brn_role.txt" echo client
echo [ok] Papel: cliente
echo.

REM ============================================================
REM  9) PYWEBVIEW
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
REM 10) RESUMO E INICIO
REM ============================================================
set "ADMIN_STATUS=NAO"
if not "%BRN_ADMIN_TOKEN%"=="" set "ADMIN_STATUS=SIM"

set "NODE_VERSION=?"
for /f "tokens=*" %%V in ('%PY% main.py --version 2^>nul') do set "NODE_VERSION=%%V"

echo ============================================================
echo   CONFIGURACAO FINAL (CLIENTE)
echo ============================================================
echo   Hostname   : %TS_HOSTNAME%
echo   Meu IP TS  : %MY_TS_IP%
echo   Peer TS    : %TS_PEER_IP%
echo   Bootstrap  : %BRN_BOOTSTRAP_PEERS%
echo   P2P porta  : %BRN_P2P_PORT%
echo   HTTP       : http://0.0.0.0:%BRN_WEB_PORT%
echo   Explorer   : http://0.0.0.0:%BRN_EXPLORER_PORT%
echo   Python     : %PYVER%
echo   pywebview  : %GUI_STATUS%
echo   Versao     : %NODE_VERSION%
echo ============================================================
echo.

echo [*] Iniciando CLIENTE BRN... ^(Ctrl+C para encerrar^)
echo.
%PY% main.py --client-mode %HEADLESS_FLAG%
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo [X] O no saiu com codigo %EXITCODE%
    echo.
    echo   Se o erro foi 'TIMEOUT esperando genesis':
    echo     - Maquina A ligada e minerando?
    echo     - tailscale ping %TS_PEER_IP%
    echo     - Segredos batem? type brn_network.env ^| findstr BRN_NETWORK_SECRET
    echo.
    echo   Se o erro foi 'Falha ao decifrar node_identity.enc':
    echo     del node_identity.enc
    echo     iniciar_completo.bat
) else (
    echo [ok] No encerrado normalmente.
)

echo.
pause
exit /b %EXITCODE%
