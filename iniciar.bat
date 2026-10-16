@echo off
REM ============================================================
REM   iniciar.bat - BRN Node CLIENTE (Windows) - v5
REM ============================================================
REM   v5:
REM     [FIX] Remove sintaxe bash que quebrava o resumo
REM     [FIX] Lista de arquivos obrigatorios completa
REM     [NEW] Auto-detecta IP Tailscale do peer
REM     [NEW] Cria pasta contracts/ se faltar
REM     [NEW] Verifica escrow_btc_brn/factory/vm
REM   v4:
REM     [NEW] Verifica Tailscale antes de subir
REM     [NEW] Faz tailscale ping no peer do brn_network.env
REM ============================================================

setlocal EnableDelayedExpansion

REM -------- Ir para pasta do script --------
cd /d "%~dp0"
if errorlevel 1 (
    echo [X] Nao consegui cd para %~dp0
    pause
    exit /b 1
)

REM -------- Argumentos --------
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
echo   BRN Node - CLIENTE (sincroniza da Maquina A) - v5
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
    echo     Instale: https://www.python.org/downloads/
    echo     Marque "Add Python to PATH" na instalacao.
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
    echo.
    echo     Verifique se todos os arquivos foram salvos na pasta:
    echo     %~dp0
    pause
    exit /b 1
)
echo [ok] Arquivos essenciais OK
echo.

REM ============================================================
REM  4) TAILSCALE
REM ============================================================
if "%SKIP_TAILSCALE%"=="1" (
    echo [*] Tailscale: pulado ^(--skip-tailscale^)
    goto tailscale_done
)

where tailscale >nul 2>&1
if errorlevel 1 (
    echo [!] Tailscale NAO instalado.
    echo     Baixe: https://tailscale.com/download/windows
    pause
    exit /b 1
)
echo [ok] Tailscale instalado

tailscale status >nul 2>&1
if errorlevel 1 (
    echo [!] Tailscale NAO esta logado.
    echo     Clique com botao direito no icone do Tailscale
    echo     perto do relogio e escolha "Log in".
    pause
    exit /b 1
)
echo [ok] Tailscale logado

set "MY_TS_IP="
for /f "tokens=*" %%I in ('tailscale ip -4 2^>nul') do (
    if "!MY_TS_IP!"=="" set "MY_TS_IP=%%I"
)
if "%MY_TS_IP%"=="" (
    echo [!] Tailscale sem IP. Verifique com: tailscale status
    pause
    exit /b 1
)
echo [ok] Meu IP Tailscale: %MY_TS_IP%

REM ---- Auto-detectar IP do peer no Tailscale ----
set "TS_PEER_IP="
if not "%BRN_BOOTSTRAP_PEERS%"=="" (
    for /f "tokens=1 delims=,:" %%I in ("%BRN_BOOTSTRAP_PEERS%") do set "TS_PEER_IP=%%I"
)

REM Se bootstrap vazio, descobre pelo tailscale status (primeiro IP que nao seja o nosso)
if "%TS_PEER_IP%"=="" (
    echo [*] Bootstrap vazio - auto-detectando peer via Tailscale...
    for /f "tokens=1" %%I in ('tailscale status 2^>nul ^| findstr /R "^100\."') do (
        if not "%%I"=="%MY_TS_IP%" (
            if "!TS_PEER_IP!"=="" set "TS_PEER_IP=%%I"
        )
    )
    if not "!TS_PEER_IP!"=="" (
        echo [ok] Peer detectado: !TS_PEER_IP!
        set "BRN_BOOTSTRAP_PEERS=!TS_PEER_IP!:6001"
        REM salva de volta no env
        (
            echo !BRN_BOOTSTRAP_PEERS!
        ) > nul
    )
)

if "%TS_PEER_IP%"=="" (
    echo [!] Nenhum peer Tailscale encontrado.
    echo     Continuando mesmo assim...
) else (
    echo [*] Testando peer %TS_PEER_IP%...
    tailscale ping -c 1 "%TS_PEER_IP%" >nul 2>&1
    if errorlevel 1 (
        echo [!] Peer %TS_PEER_IP% NAO respondeu ao ping.
        echo     Verifique se a outra maquina esta ligada e logada.
    ) else (
        echo [ok] Peer %TS_PEER_IP% alcancavel
    )
)

:tailscale_done
echo.

