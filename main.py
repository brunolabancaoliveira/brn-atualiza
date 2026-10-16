"""main.py — Entrypoint unificado do nó BRN (v12.7)
================================================================
v12.7:
  [NEW] Pool de Mineração (mining_pool.PoolManager)
        — Injetado no server.py via set_pool_manager()
        — Callback on_block_mined → broadcast P2P
v12.6:
  [FIX] Injeta P2PManager no server.py (set_p2p_manager)
v12.5:
  [NEW] Sync manager + rendezvous + relay
================================================================
Amarra tudo:
  1. Config (brn_config.Config) + env
  2. Senha do nó
  3. Identidade Ed25519 (node_identity.enc)
  4. Blockchain (blockchain.py v9.1.8)
  5. P2P (p2p_unified.P2PManager)
  6. Sync (sync_manager.ChainSyncManager)
  7. Rendezvous (rendezvous_discovery.RendezvousDiscovery)
  8. Relay (p2p_relay.RelayManager)
  9. L2 (btc_watcher.BTCWatcher)
 10. Pool de Mineração (mining_pool.PoolManager)
 11. HTTP server (server.py v9.9)
 12. Explorer (explorer.py)
 13. Miner local (miner_loop.get_miner)
 14. Loop principal até Ctrl+C
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

    # Pool
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
# L2 — fallback
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
# ARGS
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

    n_inicial = 0
    for ip, port, node_id, height, work in _list_p2p_peers(p2p):
        try:
            sync.register_peer(node_id or f"{ip}:{port}",
                               ip, port, height, work)
            n_inicial += 1
        except Exception as e:
            log.debug("sync.register_peer falhou: " + str(e))
    log.info("Sync: " + str(n_inicial) + " peer(s) registrados inicialmente")

    def _on_peer_found(node_id, ip, port, height=0, work=0):
        try:
            sync.register_peer(node_id or f"{ip}:{port}",
                               ip, port, height, work)
        except Exception:
            pass

    for hook in ("on_peer_added", "on_peer"):
        if hasattr(p2p, hook):
            try:
                setattr(p2p, hook, _on_peer_found)
                log.info("Sync: gancho " + hook + " ligado")
                break
            except Exception:
                pass

    try:
        sync.set_sync_mode("full")
        sync.start_sync_loop()
        log.info("Sync iniciado (full mode, batch="
                 + str(getattr(sync, "batch_size", "?")) + ")")
    except Exception as e:
        log.warning("sync.start_sync_loop falhou: " + str(e))
        return sync

    return sync


# ============================================================
# POOL SETUP
# ============================================================
def _setup_pool(chain, p2p, log, disabled: bool):
    """
    Cria o PoolManager e liga o callback de broadcast de bloco.
    Retorna a instância ou None.
    """
    if disabled:
        log.info("Pool: desativado por --no-pool")
        return None
    if not _pool_disponivel():
        log.info("Pool: módulo mining_pool indisponível")
        return None

    try:
        pool = PoolManager(chain)
    except Exception as e:
        log.warning("PoolManager falhou: " + str(e))
        return None

    # Callback: quando o pool encontra um bloco, propaga via P2P
    if p2p is not None:
        def _on_pool_block(block):
            try:
                p2p.broadcast_block(block)
                log.info("pool: bloco #" + str(block.get("height"))
                         + " propagado via P2P")
            except Exception as e:
                log.debug("pool: broadcast_block falhou: " + str(e))

        try:
            pool.on_block_mined = _on_pool_block
        except Exception:
            pass

    log.info("Pool: pronto (share_diff_offset="
             + str(getattr(__import__("mining_pool"), "SHARE_DIFF_OFFSET", "?"))
             + ")")
    return pool


# ============================================================
# SHUTDOWN
# ============================================================
def _stop_with_timeout(name: str, svc, timeout: float = 5.0, log=None) -> None:
    try:
        if hasattr(svc, "stop"):
            fn = svc.stop
        elif callable(svc):
            fn = svc
        else:
            return
    except Exception:
        return

    t = threading.Thread(target=fn, daemon=True, name="stop-" + name)
    t.start()
    t.join(timeout)
    if t.is_alive() and log is not None:
        log.warning("Serviço " + name + " não parou em " + str(timeout) + "s")


def _graceful_shutdown(log) -> None:
    log.info("Encerrando serviços...")
    with _services_lock:
        services = list(_running_services)

    for name, svc in reversed(services):
        _stop_with_timeout(name, svc, timeout=5.0, log=log)

    try:
        from wallet import WalletManager
        WalletManager.lock_session()
    except Exception:
        pass

    try:
        from mongo_client import mongo
        mongo.close()
    except Exception:
        pass

    _audit("node_stop", {})
    _close_audit()


# ============================================================
# MAIN
# ============================================================
def main():
    args = parse_args()
    cfg = Config(args.config)

    if args.read_only:
        cfg.data["read_only"] = True
    if args.headless:
        cfg.data["headless"] = True
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
    if args.l2 and _l2_disponivel():
        l2_mode = "ON"
    elif not args.l2:
        l2_mode = "off"
    else:
        l2_mode = "INDISPONÍVEL"
    pool_mode = "off" if args.no_pool else ("ON" if _pool_disponivel() else "off")
    log.info(" Config: " + cfg.source
             + " | Modo: " + ("ORIGEM" if is_origin else "CLIENTE")
             + " | L2: " + l2_mode
             + " | Pool: " + pool_mode)
    log.info(" HTTPS: " + ("ON" if os.environ.get("BRN_HTTPS") == "1" else "off")
             + " | Rendezvous: " + (os.environ.get("BRN_RENDEZVOUS_URL", "—") or "—")
             + " | Auto-mine: " + ("ON" if AUTO_MINE else "off"))
    log.info(" Tailscale: " + (os.environ.get("TS_TAILNET", "").strip() or "desabilitado"))
    log.info("=" * 60)

    # ---- comandos que saem sozinhos ----
    if args.version:
        print(VERSION)
        return 0
    if args.status:
        do_status(cfg)
        return 0
    if args.discover:
        _do_discover_diagnostic(cfg)
        return 0
    if args.check_update:
        print(check_for_update())
        return 0
    if args.l2_stats:
        if not _l2_disponivel():
            print("L2 não disponível")
            return 0
        try:
            from db import ChainDB
            db = ChainDB(cfg["db_path"])
            print(json.dumps(L2Manager(db).stats(), indent=2))
        except Exception as e:
            print("Erro: " + str(e))
        return 0
    if args.pool_stats:
        if not _pool_disponivel():
            print("Pool não disponível")
            return 0
        try:
            from db import ChainDB
            from blockchain import Blockchain
            bc = Blockchain(cfg["db_path"], auto_genesis=False)
            pool = PoolManager(bc)
            print(json.dumps(pool.stats(), indent=2, ensure_ascii=False))
            pool.stop()
            bc.db.close()
        except Exception as e:
            print("Erro: " + str(e))
        return 0

    # ---- módulos obrigatórios ----
    missing = _check_required_modules()
    if missing:
        log.error("Módulos obrigatórios faltando: " + ", ".join(missing))
        return 1

    # ---- Tailscale ----
    if not args.no_tailscale:
        _setup_tailscale_auto(log)
    else:
        log.info("Tailscale: pulado por --no-tailscale")

    # ---- identidade ----
    node_password = _resolve_password(args)
    try:
        node_id_priv, node_id_pub = load_or_create_node_identity(
            node_password, rotate=args.rotate_node_id)
    except SystemExit:
        raise
    log.info("Nó ID: " + node_id_pub)

    # ---- blockchain ----
    from blockchain import Blockchain
    chain = Blockchain(cfg["db_path"], auto_genesis=is_origin)
    log.info("Altura atual: " + str(chain.db.height()))

    # ---- checkpoints ----
    try:
        from checkpoints import load_checkpoints
        from blockchain import set_checkpoints, set_checkpoint_priv
        cps = load_checkpoints()
        set_checkpoints(cps)
        if is_origin and node_id_priv is not None:
            set_checkpoint_priv(node_id_priv)
            log.info("Checkpoints: " + str(len(cps)) + " carregados (assinatura ATIVA)")
        else:
            log.info("Checkpoints: " + str(len(cps)) + " carregados (só valida)")
    except Exception as e:
        log.warning("Checkpoints desativados: " + str(e))

    # ---- verify ----
    if args.verify_only:
        st = _verify_chain(chain)
        try:
            chain.db.close()
        except Exception:
            pass
        return 0 if st == VERIFY_OK else 2

    if not args.skip_verify and os.environ.get("BRN_SKIP_VERIFY", "0") != "1":
        strict = os.environ.get("BRN_VERIFY_STRICT", "0") == "1"
        st = _verify_chain(chain)
        if st == VERIFY_INVALID and strict:
            log.error("Abortando boot: cadeia inválida (BRN_VERIFY_STRICT=1)")
            try:
                chain.db.close()
            except Exception:
                pass
            return 2
        if st == VERIFY_ERROR:
            log.warning("Verificação não conclusiva — seguindo com cautela")

    # ---- P2P ----
    p2p = None
    if not args.no_p2p:
        try:
            from p2p_unified import P2PManager
            p2p = P2PManager(chain, node_id_priv,
                             tcp_port=cfg["p2p_port"],
                             enable_upnp=cfg["upnp"])
            p2p.start()
            _register_service("p2p", p2p)
            log.info("P2P iniciado")
        except Exception as e:
            log.error("Falha ao iniciar P2P: " + str(e))
            p2p = None

    # ---- Relay ----
    relay = None
    if p2p is not None:
        relay = _setup_relay(p2p, node_id_priv, args.relay)
        if relay:
            try:
                p2p.relay = relay
            except Exception:
                pass
            _register_service("relay", relay)
            log.info("Relay E2E pronto")

    # ---- Sync ----
    sync = None
    if not args.no_sync:
        sync = _setup_sync(chain, p2p, log)
        if sync:
            _register_service("sync", sync)
    else:
        log.info("Sync: pulado por --no-sync")

    # ---- Rendezvous ----
    rdv = None
    rdv_url = os.environ.get("BRN_RENDEZVOUS_URL", "").strip()
    if p2p is not None:
        try:
            from rendezvous_discovery import RendezvousDiscovery
            rdv = RendezvousDiscovery(
                tcp_port=cfg["p2p_port"],
                node_uuid=node_id_pub,
                on_peer=p2p.add_peer,
                running_flag=lambda: not _shutdown.is_set(),
                sign_fn=node_id_priv.sign,
                blockchain=chain,
            )
            rdv.start()
            if rdv.is_active():
                _register_service("rendezvous", rdv)
                log.info("Rendezvous ativo (backend=" + rdv.backend_name() + ")")
            else:
                log.info("Rendezvous: nenhum backend disponivel (no-op)")
        except Exception as e:
            log.warning("Rendezvous falhou: " + str(e))

    # ---- L2 ----
    l2_manager = None
    btc_watcher = None
    l2_ativo = False

    if _l2_disponivel():
        try:
            l2_manager = L2Manager(chain.db, chain)
            l2_ativo = True
            log.info("L2Manager carregado")
        except Exception as e:
            log.warning("L2Manager falhou: " + str(e))

    if args.l2 and l2_ativo:
        try:
            btc_addr = getattr(btc_config, "BTC_RECEIVE_ADDRESS", "")
            if not btc_addr or btc_addr == "bc1qSEU_ENDERECO_AQUI_TROQUE_ISSO":
                log.error("Configure BTC_RECEIVE_ADDRESS em btc_config.py")
            else:
                btc_watcher = BTCWatcher(chain.db, chain)
                btc_watcher.start()
                _register_service("btc_watcher", btc_watcher)
                log.info("L2 Watcher RPC iniciado em " + btc_addr)
        except Exception as e:
            log.warning("Falha ao iniciar BTCWatcher: " + str(e))
            btc_watcher = None
    elif args.l2:
        log.warning("--l2 solicitado, mas L2 não disponível. Ignorando.")

    # ---- [NEW v12.7] Pool de mineração ----
    pool = _setup_pool(chain, p2p, log, disabled=args.no_pool)
    if pool is not None:
        _register_service("pool", pool)

    # ---- Injeta P2PManager no server.py ----
    if p2p is not None:
        try:
            import server as _srv
            if hasattr(_srv, "set_p2p_manager"):
                _srv.set_p2p_manager(p2p)
                log.info("P2PManager injetado no server.py (propagação ativa)")
            else:
                log.warning("server.py sem set_p2p_manager — "
                            "tx nao serao propagadas via P2P")
        except Exception as e:
            log.warning("Falha injetando P2P no server: " + str(e))
    else:
        log.warning("Sem P2PManager — tx nao serao propagadas")

    # ---- [NEW v12.7] Injeta PoolManager no server.py ----
    if pool is not None:
        try:
            import server as _srv
            if hasattr(_srv, "set_pool_manager"):
                _srv.set_pool_manager(pool)
                log.info("PoolManager injetado no server.py")
            else:
                log.warning("server.py sem set_pool_manager — "
                            "endpoints /api/pool/* indisponiveis")
        except Exception as e:
            log.warning("Falha injetando Pool no server: " + str(e))
    else:
        log.info("Pool desativado — endpoints /api/pool/* indisponiveis")

    # ---- HTTP ----
    http_app = _init_http_server(chain, log)
    if http_app is not None:
        threading.Thread(
            target=_thread_wrap("HTTP", lambda: run_http(http_app)),
            daemon=True, name="HTTP",
        ).start()
    else:
        log.error("HTTP server não pôde ser inicializado — seguindo sem API")

    # ---- Explorer ----
    threading.Thread(target=_thread_wrap("Explorer", run_explorer),
                     daemon=True, name="Explorer").start()

    # ---- Miner local (modo clássico --mine) ----
    if args.mine or AUTO_MINE:
        try:
            from miner_loop import get_miner
            from wallet import Wallet
            from secure_store import save_wallet, load_wallet

            miner = get_miner(chain)
            miner_wallet_path = BASE_DIR / "miner_wallet.enc"

            mw = None
            try:
                data = load_wallet(node_password, str(miner_wallet_path))
                if data and hasattr(Wallet, "from_dict"):
                    mw = Wallet.from_dict(data)
            except Exception:
                mw = None

            if mw is None:
                mw = Wallet.create()
                try:
                    if hasattr(mw, "to_dict"):
                        save_wallet(mw.to_dict(), node_password,
                                    str(miner_wallet_path))
                        log.info("Miner wallet criada em " + str(miner_wallet_path))
                except Exception as e:
                    log.warning("Falha ao salvar miner wallet: " + str(e))

            pub = (getattr(mw, "pub_hex", None)
                   or getattr(mw, "pubkey_hex", "")
                   or "")
            if not pub:
                log.error("Miner wallet sem pub_hex/pubkey_hex — abortando miner")
            else:
                miner.start(mw.address, pub)
                _register_service("miner", miner)
                log.info("Miner local iniciado com " + mw.address)
        except Exception as e:
            log.warning("Miner local falhou: " + str(e))

    # ---- Status loop ----
    threading.Thread(
        target=_thread_wrap(
            "StatusLoop",
            lambda: run_status_loop(chain, p2p, l2_manager, sync, pool),
        ),
        daemon=True, name="StatusLoop",
    ).start()

    # ---- Cliente aguarda genesis ----
    if not is_origin and chain.db.height() < 0:
        log.info("Aguardando genesis (timeout " + str(CLIENT_BOOT_TIMEOUT) + "s)...")
        t0 = time.time()
        while chain.db.height() < 0 and (time.time() - t0) < CLIENT_BOOT_TIMEOUT:
            if _shutdown.is_set():
                break
            time.sleep(1)

    # ---- AUDIT ----
    _audit("node_start", {
        "mode":       "origin" if is_origin else "client",
        "l2":         bool(args.l2 and l2_ativo),
        "mine":       bool(args.mine or AUTO_MINE),
        "pool":       bool(pool),
        "https":      os.environ.get("BRN_HTTPS") == "1",
        "height":     chain.db.height(),
        "node_id":    node_id_pub[:16],
        "rendezvous": bool(rdv and rdv.is_active()),
        "relay":      bool(relay),
        "sync":       bool(sync),
        "p2p":        bool(p2p),
        "tailscale":  bool(os.environ.get("TS_TAILNET", "").strip()),
    })

    log.info("Nó pronto. Ctrl+C para encerrar.")

    # ---- UI / loop principal ----
    try:
        if not cfg["headless"]:
            run_wallet_main_thread()
        else:
            while not _shutdown.is_set():
                time.sleep(1)
    except KeyboardInterrupt:
        pass

    # ---- shutdown ----
    log.info("Encerrando...")
    _shutdown.set()
    _graceful_shutdown(log)

    try:
        chain.db.close()
    except Exception:
        pass

    try:
        del node_password
    except Exception:
        pass

    return 0


# ============================================================
# SIGNAL HANDLERS
# ============================================================
_signal_count = 0


def _on_signal(signum, frame):
    global _signal_count
    _signal_count += 1
    if _signal_count >= 2:
        print("\n[!] Forçando saída.", file=sys.stderr)
        os._exit(1)
    _shutdown.set()


if __name__ == "__main__":
    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _on_signal)

    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        try:
            log = get_logger("main")
            log.exception("Erro fatal: " + str(e))
            try:
                _graceful_shutdown(log)
            except Exception:
                pass
        except Exception:
            print("Erro fatal: " + str(e), file=sys.stderr)
        sys.exit(1)
