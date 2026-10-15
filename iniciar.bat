@echo off
REM ============================================================
REM   iniciar.bat - BRN Node CLIENTE (Windows) - v9
REM ============================================================
REM   v9:
REM     [NEW] tailscale up INCONDICIONAL (garante conexao ativa
REM           mesmo se ja logado mas desconectado)
REM   v8:
REM     [FIX] tailscale up com --auth-key= e flags completas
REM     [NEW] Detecta erro "NoState" e faz reset do estado local
REM     [NEW] Fallback: renomeia C:\ProgramData\Tailscale e reinicia
REM ============================================================

setlocal EnableDelayedExpansion

cd /d "%~dp0"
if errorlevel 1 (
    echo [X] Nao consegui cd para %~dp0
    pause
    exit /b 1
)

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
echo Uso: iniciar.bat [--skip-tailscale] [--gui] [--no-gui]
exit /b 0

:args_done

echo ============================================================
echo   BRN Node - CLIENTE (sincroniza da Maquina A) - v9
echo ============================================================
echo.

REM ============================================================
REM  0) PYTHON
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
echo [ok] Python %PYVER% ^(%PY%^)
echo.

REM ============================================================
REM  1) CARREGA brn_network.env
REM ============================================================
if not exist "brn_network.env" (
    echo [!] brn_network.env nao existe - criando com defaults...
    (
        echo BRN_NETWORK_SECRET=9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9
        echo BRN_NODE_PASSWORD=senha-no-temp-123
        echo BRN_WEB_PASS=senha-temp-123
        echo BRN_WALLET_SESSION_PASS=senha-forte-da-carteira-2026
        echo BRN_ADMIN_TOKEN=
        echo BRN_BOOTSTRAP_PEERS=
        echo BRN_IS_ORIGIN=0
        echo BRN_ALL_MINERS=0
        echo BRN_MIN_DIFFICULTY=6
        echo BRN_WEB_HOST=0.0.0.0
        echo BRN_WEB_PORT=5000
        echo BRN_EXPLORER_PORT=8080
        echo BRN_EXPLORER_HOST=0.0.0.0
        echo BRN_HTTP_THREADS=16
        echo BRN_P2P_PORT=6001
        echo BRN_P2P_AUTH=optional
        echo BRN_RATE_LIMIT=200
        echo BRN_UPNP=0
        echo BRN_SKIP_VERIFY=0
        echo BRN_VERIFY_STRICT=0
        echo BRN_LOG_LEVEL=INFO
        echo BRN_ALLOW_SOLO_MINING=1
        echo BRN_AUTO_MINE=0
        echo TS_AUTH_KEY=keBGtiE91d11CNTRL
    ) > brn_network.env
    echo [ok] brn_network.env criado
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
REM  2) DEFAULTS
REM ============================================================
if "%BRN_NODE_PASSWORD%"==""       set "BRN_NODE_PASSWORD=senha-no-temp-123"
if "%BRN_WEB_PASS%"==""            set "BRN_WEB_PASS=senha-temp-123"
if "%BRN_WALLET_SESSION_PASS%"=="" set "BRN_WALLET_SESSION_PASS=senha-forte-da-carteira-2026"
if "%BRN_WEB_PORT%"==""            set "BRN_WEB_PORT=5000"
if "%BRN_EXPLORER_PORT%"==""       set "BRN_EXPLORER_PORT=8080"
if "%BRN_P2P_PORT%"==""            set "BRN_P2P_PORT=6001"
if "%BRN_LOG_LEVEL%"==""           set "BRN_LOG_LEVEL=INFO"
set "PYTHONUNBUFFERED=1"

REM ============================================================
REM  3) ARQUIVOS ESSENCIAIS
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
REM  4) TAILSCALE - AUTO-INSTALL + AUTO-LOGIN + AUTO-RESET
REM ============================================================
set "MY_TS_IP=N/A"
set "TS_PEER_IP=N/A"

if "%SKIP_TAILSCALE%"=="1" (
    echo [*] Tailscale: pulado ^(--skip-tailscale^)
    goto tailscale_done
)

