@echo off
REM ============================================================
REM   iniciar.bat - BRN Node CLIENTE (Windows) - v3
REM ============================================================
REM   v3:
REM     [NEW] cria brn_network.env automaticamente se faltar (Opção B)
REM          — antes abortava com "[X] brn_network.env NAO existe"
REM   v2:
REM     [FIX] carrega brn_network.env ANTES do tailscale
REM     [FIX] passa --no-tailscale pro main.py
REM     [FIX] checa TODOS os modulos obrigatorios
REM     [FIX] parser de env trata comentarios (eol=#)
REM     [FIX] fingerprint via Python (nao interpola na shell)
REM     [FIX] bootstrap_peers.json sem IP hardcoded
REM     [NEW] --skip-tailscale / --gui / --no-gui
REM     [NEW] mostra versao do main.py antes de subir
REM ============================================================

setlocal EnableDelayedExpansion

REM -------- Diretorio do script --------
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
echo   BRN Node - CLIENTE (sincroniza da Maquina A) - v3
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
REM  1) CARREGA brn_network.env  (ANTES do tailscale)
REM     [v3] se faltar, cria com defaults e continua
REM ============================================================
if not exist "brn_network.env" (
    echo [!] brn_network.env nao existe - criando com defaults...
    (
        echo # ============================================================
        echo # brn_network.env - Config do no CLIENTE
        echo # ============================================================
        echo # IMPORTANTE: BRN_NETWORK_SECRET e BRN_NODE_PASSWORD devem
        echo # ser IGUAIS aos da Maquina A ^(origem^).
        echo # ============================================================
        echo.
        echo BRN_NETWORK_SECRET=9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9
        echo.
        echo BRN_NODE_PASSWORD=senha-no-temp-123
        echo BRN_WEB_PASS=senha-temp-123
        echo BRN_WALLET_SESSION_PASS=senha-forte-da-carteira-2026
        echo.
        echo BRN_BOOTSTRAP_PEERS=192.168.0.19:6001
        echo BRN_TRACKER=https://brn-tracker.onrender.com
        echo.
        echo BRN_IS_ORIGIN=0
        echo BRN_HTTPS=0
        echo BRN_AUDIT_ENABLED=1
        echo BRN_AUDIT_LOG=audit.log
        echo.
        echo BRN_P2P_AUTH=optional
        echo BRN_P2P_AUTH_WINDOW=30
        echo BRN_P2P_NONCE_CACHE=10000
        echo.
        echo BRN_SKIP_VERIFY=0
        echo BRN_VERIFY_STRICT=0
    ) > brn_network.env
    echo [ok] brn_network.env criado
    echo     ^(edite para ajustar BRN_NETWORK_SECRET e BRN_BOOTSTRAP_PEERS^)
)

echo [*] Carregando brn_network.env...

REM eol=# trata linhas iniciadas por # como comentario.
REM tokens=1,* com delims== divide em K=V (primeiro = define o corte).
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("brn_network.env") do (
    set "K=%%A"
    set "V=%%B"
    if not "!K!"=="" (
        REM Remove aspas duplas no inicio/fim
        if "!V:~0,1!"=="""" set "V=!V:~1!"
        if "!V:~-1!"=="""" set "V=!V:~0,-1!"
        REM Remove aspas simples no inicio/fim
        if "!V:~0,1!"=="'" set "V=!V:~1!"
        if "!V:~-1!"=="'" set "V=!V:~0,-1!"
        REM Remove espacos em volta do nome da variavel
        for /f "tokens=* delims= " %%X in ("!K!") do set "K=%%X"
        if not "!K!"=="" set "!K!=!V!"
    )
)

echo [ok] brn_network.env carregado
echo.

REM ============================================================
REM  2) SENHAS LOCAIS (defaults se env nao definiu)
REM ============================================================
if "%BRN_NODE_PASSWORD%"==""       set "BRN_NODE_PASSWORD=senha-no-temp-123"
if "%BRN_WEB_PASS%"==""            set "BRN_WEB_PASS=senha-temp-123"
if "%BRN_WALLET_SESSION_PASS%"=="" set "BRN_WALLET_SESSION_PASS=senha-forte-da-carteira-2026"

REM ============================================================
REM  3) PARAMETROS OPERACIONAIS
REM ============================================================
if "%BRN_MINER_AUTO%"==""        set "BRN_MINER_AUTO=0"
if "%BRN_MINER_INTERVAL%"==""    set "BRN_MINER_INTERVAL=30"
if "%BRN_ALLOW_SOLO_MINING%"=="" set "BRN_ALLOW_SOLO_MINING=0"
if "%BRN_MIN_PEER_STABLE%"==""   set "BRN_MIN_PEER_STABLE=1"
if "%BRN_UPNP%"==""              set "BRN_UPNP=1"
if "%BRN_P2P_AUTH%"==""          set "BRN_P2P_AUTH=optional"
if "%BRN_WEB_PORT%"==""          set "BRN_WEB_PORT=5000"
if "%BRN_EXPLORER_PORT%"==""     set "BRN_EXPLORER_PORT=8080"
if "%BRN_P2P_PORT%"==""          set "BRN_P2P_PORT=6001"
if "%BRN_LOG_LEVEL%"==""         set "BRN_LOG_LEVEL=INFO"
set "PYTHONUNBUFFERED=1"

