@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo   BRN - Atualizar main.py (v12.12 - Auto-start PoolClient)
echo ============================================================
echo.
echo   Este script vai substituir o seu main.py atual.
echo   Pressione qualquer tecla para comecar.
pause >nul

set "MEU_ARQ=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c = Get-Content -LiteralPath $env:MEU_ARQ -Raw -Encoding UTF8; $i = $c.IndexOf('~PSSTART~'); if ($i -lt 0) { Write-Host 'ERRO: marcador nao encontrado' -ForegroundColor Red; exit 1 }; $code = $c.Substring($i + 10); Invoke-Expression $code"

pause
exit /b

~PSSTART~
$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath (Split-Path -Parent $env:MEU_ARQ)

$backup = "backup_main_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
New-Item -ItemType Directory -Path $backup -Force | Out-Null
if (Test-Path 'main.py') { Copy-Item 'main.py' -Destination $backup -Force }
Write-Host "[1/2] Backup em: $backup" -ForegroundColor Yellow
Write-Host "[2/2] Escrevendo main.py v12.12..." -ForegroundColor Cyan

$main_content = @'
"""main.py — Entrypoint unificado do nó BRN (v12.12)
================================================================
v12.12:
  [NEW] Auto-start do PoolClient: se o pool estiver ativo, o nó
        inicia o minerador de pool automaticamente.
v12.11:
  [FIX] Validador (is_origin) pode rodar Pool e Minerador Solo juntos.
v12.10:
  [FIX] Aceita --gui e --no-gui (do iniciar.bat v6)
  [FIX] Wallet() em vez de Wallet.create() (nao existia)
================================================================
"""
from __future__ import annotations

import os
import sys
import signal
import argparse
import threading
import time
import json
import subprocess
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

def _bootstrap_env():
    env_file = BASE_DIR / "brn_network.env"
    if not env_file.exists(): return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ: os.environ[k] = v
    except Exception as e:
        print("[boot] aviso: falha lendo " + str(env_file) + ": " + str(e))

_HARDCODED_DEFAULTS = {
    "BRN_NODE_PASSWORD": "senha-no-temp-123",
    "BRN_NETWORK_SECRET": "9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9",
    "BRN_BOOTSTRAP_PEERS": "",
    "BRN_WEB_PASS": "senha-temp-123",
    "BRN_WALLET_SESSION_PASS": "senha-forte-da-carteira-2026",
    "BRN_IS_ORIGIN": "0",
    "BRN_HTTPS": "0",
    "BRN_AUDIT_ENABLED": "1",
    "BRN_AUDIT_LOG": "audit.log",
    "BRN_P2P_AUTH": "optional",
    "BRN_P2P_AUTH_WINDOW": "30",
    "BRN_P2P_NONCE_CACHE": "10000",
    "BRN_SKIP_VERIFY": "0",
    "BRN_VERIFY_STRICT": "0",
    "BRN_TRACKER": "",
    "MONGO_USER": "", "MONGO_PASS": "", "MONGO_HOST": "", "MONGO_DB": "brn_analytics",
    "TS_AUTH_KEY": "", "TS_API_KEY": "", "TS_TAILNET": "", "TS_HOSTNAME_PREFIX": "brn-node",
    "BRN_POOL_ENABLED": "1",
}

def _apply_hardcoded_defaults():
    aplicados = 0
    for k, v in _HARDCODED_DEFAULTS.items():
        if k not in os.environ or os.environ[k] == "":
            os.environ[k] = v
            aplicados += 1
    return aplicados

_bootstrap_env()
_applied = _apply_hardcoded_defaults()
if _applied: print(f"[boot] defaults aplicados: {_applied} variaveis")

from brn_config import Config
from brn_logger import setup_logger, get_logger
from version import VERSION, BUILD_DATE, GITHUB_USER, GITHUB_REPO

