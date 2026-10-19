"""main.py — Entrypoint unificado do nó BRN (v13.2)
================================================================
v13.2:
  [NEW] Injeta P2P, Sync e Pool em server.py APÓS criar cada um
        (sem isto, /api/pool/* e /api/sync/status devolvem 503)
  [NEW] _init_http_server() aceita p2p/sync/pool e injeta internamente
  [NEW] Logs explícitos das injeções (INFO ou WARN)
  [FIX] Ordem de boot: HTTP → P2P → Sync → Pool → injeção retroativa
v13.1:
  [FIX] Corrigido SyntaxError na linha 925 (try sem except)
  [NEW] Inicialização completa do PoolManager + PoolClient
  [NEW] Encerramento gracioso de todas as threads (HTTP, P2P, GUI)
v13.0:
  [NEW] Pool client automático no nó ORIGEM
v12.10:
  [FIX] Aceita --gui e --no-gui
  [FIX] Wallet() em vez de Wallet.create()
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


# ============================================================
# BASE_DIR
# ============================================================
BASE_DIR = Path(__file__).resolve().parent


def _bootstrap_env() -> None:
    env_file = BASE_DIR / "brn_network.env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception as e:
        print("[boot] aviso: falha lendo " + str(env_file) + ": " + str(e))


# ============================================================
# DEFAULTS HARDCODED
# ============================================================
_HARDCODED_DEFAULTS = {
    "BRN_NODE_PASSWORD":       "senha-no-temp-123",
    "BRN_NETWORK_SECRET":      "9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9",
    "BRN_BOOTSTRAP_PEERS":     "",
    "BRN_WEB_PASS":            "senha-temp-123",
    "BRN_WALLET_SESSION_PASS": "senha-forte-da-carteira-2026",
    "BRN_IS_ORIGIN":           "0",
    "BRN_HTTPS":               "0",
    "BRN_AUDIT_ENABLED":       "1",
    "BRN_AUDIT_LOG":           "audit.log",
    "BRN_P2P_AUTH":            "optional",
    "BRN_P2P_AUTH_WINDOW":     "30",
    "BRN_P2P_NONCE_CACHE":     "10000",
    "BRN_SKIP_VERIFY":         "0",
    "BRN_VERIFY_STRICT":       "0",
    "BRN_TRACKER":             "",
    "MONGO_USER":              "",
    "MONGO_PASS":              "",
    "MONGO_HOST":              "",
    "MONGO_DB":                "brn_analytics",
    "TS_AUTH_KEY":             "",
    "TS_API_KEY":              "",
    "TS_TAILNET":              "",
    "TS_HOSTNAME_PREFIX":      "brn-node",
    "BRN_POOL_ENABLED":        "1",
    "BRN_POOL_URL_LOCAL":      "http://127.0.0.1:5000",
}


def _apply_hardcoded_defaults() -> int:
    aplicados = 0
    for k, v in _HARDCODED_DEFAULTS.items():
        if k not in os.environ or os.environ[k] == "":
            os.environ[k] = v
            aplicados += 1
    return aplicados


_bootstrap_env()
_applied = _apply_hardcoded_defaults()
if _applied:
    print(f"[boot] defaults aplicados: {_applied} variáveis")


# ============================================================
# IMPORTS APÓS BOOTSTRAP
# ============================================================
from brn_config import Config
from brn_logger import setup_logger, get_logger
from version import VERSION, BUILD_DATE, GITHUB_USER, GITHUB_REPO

_shutdown = threading.Event()

NODE_ID_PATH        = BASE_DIR / "node_identity.enc"
CLIENT_BOOT_TIMEOUT = int(os.environ.get("BRN_CLIENT_BOOT_TIMEOUT", "120"))

WEB_HOST      = os.environ.get("BRN_WEB_HOST",      "127.0.0.1")
EXPLORER_HOST = os.environ.get("BRN_EXPLORER_HOST", "127.0.0.1")

AUTO_MINE = os.environ.get("BRN_AUTO_MINE", "0") == "1"

_running_services: list = []
_services_lock = threading.Lock()


def _register_service(name: str, svc) -> None:
    with _services_lock:
        _running_services.append((name, svc))


# ============================================================
# THREAD EXCEPTHOOK
# ============================================================
def _thread_excepthook(args):
    try:
        log = get_logger("thread")
        thread_name = args.thread.name if args.thread else "?"
        log.error(
            "Thread '" + str(thread_name) + "' morreu: " + str(args.exc_value),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
    except Exception:
        pass


threading.excepthook = _thread_excepthook


# ============================================================
# MÓDULOS OBRIGATÓRIOS
# ============================================================
def _check_required_modules() -> list:
    required = [
        "brn_config", "brn_logger", "version",
        "blockchain", "db", "crypto", "wallet", "bech32",
        "secure_store", "chain_validator", "checkpoints",
        "p2p_auth", "p2p_cipher", "p2p_secure", "p2p_unified",
        "discovery_v2", "miner_loop",
    ]
    missing = []
    for m in required:
        try:
            __import__(m)
        except Exception:
            missing.append(m)
    return missing


# ============================================================
# L2 / POOL
# ============================================================
L2_ENABLED = False
btc_config = None
BTCWatcher = None
L2Manager = None

try:
    import btc_config
    from btc_watcher import BTCWatcher
    from l2_manager import L2Manager
    L2_ENABLED = True
    print("[L2] Módulos BTC carregados OK")
except Exception as _e:
    print("[AVISO] btc_watcher/l2_manager indisponível: " + str(_e))

POOL_ENABLED = os.environ.get("BRN_POOL_ENABLED", "1") == "1"
PoolManager = None
PoolClient = None

if POOL_ENABLED:
    try:
        from mining_pool import PoolManager
        print("[Pool] PoolManager carregado OK")
    except Exception as _e:
        print("[AVISO] mining_pool indisponível: " + str(_e))
        POOL_ENABLED = False

    try:
        from miner_pool_client import PoolClient
        print("[Pool] PoolClient carregado OK")
    except Exception as _e:
        print("[AVISO] miner_pool_client indisponível: " + str(_e))
        PoolClient = None


# ============================================================
# ARGS
# ============================================================
def parse_args():
    p = argparse.ArgumentParser(prog="main.py", description="BRN Node v13")
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
    p.add_argument("--no-pool-client", action="store_true")
    p.add_argument("--pool-stats", action="store_true")
    p.add_argument("--gui", action="store_true")
    p.add_argument("--no-gui", action="store_true")
    return p.parse_args()


# ============================================================
# SENHA / IDENTIDADE
# ============================================================
def _resolve_password(args) -> str:
    if args.password_file:
        p = Path(args.password_file)
        if not p.exists():
            raise SystemExit("--password-file não encontrado: " + str(p))
        return p.read_text(encoding="utf-8").rstrip("\r\n")
    env = os.environ.get("BRN_NODE_PASSWORD")
    if env:
        return env
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass("Senha do nó (identidade Ed25519): ")
    raise SystemExit("Senha do nó não informada.")


def load_or_create_node_identity(password: str, rotate: bool = False):
    from crypto import Ed25519PrivateKey
    from secure_store import save_wallet, load_wallet

    log = get_logger("identity")

    if rotate and NODE_ID_PATH.exists():
        backup = NODE_ID_PATH.with_suffix(".enc.bak")
        NODE_ID_PATH.replace(backup)
        log.warning("Identidade rotacionada. Backup em " + str(backup))

    if NODE_ID_PATH.exists():
        data = load_wallet(password, str(NODE_ID_PATH))
        if not data:
            raise SystemExit("Falha ao decifrar " + str(NODE_ID_PATH))
        sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
        return sk, data["pub"]

    sk = Ed25519PrivateKey.generate()
    pub_hex = sk.public_key().public_bytes_raw().hex()
    save_wallet(
        {"sk": sk.private_bytes_raw().hex(), "pub": pub_hex},
        password, str(NODE_ID_PATH),
    )
    log.info("Identidade nova criada: " + pub_hex[:16] + "... (" + str(NODE_ID_PATH) + ")")
    return sk, pub_hex


# ============================================================
# PAPEL DO NÓ
# ============================================================
def _read_role() -> str:
    try:
        p = BASE_DIR / "brn_role.txt"
        if p.exists():
            return p.read_text(encoding="utf-8").strip().lower()
    except Exception:
        pass
    return "auto"


def _is_origin(client_mode: bool) -> bool:
    if client_mode:
        return False
    if os.environ.get("BRN_IS_ORIGIN", "0") == "1":
        return True
    return _read_role() in ("origin", "hub", "servidor")


# ============================================================
# VERIFY
# ============================================================
def _verify_chain(chain) -> str:
    try:
        from chain_validator import verify_chain
    except ImportError:
        get_logger("verify").info("chain_validator.py não disponível")
        return "error"

    log = get_logger("verify")
    log.info("Verificando integridade da cadeia...")
    t0 = time.time()
    try:
        r = verify_chain(chain)
    except Exception as e:
        log.exception("validador falhou: " + str(e))
        return "error"

    dt = time.time() - t0
    valid = getattr(r, "valid", None)
    if valid is None:
        valid = bool(r)

    if valid:
        log.info(f"OK cadeia íntegra ({dt:.2f}s)")
        return "ok"

    log.error("CADEIA INVÁLIDA")
    return "invalid"


# ============================================================
# [NEW v13.2] INJEÇÕES NO SERVER.PY
# ============================================================
def _inject_into_server(what: str, obj) -> bool:
    """
    Injeta um manager no server.py.
    Retorna True se conseguiu, False se server.py não está disponível
    ou não tem o setter (versões antigas).
    """
    log = get_logger("server_inject")
    setter_name = {
        "p2p":  "set_p2p_manager",
        "sync": "set_sync_manager",
        "pool": "set_pool_manager",
    }.get(what)

    if setter_name is None:
        log.warning(f"[inject] tipo desconhecido: {what}")
        return False

    try:
        import server as _srv
        setter = getattr(_srv, setter_name, None)
        if setter is None:
            log.warning(
                f"[inject] server.py NAO tem {setter_name}() — "
                f"atualize o server.py para v9.9.7+"
            )
            return False
        setter(obj)
        log.info(f"[inject] {what} injetado em server.py")
        return True
    except ImportError:
        log.warning("[inject] server.py indisponível (import falhou)")
        return False
    except Exception as e:
        log.warning(f"[inject] {what} falhou: {e}")
        return False


# ============================================================
# HTTP / EXPLORER
# ============================================================
def _init_http_server(chain, log, p2p=None, sync=None, pool=None):
    """
    Importa server.py, chama set_chain() e (se v9.9.7+)
    set_p2p_manager / set_sync_manager / set_pool_manager.

    Retorna a instância Flask (app) ou None se falhar.
    """
    try:
        from server import (
            app as http_app,
            set_chain,
            auto_registrar_carteira_atual,
        )
    except Exception as e:
        log.error("Falha importando server.py: " + str(e))
        return None

    # 1. Chain (obrigatório)
    try:
        set_chain(chain)
        log.info("server.py: chain injetada")
    except Exception as e:
        log.error("Falha set_chain: " + str(e))
        return None

    # 2. P2P (opcional — só se server.py for v9.9.7+)
    if p2p is not None:
        _inject_into_server("p2p", p2p)

    # 3. Sync (opcional)
    if sync is not None:
        _inject_into_server("sync", sync)

    # 4. Pool (opcional)
    if pool is not None:
        _inject_into_server("pool", pool)

    # 5. Auto-registro de carteira
    try:
        reg = auto_registrar_carteira_atual()
        if reg.get("ok"):
            log.info("auto-registro pubkey: " + str(reg.get("msg")))
        else:
            log.info("auto-registro pubkey: pulado (" + str(reg.get("msg")) + ")")
    except Exception as e:
        log.warning("auto-registro pubkey falhou: " + str(e))

    return http_app


def run_http(app_instance):
    log = get_logger("http")
    port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    ssl_ctx = None
    if os.environ.get("BRN_HTTPS", "0") == "1":
        try:
            from security import ensure_self_signed_cert
            cert, key = ensure_self_signed_cert()
            ssl_ctx = (cert, key)
            log.info("HTTPS https://" + WEB_HOST + ":" + str(port))
        except Exception as e:
            log.warning("Falha ao gerar cert HTTPS: " + str(e) + ". Caindo para HTTP.")
            ssl_ctx = None
    if ssl_ctx is None:
        log.info("HTTP http://" + WEB_HOST + ":" + str(port))
    app_instance.run(
        host=WEB_HOST, port=port, threaded=True,
        debug=False, use_reloader=False, ssl_context=ssl_ctx,
    )


def run_explorer():
    from explorer import app as explorer_app
    log = get_logger("explorer")
    port = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
    ssl_ctx = None
    if os.environ.get("BRN_HTTPS", "0") == "1":
        try:
            from security import ensure_self_signed_cert
            ssl_ctx = ensure_self_signed_cert()
        except Exception:
            ssl_ctx = None
    proto = "https" if ssl_ctx else "http"
    log.info("Explorer " + proto + "://" + EXPLORER_HOST + ":" + str(port))
    explorer_app.run(
        host=EXPLORER_HOST, port=port, threaded=True,
        debug=False, use_reloader=False, ssl_context=ssl_ctx,
    )


# ============================================================
# STATUS LOOP
# ============================================================
def run_status_loop(chain, p2p, sync=None, pool=None, pool_client=None):
    log = get_logger("status")
    while not _shutdown.is_set():
        time.sleep(30)
        try:
            peer_count = 0
            if p2p is not None:
                try:
                    st = p2p.get_status()
                    peer_count = st.get("peer_count", 0)
                except Exception:
                    pass

            mempool_n = len(chain.db.all_mempool(limit=1000))
            utxos_n = chain.db.count_utxos()

            msg = ("Altura=" + str(chain.db.height())
                   + " Peers=" + str(peer_count)
                   + " Mempool=" + str(mempool_n)
                   + " UTXOs=" + str(utxos_n))

            if sync is not None:
                try:
                    s = sync.get_status()
                    msg += (" Sync=" + str(s.get("progress", 0)) + "%")
                except Exception:
                    pass

            if pool_client is not None:
                try:
                    pcs = pool_client.status()
                    if pcs.get("running"):
                        msg += (" PoolClient["
                                + str(round(pcs.get("hashrate_khs", 0), 1))
                                + " kH/s]")
                except Exception:
                    pass

            log.info(msg)
        except Exception as e:
            log.warning("status loop: " + str(e))


def run_wallet_main_thread():
    try:
        if not os.environ.get("BRN_WEB_PASS"):
            get_logger("wallet").error("BRN_WEB_PASS não definida")
            return
        from app_wallet_v3 import WalletApi
        import webview
        log = get_logger("wallet")
        index_path = BASE_DIR / "index_wallet.html"
        if not index_path.exists():
            log.error("index_wallet.html não encontrado: " + str(index_path))
            return
        api = WalletApi()
        webview.create_window(
            "BRN RWA — Carteira Digital",
            url=index_path.resolve().as_uri(),
            js_api=api,
            width=1020, height=880,
            min_size=(820, 640),
            background_color="#0d1117",
        )
        webview.start(debug=False)
    except Exception as e:
        get_logger("wallet").error("Falha: " + str(e))


# ============================================================
# GRACEFUL SHUTDOWN
# ============================================================
def _graceful_shutdown(log):
    log.info("Encerrando servicos...")
    with _services_lock:
        services = list(_running_services)

    for name, svc in reversed(services):
        try:
            if hasattr(svc, "stop"):
                svc.stop()
            elif callable(svc):
                svc()
        except Exception as e:
            log.warning(f"Erro ao parar {name}: {e}")

    try:
        from wallet import WalletManager
        WalletManager.lock_session()
    except Exception:
        pass

    _shutdown.set()


# ============================================================
# MAIN
# ============================================================
def main():
    args = parse_args()
    cfg = Config(args.config)

    if args.read_only:
        cfg.data["read_only"] = True
    if args.headless or args.no_gui:
        cfg.data["headless"] = True
    if args.gui:
        cfg.data["headless"] = False
    if args.log_level:
        cfg.data["log_level"] = args.log_level
    if args.log_file:
        cfg.data["log_file"] = args.log_file

    cfg.apply_to_env()

    log = setup_logger(level=cfg["log_level"],
                       log_file=cfg["log_file"] or None)

    is_origin = _is_origin(args.client_mode)

    log.info("=" * 60)
    log.info(" BRN Node v" + VERSION + " (" + BUILD_DATE + ")")
    log.info(" Modo: " + ("ORIGEM" if is_origin else "CLIENTE"))
    log.info("=" * 60)

    if args.version:
        print(VERSION)
        return 0

    missing = _check_required_modules()
    if missing:
        log.error("Modulos obrigatorios faltando: " + ", ".join(missing))
        return 1

    # --- Resolve Senha e Identidade ---
    try:
        node_password = _resolve_password(args)
        node_sk, node_pub = load_or_create_node_identity(
            node_password, rotate=args.rotate_node_id
        )
    except SystemExit as e:
        log.error(str(e))
        return 1
    except Exception as e:
        log.exception("Falha ao carregar identidade: " + str(e))
        return 1

    # --- Inicializa a Blockchain ---
    from blockchain import Blockchain
    chain = Blockchain(
        cfg["db_path"],
        auto_genesis=is_origin,
        network_secret=cfg["network_secret"],
    )
    chain._fork_signal = threading.Event()

    if not args.skip_verify:
        status = _verify_chain(chain)
        if status == "invalid":
            log.error("Cadeia inválida. Abortando.")
            return 1

    if args.verify_only:
        log.info("Apenas verificação. Encerrando.")
        return 0

    # --- Inicializa o P2P (antes do HTTP, para já injetar) ---
    p2p = None
    if not args.no_p2p:
        try:
            from p2p_unified import P2PManager
            p2p = P2PManager(chain, node_sk, cfg["p2p_port"])
            p2p.start()
            _register_service("p2p", p2p)
            log.info(f"P2P iniciado na porta {cfg['p2p_port']}")
        except Exception as e:
            log.error("Falha ao iniciar P2P: " + str(e))
            p2p = None

    # --- Inicializa o Sync (antes do HTTP, para já injetar) ---
    sync = None
    if not args.no_sync and p2p is not None:
        try:
            from sync_manager import ChainSyncManager
            sync = ChainSyncManager(chain, chain.db, p2p)
            sync.start_sync_loop()
            _register_service("sync", sync)
            log.info("Sync iniciado")
        except Exception as e:
            log.warning("Falha ao iniciar Sync: " + str(e))
            sync = None

    # --- Inicializa o Pool (antes do HTTP, para já injetar) ---
    pool = None
    if not args.no_pool and POOL_ENABLED:
        try:
            pool = PoolManager(chain)
            _register_service("pool", pool)
            log.info("PoolManager iniciado")
        except Exception as e:
            log.warning("Falha ao iniciar PoolManager: " + str(e))
            pool = None

    # --- Inicializa o servidor HTTP (AGORA com p2p/sync/pool) ---
    http_app = _init_http_server(chain, log, p2p=p2p, sync=sync, pool=pool)
    if http_app is None:
        log.error("Falha ao inicializar servidor HTTP. Abortando.")
        return 1

    http_thread = threading.Thread(
        target=run_http, args=(http_app,), daemon=True, name="HTTP"
    )
    http_thread.start()
    _register_service("http", http_thread)

    # --- Inicia o PoolClient (auto-mineração do nó ORIGEM) ---
    pool_client = None
    if (pool is not None
            and not args.no_pool_client
            and is_origin
            and PoolClient is not None):
        try:
            pool_url = os.environ.get("BRN_POOL_URL_LOCAL",
                                      "http://127.0.0.1:5000")

            # pega endereço/pubkey da carteira ativa
            w = {"address": "", "pubkey": ""}
            try:
                from secure_store import load_current_wallet
                session_pass = os.environ.get("BRN_WALLET_SESSION_PASS", "")
                wd = load_current_wallet(session_pass)
                if wd.get("ok"):
                    w = {
                        "address": wd.get("address", ""),
                        "pubkey": (wd.get("public_key")
                                   or wd.get("pubkey")
                                   or wd.get("pub_hex")
                                   or ""),
                    }
            except Exception as e:
                log.warning(f"PoolClient: carteira ativa indisponível: {e}")

            if w.get("address") and w.get("pubkey"):
                pool_client = PoolClient(
                    address=w["address"],
                    pubkey=w["pubkey"],
                    pool_url=pool_url,
                )
                ok, msg = pool_client.start_mining()
                if ok:
                    _register_service("pool_client", pool_client)
                    log.info(f"PoolClient iniciado ({msg})")
                else:
                    log.warning(f"PoolClient start falhou: {msg}")
            else:
                log.info("PoolClient: sem carteira ativa — pulado")
        except Exception as e:
            log.warning("Falha ao iniciar PoolClient: " + str(e))
            pool_client = None

    # --- Inicia a Mineração Solo (se solicitado) ---
    if args.mine or AUTO_MINE:
        from miner_loop import get_miner
        miner = get_miner(chain)

        w = {"address": "", "pubkey": ""}
        try:
            from secure_store import load_current_wallet
            session_pass = os.environ.get("BRN_WALLET_SESSION_PASS", "")
            wd = load_current_wallet(session_pass)
            if wd.get("ok"):
                w = {
                    "address": wd.get("address", ""),
                    "pubkey": (wd.get("public_key")
                               or wd.get("pubkey")
                               or wd.get("pub_hex")
                               or ""),
                }
        except Exception:
            pass

        if w.get("address"):
            ok, msg = miner.start(w["address"], w.get("pubkey", ""))
            log.info(f"Miner auto-start: {msg}")
            _register_service("miner", miner)
        else:
            log.warning("Miner: sem carteira aberta para minerar.")

    # --- Inicia o Loop de Status ---
    status_thread = threading.Thread(
        target=run_status_loop,
        args=(chain, p2p, sync, pool, pool_client),
        daemon=True,
        name="StatusLoop",
    )
    status_thread.start()
    _register_service("status", status_thread)

    # --- Inicia a GUI ou mantém headless ---
    if not cfg["headless"]:
        log.info("Abrindo carteira desktop (pywebview)...")
        run_wallet_main_thread()
    else:
        log.info("Modo headless. Pressione Ctrl+C para encerrar.")
        try:
            while not _shutdown.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Encerrando por Ctrl+C...")

    _graceful_shutdown(log)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nEncerrado pelo usuário.")
        sys.exit(130)