REM ============================================================
REM  4) ARQUIVOS ESSENCIAIS
REM ============================================================
set "FALTA="
for %%F in (
    main.py server.py explorer.py
    blockchain.py db.py crypto.py wallet.py
    chain_validator.py checkpoints.py
    brn_config.py brn_logger.py version.py
    security.py secure_store.py
    p2p_unified.py p2p_auth.py p2p_secure.py p2p_cipher.py p2p_relay.py
    discovery_v2.py rendezvous_discovery.py network_guard.py
    miner_loop.py update_peers.py bech32.py
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
REM  5) PAPEL: CLIENTE
REM ============================================================
> "brn_role.txt" echo client
echo [ok] Papel: cliente (nao origina genesis)
echo.

REM ============================================================
REM  6) TAILSCALE (opcional - DEPOIS do env)
REM ============================================================
set "TAILSCALE_FLAG=--no-tailscale"

if "%SKIP_TAILSCALE%"=="1" (
    echo [*] Tailscale: pulado ^(--skip-tailscale^)
    goto tailscale_done
)

if not exist "brn_tailscale_setup.py" (
    echo [*] brn_tailscale_setup.py nao encontrado. Pulando.
    goto tailscale_done
)

if "%TS_AUTH_KEY%"=="" (
    echo [*] TS_AUTH_KEY vazia. Pulando Tailscale.
    goto tailscale_done
)

echo [*] Configurando Tailscale...
%PY% brn_tailscale_setup.py --auto >nul 2>&1
if errorlevel 1 (
    echo [!] Tailscale nao esta pronto.
    echo     Para configurar agora: %PY% brn_tailscale_setup.py
    echo     Continuando sem Tailscale...
) else (
    echo [ok] Tailscale OK
)

:tailscale_done
echo.

REM ============================================================
REM  7) BOOTSTRAP PEERS
REM ============================================================
if not "%BRN_BOOTSTRAP_PEERS%"=="" (
    echo [ok] BRN_BOOTSTRAP_PEERS = %BRN_BOOTSTRAP_PEERS%
) else (
    echo [!] BRN_BOOTSTRAP_PEERS nao definido em brn_network.env
)

if not "%BRN_BOOTSTRAP_FILE%"=="" (
    if not exist "%BRN_BOOTSTRAP_FILE%" (
        > "%BRN_BOOTSTRAP_FILE%" echo []
        echo [ok] %BRN_BOOTSTRAP_FILE% criado ^(vazio^)
    )
)
echo.

REM ============================================================
REM  8) FINGERPRINT DO SEGREDO
REM ============================================================
set "SEC_FP=(vazio)"
if not "%BRN_NETWORK_SECRET%"=="" (
    for /f "tokens=*" %%H in ('%PY% -c "import hashlib,os;print(hashlib.sha256(os.environ.get('BRN_NETWORK_SECRET','').encode()).hexdigest()[:16])" 2^>nul') do (
        set "SEC_FP=%%H"
    )
)

REM ============================================================
REM  9) PYWEBVIEW (GUI ou headless)
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
REM 10) VERSAO DO main.py
REM ============================================================
set "NODE_VERSION=?"
for /f "tokens=*" %%V in ('%PY% main.py --version 2^>nul') do set "NODE_VERSION=%%V"
echo [ok] main.py versao: %NODE_VERSION%
echo.

REM ============================================================
REM 11) RESUMO
REM ============================================================
echo ============================================================
echo   CONFIGURACAO ATUAL (CLIENTE)
echo ============================================================
echo   Modo       : CLIENTE (sincroniza da rede)
echo   Versao     : %NODE_VERSION%
echo   Auth FP    : %SEC_FP%...  ^<- deve ser IGUAL ao da Maquina A
echo   Tracker    : %BRN_TRACKER%
echo   Bootstrap  : %BRN_BOOTSTRAP_PEERS%
echo   Mineracao  : %BRN_MINER_AUTO% (pode ativar na carteira)
echo   P2P porta  : %BRN_P2P_PORT%
echo   HTTP       : http://127.0.0.1:%BRN_WEB_PORT%
echo   Explorer   : http://127.0.0.1:%BRN_EXPLORER_PORT%
echo   Python     : %PYVER%
echo   pywebview  : %GUI_STATUS%
echo   Tailscale  : %TAILSCALE_FLAG%
echo ============================================================
echo.
echo   ATENCAO: deixe esta janela ABERTA enquanto o cliente roda.
echo.

REM ============================================================
REM 12) INICIA O NO EM MODO CLIENTE
REM ============================================================
echo [*] Iniciando CLIENTE BRN... ^(Ctrl+C para encerrar^)
echo.

%PY% main.py --client-mode %TAILSCALE_FLAG% %HEADLESS_FLAG%
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
    echo     - Verifique se a Maquina A esta rodando
    echo     - Verifique se as duas estao na mesma rede/Tailscale
    echo     - Verifique se os segredos batem: type brn_network.env
    echo.
    echo   Se o erro foi 'Modulos obrigatorios faltando':
    echo     Rode: %PY% verify.py
    echo.
) else (
    echo [ok] No encerrado normalmente.
)

echo.
pause
exit /b %EXITCODE%