REM ============================================================
REM  5) WAITRESS (opcional)
REM ============================================================
%PY% -c "import waitress" >nul 2>&1
if errorlevel 1 (
    echo [!] Waitress NAO instalado - HTTP usara Flask dev server
    echo     Para producao: %PY% -m pip install waitress
) else (
    for /f "tokens=*" %%V in ('%PY% -c "import waitress;print(waitress.__version__)" 2^>nul') do set "WV=%%V"
    echo [ok] Waitress v!WV!
)
echo.

REM ============================================================
REM  6) PAPEL: CLIENTE
REM ============================================================
> "brn_role.txt" echo client
echo [ok] Papel: cliente (nao origina genesis)
echo.

REM ============================================================
REM  7) PYWEBVIEW (GUI ou headless)
REM ============================================================
set "HEADLESS_FLAG="

if "%FORCE_NO_GUI%"=="1" (
    set "HEADLESS_FLAG=--headless"
    echo [*] GUI desabilitada por --no-gui
    goto gui_done
)

if "%FORCE_GUI%"=="1" (
    echo [ok] GUI forcada por --gui
    goto gui_done
)

%PY% -c "import webview" >nul 2>&1
if errorlevel 1 (
    set "HEADLESS_FLAG=--headless"
    echo [!] pywebview ausente - modo HEADLESS
    echo     Para instalar GUI: %PY% -m pip install pywebview
    goto gui_done
)

echo [ok] pywebview OK - carteira desktop vai abrir

:gui_done
set "GUI_STATUS=GUI"
if "%HEADLESS_FLAG%"=="--headless" set "GUI_STATUS=HEADLESS"
echo.

REM ============================================================
REM  8) RESOLVE ADMIN TOKEN STATUS (sintaxe batch correta)
REM ============================================================
set "ADMIN_STATUS=NAO"
if not "%BRN_ADMIN_TOKEN%"=="" set "ADMIN_STATUS=SIM"

REM ============================================================
REM  9) VERSAO main.py
REM ============================================================
set "NODE_VERSION=?"
for /f "tokens=*" %%V in ('%PY% main.py --version 2^>nul') do set "NODE_VERSION=%%V"
echo [ok] main.py versao: %NODE_VERSION%
echo.

REM ============================================================
REM 10) RESUMO
REM ============================================================
echo ============================================================
echo   CONFIGURACAO ATUAL (CLIENTE)
echo ============================================================
echo   Modo       : CLIENTE (sincroniza da rede)
echo   Versao     : %NODE_VERSION%
echo   Bootstrap  : %BRN_BOOTSTRAP_PEERS%
echo   All miners : %BRN_ALL_MINERS%
echo   Min diff   : %BRN_MIN_DIFFICULTY%
echo   Admin tok  : %ADMIN_STATUS%
echo   P2P porta  : %BRN_P2P_PORT%
echo   HTTP       : http://0.0.0.0:%BRN_WEB_PORT%
echo   Explorer   : http://0.0.0.0:%BRN_EXPLORER_PORT%
echo   Python     : %PYVER%
echo   pywebview  : %GUI_STATUS%
echo   Tailscale  : %MY_TS_IP%  -^>  %TS_PEER_IP%
echo ============================================================
echo.
echo   Modo 2 (mineracao aberta):
echo     - /api/mine, /api/miner/*  sao PUBLICOS
echo     - /api/faucet             protegido por token
echo.
echo   ATENCAO: deixe esta janela ABERTA enquanto o cliente roda.
echo.

REM ============================================================
REM 11) INICIA O NO
REM ============================================================
echo [*] Iniciando CLIENTE BRN... ^(Ctrl+C para encerrar^)
echo.

%PY% main.py --client-mode %HEADLESS_FLAG%
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo [X] O no saiu com codigo %EXITCODE%
    echo.
    echo   Se o erro foi 'Falha ao decifrar node_identity.enc':
    echo     del node_identity.enc
    echo     iniciar.bat
    echo.
    echo   Se o erro foi 'TIMEOUT esperando genesis':
    echo     - Maquina A ligada? tailscale ping %TS_PEER_IP%
    echo     - segredos batem? type brn_network.env
    echo.
    echo   Se o erro foi 'Modulos obrigatorios faltando':
    echo     %PY% verify.py
    echo.
) else (
    echo [ok] No encerrado normalmente.
)

echo.
pause
exit /b %EXITCODE%