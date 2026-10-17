"""main.py — Entrypoint unificado do nó BRN (v12.10)
================================================================
v12.10:
  [FIX] Aceita --gui e --no-gui (do iniciar.bat v6).
        Antes o argparse abortava com 'unrecognized arguments'.
  [FIX] Wallet.create() não existe — usar Wallet() direto.
        Antes a miner wallet nunca era criada na 1ª execução.
  [NEW] Prioridade de flags: --no-gui > --gui > --headless > auto
v12.9:
  [FIX] Não arranca miner_loop local quando o pool está ativo
        — antes ambos competiam e causavam forks garantidos
v12.8:
  [FIX] Ligar chain.on_block_mined = p2p.broadcast_block
v12.7:
  [NEW] Pool de Mineração (mining_pool.PoolManager)
v12.6:
  [FIX] Injeta P2PManager no server.py (set_p2p_manager)
v12.5:
  [NEW] Sync manager + rendezvous + relay
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


def _thread_wrap(name: str, fn):
    def runner():
        try:
            fn()
        except Exception as e:
            try:
                get_logger(name).exception(
                    "thread " + name + " caiu: " + str(e))
            except Exception:
                pass
    return runner


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
# L2
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
    print("[AVISO] Ponte BTC e L2 DESATIVADAS — o nó continua normal.")


def _l2_disponivel() -> bool:
    return (L2_ENABLED
            and btc_config is not None
            and BTCWatcher is not None
            and L2Manager is not None)


# ============================================================
# POOL — import com fallback
# ============================================================
POOL_ENABLED = os.environ.get("BRN_POOL_ENABLED", "1") == "1"
PoolManager = None

if POOL_ENABLED:
    try:
        from mining_pool import PoolManager
        print("[Pool] Módulo de pool carregado OK")
    except Exception as _e:
        print("[AVISO] mining_pool indisponível: " + str(_e))
        print("[AVISO] Pool DESATIVADO — mineração local continua.")
        PoolManager = None


def _pool_disponivel() -> bool:
    return POOL_ENABLED and PoolManager is not None


# ============================================================
# AUDIT
# ============================================================
def _audit(event: str, data: dict = None) -> None:
    try:
        from security import audit_log
        audit_log(event, data or {})
    except Exception:
        pass


def _close_audit() -> None:
    try:
        from security import close_audit
        close_audit()
    except Exception:
        pass


# ============================================================
# TAILSCALE AUTO-SETUP
# ============================================================
def _setup_tailscale_auto(log) -> bool:
    auth_key = os.environ.get("TS_AUTH_KEY", "").strip()
    if not auth_key:
        log.info("Tailscale: desabilitado (TS_AUTH_KEY vazia)")
        return True

    setup_path = BASE_DIR / "brn_tailscale_setup.py"
    if not setup_path.exists():
        log.warning("Tailscale: brn_tailscale_setup.py não encontrado — pulando")
        return True

    log.info("Tailscale: configurando automaticamente...")
    try:
        r = subprocess.run(
            [sys.executable, str(setup_path), "--auto"],
            capture_output=True, text=True, timeout=180,
            cwd=str(BASE_DIR),
        )
        if r.returncode == 0:
            log.info("Tailscale: OK")
            for line in (r.stdout or "").splitlines()[-6:]:
                if line.strip():
                    log.info("  " + line)
            return True
        log.warning("Tailscale: falhou (código " + str(r.returncode) + ")")
        return False
    except subprocess.TimeoutExpired:
        log.warning("Tailscale: timeout de 180s — continuando sem VPN")
        return False
    except Exception as e:
        log.warning("Tailscale: erro — " + str(e))
        return False


# ============================================================
# ARGS  [FIX v12.10] --gui / --no-gui
# ============================================================
def parse_args():
    p = argparse.ArgumentParser(prog="main.py", description="BRN Node v12")
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

    # [FIX v12.10] GUI: aceitar --gui / --no-gui (do iniciar.bat v6)
    gui_group = p.add_mutually_exclusive_group()
    gui_group.add_argument("--gui", action="store_true",
                           help="Força modo GUI (pywebview)")
    gui_group.add_argument("--no-gui", action="store_true",
                           help="Força modo headless (sem pywebview)")

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
VERIFY_OK      = "ok"
VERIFY_INVALID = "invalid"
VERIFY_ERROR   = "error"


def _verify_chain(chain) -> str:
    try:
        from chain_validator import verify_chain
    except ImportError:
        get_logger("verify").info("chain_validator.py não disponível")
        return VERIFY_ERROR

    log = get_logger("verify")
    log.info("Verificando integridade da cadeia...")
    t0 = time.time()
    try:
        r = verify_chain(chain)
    except Exception as e:
        log.exception("validador falhou: " + str(e))
        return VERIFY_ERROR

    dt = time.time() - t0

    valid = getattr(r, "valid", None)
    if valid is None:
        valid = bool(r)
    summary_fn = getattr(r, "summary", None)
    blocks = getattr(r, "blocks_checked", 0)
    cps = getattr(r, "checkpoints_checked", 0)
    warns = getattr(r, "warnings", []) or []
    errs = getattr(r, "errors", []) or []

    if valid:
        if callable(summary_fn):
            log.info(f"OK {summary_fn()} ({dt:.2f}s)")
        else:
            log.info(f"OK cadeia íntegra ({dt:.2f}s, {blocks} blocos)")
        for w in warns[:5]:
            log.warning("  " + str(w))
        return VERIFY_OK

    log.error(f"CADEIA INVÁLIDA ({len(errs)} erros)")
    for e in errs[:10]:
        log.error("  - " + str(e))
    return VERIFY_INVALID


# ============================================================
# VERSION / UPDATE
# ============================================================
def _parse_version(s):
    try:
        return tuple(int(p) for p in s.strip().lstrip("v").split(".")[:3])
    except Exception:
        return (0, 0, 0)


def check_for_update(timeout: int = 5):
    url = ("https://raw.githubusercontent.com/"
           + GITHUB_USER + "/" + GITHUB_REPO + "/main/version.json")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brn-node"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        remote = data.get("version", "0.0.0")
        return {
            "ok": True, "local": VERSION, "remote": remote,
            "update_available": _parse_version(remote) > _parse_version(VERSION),
            "url": data.get("url", ""), "notes": data.get("notes", ""),
        }
    except Exception as e:
        return {"ok": False, "local": VERSION, "error": str(e)}


# ============================================================
# DIAGNÓSTICO
# ============================================================
def do_status(cfg):
    print("=" * 64)
    print(" BRN Node — Diagnóstico (v" + VERSION + ")")
    print("=" * 64)
    print(" Versão : " + VERSION + " (" + BUILD_DATE + ")")
    print(" Config : " + cfg.source)
    print(" Web    : " + WEB_HOST + ":" + str(cfg["web_port"]))
    print(" Expl.  : " + EXPLORER_HOST + ":" + str(cfg["explorer_port"]))
    print(" P2P    : " + str(cfg["p2p_port"]))
    print()

    https = os.environ.get("BRN_HTTPS", "0") == "1"
    audit = os.environ.get("BRN_AUDIT_ENABLED", "1") == "1"
    rdv   = os.environ.get("BRN_RENDEZVOUS_URL", "").strip()
    ts    = os.environ.get("TS_TAILNET", "").strip()

    print(" HTTPS       : " + ("SIM" if https else "não"))
    print(" Audit log   : " + ("SIM" if audit else "não"))
    print(" Rendezvous  : " + (rdv if rdv else "(não configurado)"))
    print(" Tailscale   : " + (ts if ts else "(desabilitado)"))
    print(" Auto-mine   : " + ("SIM" if AUTO_MINE else "não"))
    print(" Pool        : " + ("SIM" if _pool_disponivel() else "não"))
    print()

    missing = _check_required_modules()
    if missing:
        print(" [!] Módulos faltando: " + ", ".join(missing))
    else:
        print(" Módulos     : OK")
    print()

    if _l2_disponivel():
        try:
            print(" L2 Enabled  : SIM")
            print(" BTC Addr    : " + str(getattr(btc_config, "BTC_RECEIVE_ADDRESS", "?")))
        except Exception as e:
            print(" L2 Enabled  : SIM (erro: " + str(e) + ")")
    else:
        print(" L2 Enabled  : NÃO")

    try:
        from checkpoints import load_checkpoints
        cps = load_checkpoints()
        print(" Checkpoints : " + str(len(cps)) + " carregados")
    except Exception as e:
        print(" Checkpoints : indisponível (" + str(e) + ")")

    origin = _is_origin(False)
    print(" Role (auto) : " + ("ORIGEM" if origin else "CLIENTE"))

    db_path = cfg["db_path"]
    print(" DB path     : " + str(db_path))
    if os.path.exists(db_path):
        size_kb = os.path.getsize(db_path) / 1024
        print(" DB size     : " + str(round(size_kb, 1)) + " KB")
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT MAX(height) FROM blocks").fetchone()
            h = row[0] if row else None
            print(" DB height   : " + str(h))
            conn.close()
        except Exception as e:
            print(" DB height   : erro (" + str(e) + ")")

    try:
        from mongo_client import mongo
        ok, err = mongo.ping()
        if ok:
            print(" MongoDB     : OK")
        else:
            print(" MongoDB     : off (" + (err if err else "não configurado") + ")")
    except Exception:
        print(" MongoDB     : pymongo indisponível")

    print("=" * 64)


def _do_discover_diagnostic(cfg):
    print("=" * 64)
    print(" BRN Discover Diagnostic")
    print("=" * 64)
    try:
        from discovery_v2 import UDPDiscovery, get_all_local_ips
    except ImportError as e:
        print(" discovery_v2 indisponível: " + str(e))
        return
    print()
    print(" IPs locais: " + str(get_all_local_ips()))
    print()
    print(" Escutando 15s...")
    found = []

    def on_peer(ip, port):
        found.append(ip + ":" + str(port))
        print(" [achou] " + ip + ":" + str(port))

    stop = [False]

    try:
        udp = UDPDiscovery(cfg["p2p_port"], "diag", on_peer,
                           lambda: not stop[0])
        udp.start()
        time.sleep(15)
        stop[0] = True
        udp.stop()
    except Exception as e:
        print(" falha: " + str(e))
    print()
    print(" Resultado: " + str(len(found)) + " peer(s)")
    print("=" * 64)


# ============================================================
# HTTP / EXPLORER
# ============================================================
def _init_http_server(chain, log):
    try:
        from server import (
            app as http_app,
            set_chain,
            auto_registrar_carteira_atual,
        )
    except Exception as e:
        log.error("Falha importando server.py: " + str(e))
        return None

    try:
        set_chain(chain)
    except Exception as e:
        log.error("Falha set_chain: " + str(e))
        return None

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
def run_status_loop(chain, p2p, l2_manager=None, sync=None, pool=None):
    log = get_logger("status")
    if p2p is None:
        log.warning("P2P desabilitado — status loop sem peers")

    mongo = None
    try:
        from mongo_client import mongo as _m
        mongo = _m
    except Exception:
        pass

    while not _shutdown.is_set():
        time.sleep(30)
        try:
            peer_count = 0
            relay_routes = 0
            if p2p is not None:
                try:
                    st = p2p.get_status()
                    peer_count = st.get("peer_count", 0)
                    relay = getattr(p2p, "relay", None)
                    if relay:
                        relay_routes = len(relay.routes)
                except Exception as e:
                    log.warning("p2p.get_status: " + str(e))

            mempool_n = len(chain.db.all_mempool(limit=1000))
            utxos_n = chain.db.count_utxos()

            msg = ("Altura=" + str(chain.db.height())
                   + " Peers=" + str(peer_count)
                   + " Relay=" + str(relay_routes)
                   + " Mempool=" + str(mempool_n)
                   + " UTXOs=" + str(utxos_n))

            if sync is not None:
                try:
                    s = sync.get_status()
                    msg += (" Sync=" + str(s.get("mode", "?"))
                            + "/" + str(s.get("local_height", 0))
                            + "→" + str(s.get("target_height", 0))
                            + " (" + str(s.get("progress", 0)) + "%)")
                    if s.get("active_peer"):
                        msg += " via " + str(s["active_peer"])[:12]
                    if s.get("last_error"):
                        msg += " err=" + str(s["last_error"])[:40]
                except Exception:
                    pass

            if l2_manager is not None:
                try:
                    s = l2_manager.stats()
                    msg += (" L2[OPEN=" + str(s.get("open", 0))
                            + " DETECTED=" + str(s.get("btc_detected", 0))
                            + " RELEASED=" + str(s.get("released", 0)) + "]")
                except Exception:
                    pass

            if pool is not None:
                try:
                    ps = pool.stats()
                    msg += (" Pool[miners=" + str(ps.get("miners_active", 0))
                            + " hash=" + str(round(ps.get("hashrate_total", 0), 1))
                            + " H/s"
                            + " shares=" + str(ps.get("shares_period", 0))
                            + " blocks=" + str(ps.get("blocks_found", 0)) + "]")
                except Exception:
                    pass

            if mongo is not None and getattr(mongo, "uri", None):
                ok, err = mongo.ping()
                msg += " Mongo=" + ("OK" if ok else "off")

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
# RELAY SETUP
# ============================================================
def _setup_relay(p2p, node_id_priv, enabled):
    if enabled is False:
        return None
    if p2p is None:
        return None

    try:
        from p2p_relay import RelayManager
    except ImportError as e:
        get_logger("relay").info("p2p_relay indisponível: " + str(e))
        return None

    try:
        relay = RelayManager(p2p, node_id_priv)
    except Exception as e:
        get_logger("relay").warning("RelayManager falhou: " + str(e))
        return None

    srv = getattr(p2p, "server", None)
    if srv is None:
        return relay

    def _caps_handler(ip, peer_id, msg):
        if not peer_id:
            return
        verified = False
        try:
            from p2p_auth import verify_auth, auth_enabled
            if auth_enabled():
                a = msg.get("_auth")
                if a:
                    payload_sem = {k: v for k, v in msg.items() if k != "_auth"}
                    ok, _ = verify_auth(a, msg=payload_sem)
                    verified = ok
        except Exception:
            pass
        relay.register_capability(
            peer_id,
            {"can_relay": msg.get("can_relay", False),
             "bandwidth_tier": msg.get("bandwidth_tier", "unknown")},
            verified=verified,
        )

    if hasattr(srv, "on_relay_caps"):
        srv.on_relay_caps = _caps_handler
    if hasattr(srv, "on_relay_request"):
        srv.on_relay_request = relay.handle_relay_request
    if hasattr(srv, "on_relay_incoming"):
        srv.on_relay_incoming = relay.handle_incoming_relay
    if hasattr(srv, "on_relay_accepted"):
        srv.on_relay_accepted = lambda ip, pid, msg: relay.handle_route_accepted(msg)
    if hasattr(srv, "on_relay_data"):
        srv.on_relay_data = relay.handle_relay_data

    return relay


# ============================================================
# SYNC SETUP
# ============================================================
def _list_p2p_peers(p2p):
    out = []

    def _pick(v, *names):
        for n in names:
            if isinstance(v, dict):
                if n in v and v[n] is not None:
                    return v[n]
            else:
                val = getattr(v, n, None)
                if val is not None:
                    return val
        return None

    try:
        peers = getattr(p2p, "peers", None)
        items = []
        if isinstance(peers, dict):
            items = list(peers.items())
        elif isinstance(peers, list):
            items = [(None, v) for v in peers]

        for node_id, v in items:
            ip    = _pick(v, "ip", "address", "host")
            port  = _pick(v, "port")
            nid   = node_id or _pick(v, "node_id", "uuid", "id") or ""
            height = _pick(v, "height", "h") or 0
            work   = _pick(v, "cumulative_work", "work") or 0
            if ip and port:
                try:
                    out.append((str(ip), int(port), str(nid),
                                int(height), int(work)))
                except (ValueError, TypeError):
                    pass
        if out:
            return out
    except Exception:
        pass

    fn = getattr(p2p, "listar_peers", None)
    if callable(fn):
        try:
            raw = fn()
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, str) and ":" in item:
                        ip, _, port_s = item.rpartition(":")
                        try:
                            out.append((ip, int(port_s), "", 0, 0))
                        except ValueError:
                            pass
                    elif isinstance(item, dict):
                        ip = item.get("ip")
                        port = item.get("port")
                        if ip and port:
                            out.append((str(ip), int(port),
                                        str(item.get("node_id", "")),
                                        int(item.get("height", 0) or 0),
                                        int(item.get("cumulative_work", 0) or 0)))
        except Exception:
            pass
    return out


def _setup_sync(chain, p2p, log):
    if p2p is None:
        log.info("Sync: P2P desligado — sem sync")
        return None

    try:
        from sync_manager import ChainSyncManager
    except ImportError as e:
        log.warning("sync_manager indisponível: " + str(e))
        return None

    try:
        sync = ChainSyncManager(chain, chain.db, p2p)
    except Exception as e:
        log.warning("ChainSyncManager falhou: " + str(e))
        return None

    n_inicial