_shutdown = threading.Event()
NODE_ID_PATH = BASE_DIR / "node_identity.enc"
CLIENT_BOOT_TIMEOUT = int(os.environ.get("BRN_CLIENT_BOOT_TIMEOUT", "120"))
WEB_HOST = os.environ.get("BRN_WEB_HOST", "127.0.0.1")
EXPLORER_HOST = os.environ.get("BRN_EXPLORER_HOST", "127.0.0.1")
AUTO_MINE = os.environ.get("BRN_AUTO_MINE", "0") == "1"

_running_services = []
_services_lock = threading.Lock()

def _register_service(name, svc):
    with _services_lock: _running_services.append((name, svc))

def _thread_excepthook(args):
    try:
        log = get_logger("thread")
        log.error("Thread caiu: " + str(args.exc_value), exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    except Exception: pass

threading.excepthook = _thread_excepthook

def _thread_wrap(name, fn):
    def runner():
        try: fn()
        except Exception as e: get_logger(name).exception("thread " + name + " caiu: " + str(e))
    return runner

def _check_required_modules():
    required = ["brn_config", "brn_logger", "version", "blockchain", "db", "crypto", "wallet", "bech32", "secure_store", "chain_validator", "checkpoints", "p2p_auth", "p2p_cipher", "p2p_secure", "p2p_unified", "discovery_v2", "miner_loop"]
    missing = []
    for m in required:
        try: __import__(m)
        except Exception: missing.append(m)
    return missing

L2_ENABLED = False
btc_config = None
BTCWatcher = None
L2Manager = None
try:
    import btc_config
    from btc_watcher import BTCWatcher
    from l2_manager import L2Manager
    L2_ENABLED = True
    print("[L2] Modulos BTC carregados OK")
except Exception as _e:
    print("[AVISO] btc_watcher/l2_manager indisponivel: " + str(_e))

def _l2_disponivel(): return L2_ENABLED and btc_config and BTCWatcher and L2Manager

POOL_ENABLED = os.environ.get("BRN_POOL_ENABLED", "1") == "1"
PoolManager = None
if POOL_ENABLED:
    try:
        from mining_pool import PoolManager
        print("[Pool] Modulo de pool carregado OK")
    except Exception as _e:
        print("[AVISO] mining_pool indisponivel: " + str(_e))
        PoolManager = None

def _pool_disponivel(): return POOL_ENABLED and PoolManager is not None

def _audit(event, data=None):
    try:
        from security import audit_log
        audit_log(event, data or {})
    except Exception: pass

def _close_audit():
    try:
        from security import close_audit
        close_audit()
    except Exception: pass

def _setup_tailscale_auto(log):
    auth_key = os.environ.get("TS_AUTH_KEY", "").strip()
    if not auth_key: return True
    setup_path = BASE_DIR / "brn_tailscale_setup.py"
    if not setup_path.exists(): return True
    try:
        r = subprocess.run([sys.executable, str(setup_path), "--auto"], capture_output=True, text=True, timeout=180, cwd=str(BASE_DIR))
        return r.returncode == 0
    except Exception: return False

def parse_args():
    p = argparse.ArgumentParser(prog="main.py", description="BRN Node")
    p.add_argument("--status", action="store_true")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--version", action="store_true")
    p.add_argument("--check-update", action="store_true")
    p.add_argument("--config", default="config.json")
    p.add_argument("--log-level", default=None)
    p.add_argument("--log-file", default=None)
    p.add_argument("--password-file", default=None)
    p.add_argument("--rotate-node-id", action="store_true")
    p.add_argument("--client-mode", action="store_true")
    p.add_argument("--discover", action="store_true")
    p.add_argument("--l2", action="store_true")
    p.add_argument("--l2-stats", action="store_true")
    p.add_argument("--mine", action="store_true")
    p.add_argument("--no-p2p", action="store_true")
    p.add_argument("--relay", dest="relay", action="store_true", default=None)
    p.add_argument("--no-relay", dest="relay", action="store_false")
    p.add_argument("--verify-only", action="store_true")
    p.add_argument("--skip-verify", action="store_true")
    p.add_argument("--no-tailscale", action="store_true")
    p.add_argument("--no-sync", action="store_true")
    p.add_argument("--no-pool", action="store_true")
    p.add_argument("--pool-stats", action="store_true")
    
    gui_group = p.add_mutually_exclusive_group()
    gui_group.add_argument("--gui", action="store_true")
    gui_group.add_argument("--no-gui", action="store_true")
    return p.parse_args()

def _resolve_password(args):
    if args.password_file:
        return Path(args.password_file).read_text(encoding="utf-8").rstrip("\r\n")
    env = os.environ.get("BRN_NODE_PASSWORD")
    if env: return env
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass("Senha do no: ")
    raise SystemExit("Senha do no nao informada.")

def load_or_create_node_identity(password, rotate=False):
    from crypto import Ed25519PrivateKey
    from secure_store import save_wallet, load_wallet
    log = get_logger("identity")
    if rotate and NODE_ID_PATH.exists():
        NODE_ID_PATH.replace(NODE_ID_PATH.with_suffix(".enc.bak"))
    if NODE_ID_PATH.exists():
        data = load_wallet(password, str(NODE_ID_PATH))
        if not data: raise SystemExit("Falha ao decifrar " + str(NODE_ID_PATH))
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"])), data["pub"]
    sk = Ed25519PrivateKey.generate()
    pub_hex = sk.public_key().public_bytes_raw().hex()
    save_wallet({"sk": sk.private_bytes_raw().hex(), "pub": pub_hex}, password, str(NODE_ID_PATH))
    return sk, pub_hex

def _read_role():
    try:
        p = BASE_DIR / "brn_role.txt"
        if p.exists(): return p.read_text(encoding="utf-8").strip().lower()
    except Exception: pass
    return "auto"

def _is_origin(client_mode):
    if client_mode: return False
    if os.environ.get("BRN_IS_ORIGIN", "0") == "1": return True
    return _read_role() in ("origin", "hub", "servidor")

VERIFY_OK, VERIFY_INVALID, VERIFY_ERROR = "ok", "invalid", "error"

def _verify_chain(chain):
    try:
        from chain_validator import verify_chain
    except ImportError: return VERIFY_ERROR
    log = get_logger("verify")
    try:
        r = verify_chain(chain)
        valid = getattr(r, "valid", bool(r))
        if valid: return VERIFY_OK
        return VERIFY_INVALID
    except Exception: return VERIFY_ERROR

def do_status(cfg):
    print("=" * 64)
    print(" BRN Node - Diagnostico")
    print("=" * 64)
    print(" Config : " + cfg.source)
    print(" Web    : " + WEB_HOST + ":" + str(cfg["web_port"]))
    print(" P2P    : " + str(cfg["p2p_port"]))
    missing = _check_required_modules()
    if missing: print(" [!] Modulos faltando: " + ", ".join(missing))
    else: print(" Modulos     : OK")
    print("=" * 64)

def _init_http_server(chain, log):
    try:
        from server import app as http_app, set_chain, auto_registrar_carteira_atual
        set_chain(chain)
        auto_registrar_carteira_atual()
        return http_app
    except Exception as e:
        log.error("Falha importando server.py: " + str(e))
        return None

def run_http(app_instance):
    log = get_logger("http")
    port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    log.info("HTTP http://" + WEB_HOST + ":" + str(port))
    app_instance.run(host=WEB_HOST, port=port, threaded=True, debug=False, use_reloader=False)

def run_explorer():
    from explorer import app as explorer_app
    port = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
    get_logger("explorer").info("Explorer http://" + EXPLORER_HOST + ":" + str(port))
    explorer_app.run(host=EXPLORER_HOST, port=port, threaded=True, debug=False, use_reloader=False)

def run_status_loop(chain, p2p, l2_manager=None, sync=None, pool=None):
    log = get_logger("status")
    while not _shutdown.is_set():
        time.sleep(30)
        try:
            msg = "Altura=" + str(chain.db.height()) + " Peers=" + str(p2p.get_status().get("peer_count", 0) if p2p else 0)
            if pool: msg += " Pool[" + str(pool.stats().get("miners_active", 0)) + "]"
            log.info(msg)
        except Exception: pass

def run_wallet_main_thread():
    try:
        from app_wallet_v3 import WalletApi
        import webview
        api = WalletApi()
        webview.create_window("BRN RWA — Carteira Digital", url=(BASE_DIR / "index_wallet.html").resolve().as_uri(), js_api=api, width=1020, height=880)
        webview.start(debug=False)
    except Exception as e: get_logger("wallet").error("Falha GUI: " + str(e))

def _setup_relay(p2p, node_id_priv, enabled):
    if not enabled or p2p is None: return None
    try:
        from p2p_relay import RelayManager
        relay = RelayManager(p2p, node_id_priv)
        srv = getattr(p2p, "server", None)
        if srv and hasattr(srv, "on_relay_request"):
            srv.on_relay_request = relay.handle_relay_request
            srv.on_relay_incoming = relay.handle_incoming_relay
            srv.on_relay_accepted = lambda ip, pid, msg: relay.handle_route_accepted(msg)
            srv.on_relay_data = relay.handle_relay_data
        return relay
    except Exception: return None

def _setup_sync(chain, p2p, log):
    if p2p is None: return None
    try:
        from sync_manager import ChainSyncManager
        sync = ChainSyncManager(chain, chain.db, p2p)
        sync.set_sync_mode("full")
        sync.start_sync_loop()
        return sync
    except Exception: return None

def _setup_pool(chain, p2p, log, disabled):
    if disabled or not _pool_disponivel(): return None
    try:
        pool = PoolManager(chain)
        if p2p: pool.on_block_mined = lambda blk: p2p.broadcast_block(blk)
        return pool
    except Exception: return None

def _setup_local_miner_broadcast(chain, p2p, log):
    if p2p is None: return False
    try:
        chain.on_block_mined = lambda blk: p2p.broadcast_block(blk)
        return True
    except Exception: return False

def _stop_with_timeout(name, svc, timeout=5.0, log=None):
    try:
        fn = svc.stop if hasattr(svc, "stop") else svc
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        t.join(timeout)
    except Exception: pass

def _graceful_shutdown(log):
    log.info("Encerrando servicos...")
    with _services_lock: services = list(_running_services)
    for name, svc in reversed(services): _stop_with_timeout(name, svc)
    _audit("node_stop", {})
    _close_audit()

def main():
    args = parse_args()
    cfg = Config(args.config)

    if args.read_only: cfg.data["read_only"] = True
    if args.headless: cfg.data["headless"] = True
    if args.log_level: cfg.data["log_level"] = args.log_level
    if args.log_file: cfg.data["log_file"] = args.log_file

    if getattr(args, "no_gui", False): cfg.data["headless"] = True
    elif getattr(args, "gui", False): cfg.data["headless"] = False

    cfg.apply_to_env()
    log = setup_logger(level=cfg["log_level"], log_file=cfg["log_file"] or None)
    is_origin = _is_origin(args.client_mode)

    log.info("=" * 60)
    log.info(" BRN Node v" + VERSION + " (" + BUILD_DATE + ")")
    log.info(" Modo: " + ("ORIGEM" if is_origin else "CLIENTE"))
    log.info("=" * 60)

    if args.version: print(VERSION); return 0
    if args.status: do_status(cfg); return 0

    missing = _check_required_modules()
    if missing:
        log.error("Modulos faltando: " + ", ".join(missing))
        return 1

    if not args.no_tailscale: _setup_tailscale_auto(log)

    node_password = _resolve_password(args)
    node_id_priv, node_id_pub = load_or_create_node_identity(node_password, rotate=args.rotate_node_id)

    from blockchain import Blockchain
    chain = Blockchain(cfg["db_path"], auto_genesis=is_origin)
    
    if not args.skip_verify: _verify_chain(chain)

    p2p = None
    if not args.no_p2p:
        try:
            from p2p_unified import P2PManager
            p2p = P2PManager(chain, node_id_priv, tcp_port=cfg["p2p_port"], enable_upnp=cfg["upnp"])
            p2p.start()
            _register_service("p2p", p2p)
        except Exception as e: log.error("P2P falhou: " + str(e))

    relay = _setup_relay(p2p, node_id_priv, args.relay)
    if relay: _register_service("relay", relay)

    sync = _setup_sync(chain, p2p, log)
    if sync: _register_service("sync", sync)

    pool = _setup_pool(chain, p2p, log, disabled=args.no_pool)
    if pool: _register_service("pool", pool)

    _setup_local_miner_broadcast(chain, p2p, log)

    if p2p:
        try:
            import server as _srv
            _srv.set_p2p_manager(p2p)
            if pool: _srv.set_pool_manager(pool)
        except Exception: pass

    http_app = _init_http_server(chain, log)
    if http_app: threading.Thread(target=_thread_wrap("HTTP", lambda: run_http(http_app)), daemon=True).start()
    threading.Thread(target=_thread_wrap("Explorer", run_explorer), daemon=True).start()

    # ============================================================
    # v12.11: Validador pode rodar Pool e Solo juntos
    # ============================================================
    quer_miner_local = bool(args.mine or AUTO_MINE)
    if quer_miner_local and pool is not None and not is_origin:
        log.warning("Cliente: Minerador Solo IGNORADO (Pool ativo).")
        quer_miner_local = False

    if quer_miner_local:
        try:
            from miner_loop import get_miner
            from wallet import Wallet
            from secure_store import save_wallet, load_wallet
            miner = get_miner(chain)
            mw_path = BASE_DIR / "miner_wallet.enc"
            mw = None
            try:
                data = load_wallet(node_password, str(mw_path))
                if data: mw = Wallet.from_dict(data)
            except Exception: pass
            
            if mw is None:
                mw = Wallet()
                save_wallet(mw.to_dict(), node_password, str(mw_path))
                
            miner.start(mw.address, mw.pub_hex)
            _register_service("miner", miner)
        except Exception as e:
            log.warning("Miner local falhou: " + str(e))

    # ============================================================
    # v12.12: Auto-start do PoolClient (se o pool estiver ativo)
    # ============================================================
    if pool is not None and not args.no_pool:
        try:
            from wallet import Wallet
            from secure_store import save_wallet, load_wallet
            from miner_pool_client import get_pool_client

            mw_path = BASE_DIR / "miner_wallet.enc"
            mw = None
            try:
                data = load_wallet(node_password, str(mw_path))
                if data: mw = Wallet.from_dict(data)
            except Exception: pass

            if mw is None:
                mw = Wallet()
                save_wallet(mw.to_dict(), node_password, str(mw_path))

            pool_client = get_pool_client(address=mw.address, pubkey=mw.pub_hex)
            if pool_client:
                ok, msg = pool_client.start_mining()
                log.info(f"Auto-start PoolClient: {ok} - {msg}")
                _register_service("pool_client", pool_client)
        except Exception as e:
            log.warning("Falha ao iniciar PoolClient automaticamente: " + str(e))

    threading.Thread(target=_thread_wrap("StatusLoop", lambda: run_status_loop(chain, p2p, None, sync, pool)), daemon=True).start()

    _audit("node_start", {"mode": "origin" if is_origin else "client", "height": chain.db.height()})
    log.info("No pronto. Ctrl+C para encerrar.")

    try:
        if not cfg["headless"]: run_wallet_main_thread()
        else:
            while not _shutdown.is_set(): time.sleep(1)
    except KeyboardInterrupt: pass

    _shutdown.set()
    _graceful_shutdown(log)
    try: chain.db.close()
    except Exception: pass
    return 0

_signal_count = 0
def _on_signal(signum, frame):
    global _signal_count
    _signal_count += 1
    if _signal_count >= 2: os._exit(1)
    _shutdown.set()

if __name__ == "__main__":
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    try: sys.exit(main())
    except SystemExit: raise
    except Exception as e:
        get_logger("main").exception("Erro fatal: " + str(e))
        sys.exit(1)
'@

Set-Content -Path 'main.py' -Value $main_content -Encoding UTF8

Write-Host "Concluido! main.py atualizado para v12.12." -ForegroundColor Green
Write-Host ""
Write-Host "Para aplicar, rode o seu iniciar.bat novamente." -ForegroundColor Cyan
Write-Host ""