REM ---- 4.1: Tailscale instalado? ----
where tailscale >nul 2>&1
if errorlevel 1 (
    echo [!] Tailscale NAO instalado. Tentando instalar via winget...
    where winget >nul 2>&1
    if errorlevel 1 (
        echo [X] winget NAO disponivel. Instale manualmente.
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

REM ---- 4.2: Servico Windows ----
sc query Tailscale >nul 2>&1
if not errorlevel 1 (
    sc query Tailscale | findstr /C:"RUNNING" >nul
    if errorlevel 1 (
        echo [*] Servico Tailscale parado - arrancando...
        net start Tailscale >nul 2>&1
        timeout /t 3 /nobreak >nul
    )
)

REM ---- 4.3: Detecta NoState ----
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

REM ---- 4.4: Ja logado? Se nao, autentica ----
tailscale status >nul 2>&1
if errorlevel 1 (
    echo [!] Tailscale NAO esta logado.
    if "%TS_AUTH_KEY%"=="" (
        echo [X] TS_AUTH_KEY vazia em brn_network.env
        echo     Gere em: https://login.tailscale.com/admin/settings/keys
        set "SKIP_TAILSCALE=1"
        goto tailscale_done
    )
    echo [*] Autenticando com TS_AUTH_KEY...
    tailscale up --auth-key="%TS_AUTH_KEY%" --unattended --accept-dns=false --hostname="%COMPUTERNAME%" --accept-routes

    if errorlevel 1 (
        echo [!] Falhou - reset + retry...
        net stop Tailscale >nul 2>&1
        taskkill /F /IM tailscaled.exe >nul 2>&1
        if exist "C:\ProgramData\Tailscale" (
            if exist "C:\ProgramData\Tailscale.bak" rmdir /s /q "C:\ProgramData\Tailscale.bak" >nul 2>&1
            ren "C:\ProgramData\Tailscale" "Tailscale.bak" >nul 2>&1
        )
        net start Tailscale >nul 2>&1
        timeout /t 5 /nobreak >nul
        tailscale up --auth-key="%TS_AUTH_KEY%" --unattended --accept-dns=false --hostname="%COMPUTERNAME%" --accept-routes
        if errorlevel 1 (
            echo [X] Ainda falhou.
            set "SKIP_TAILSCALE=1"
            goto tailscale_done
        )
    )
    echo [ok] Tailscale autenticado.
    timeout /t 5 /nobreak >nul
)

REM ============================================================
REM  ★★★ 4.4b: GARANTE CONEXAO ATIVA (NOVO) ★★★
REM ============================================================
echo [*] Garantindo conexao Tailscale ativa...

if not "%TS_AUTH_KEY%"=="" (
    tailscale up --auth-key="%TS_AUTH_KEY%" --unattended --accept-dns=false --hostname="%COMPUTERNAME%" --accept-routes >nul 2>&1
) else (
    tailscale up --accept-dns=false --accept-routes >nul 2>&1
)

if errorlevel 1 (
    echo [!] tailscale up retornou erro - tentando sem flags...
    tailscale up >nul 2>&1
)

timeout /t 3 /nobreak >nul
echo [ok] Tailscale logado e conectado

REM ---- 4.5: Deteta IP ----
set "MY_TS_IP="
for /f "tokens=*" %%I in ('tailscale ip -4 2^>nul') do (
    if "!MY_TS_IP!"=="" set "MY_TS_IP=%%I"
)
if "%MY_TS_IP%"=="" (
    echo [!] Tailscale sem IP.
    set "SKIP_TAILSCALE=1"
    set "MY_TS_IP=N/A"
    goto tailscale_done
)
echo [ok] Meu IP Tailscale: %MY_TS_IP%

REM ---- 4.6: Auto-detecta peer ----
set "TS_PEER_IP="
if not "%BRN_BOOTSTRAP_PEERS%"=="" (
    for /f "tokens=1 delims=,:" %%I in ("%BRN_BOOTSTRAP_PEERS%") do set "TS_PEER_IP=%%I"
)

if "%TS_PEER_IP%"=="" (
    echo [*] Bootstrap vazio - auto-detectando peer...
    for /f "tokens=1" %%I in ('tailscale status 2^>nul ^| findstr /R "^100\."') do (
        if not "%%I"=="%MY_TS_IP%" (
            if "!TS_PEER_IP!"=="" set "TS_PEER_IP=%%I"
        )
    )
    if not "!TS_PEER_IP!"=="" (
        echo [ok] Peer detectado: !TS_PEER_IP!
        set "BRN_BOOTSTRAP_PEERS=!TS_PEER_IP!:6001"
    )
)

if "%TS_PEER_IP%"=="" (
    echo [!] Nenhum peer encontrado.
    set "TS_PEER_IP=N/A"
) else (
    echo [*] Testando peer %TS_PEER_IP%...
    tailscale ping -c 1 "%TS_PEER_IP%" >nul 2>&1
    if errorlevel 1 (
        echo [!] Peer NAO respondeu.
    ) else (
        echo [ok] Peer alcancavel
    )
)

:tailscale_done
echo.

REM ============================================================
REM  5) WAITRESS
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
REM  6) PAPEL: CLIENTE
REM ============================================================
> "brn_role.txt" echo client
echo [ok] Papel: cliente
echo.

REM ============================================================
REM  7) PYWEBVIEW
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
REM  8) RESUMO E INICIO
REM ============================================================
set "ADMIN_STATUS=NAO"
if not "%BRN_ADMIN_TOKEN%"=="" set "ADMIN_STATUS=SIM"

set "NODE_VERSION=?"
for /f "tokens=*" %%V in ('%PY% main.py --version 2^>nul') do set "NODE_VERSION=%%V"

echo ============================================================
echo   CONFIGURACAO ATUAL (CLIENTE)
echo ============================================================
echo   Bootstrap  : %BRN_BOOTSTRAP_PEERS%
echo   P2P porta  : %BRN_P2P_PORT%
echo   HTTP       : http://0.0.0.0:%BRN_WEB_PORT%
echo   Tailscale  : %MY_TS_IP%  -^>  %TS_PEER_IP%
echo ============================================================
echo.

echo [*] Iniciando CLIENTE BRN...
echo.
%PY% main.py --client-mode %HEADLESS_FLAG%
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo [X] O no saiu com codigo %EXITCODE%
) else (
    echo [ok] No encerrado normalmente.
)
pause
exit /b %EXITCODE%
