"""
explorer.py — Explorador de blocos BRN (Flask) v9.4
================================================================
- Compatível com db.py v6.3, security.py v1, mongo_client (opcional)
- Não quebra se pymongo não estiver instalado
- Rate limit por endpoint via security.require_rate_limit
- Audit log em rendezvous
- Conexão SQLite por thread (evita lock exclusivo por request)
- Endpoints de rendezvous para descoberta P2P via hub

v9.4 (13/10/2026):
  [NEW] /panel + /panel.html  → serve painel de controle HTML
  [NEW] /api/chain-info-proxy     → proxy para server.py :5000
  [NEW] /api/sync-status-proxy    → proxy para server.py :5000
  [NEW] /api/miner-status-proxy   → proxy para server.py :5000
  [NEW] /api/status-proxy         → proxy para server.py :5000
  [NEW] /api/peers-score-proxy    → proxy para server.py :5000
  [NEW] /api/portfolio-proxy/<addr> → proxy para server.py :5000
  [CHANGE] CORS habilitado para todas as origens locais
================================================================
"""
from __future__ import annotations

import os
import re
import time
import atexit
import logging
import threading
from pathlib import Path

from flask import Flask, jsonify, send_from_directory, request, abort

try:
    from flask_cors import CORS
    _HAS_CORS = True
except ImportError:
    _HAS_CORS = False

try:
    from werkzeug.middleware.proxy_fix import ProxyFix
    _HAS_PROXYFIX = True
except ImportError:
    _HAS_PROXYFIX = False

from db import ChainDB

# mongo_client é opcional
try:
    from mongo_client import mongo
    _HAS_MONGO = True
except Exception:
    mongo = None
    _HAS_MONGO = False

# security.py — usa se estiver disponível
try:
    from security import (
        require_rate_limit, audit_log, close_audit,
        hash_short, get_client_ip,
    )
    _HAS_SECURITY = True
except Exception:
    _HAS_SECURITY = False

    def require_rate_limit(name):
        def deco(fn):
            return fn
        return deco

    def audit_log(event, data=None):
        pass

    def close_audit():
        pass

    def hash_short(v, n=12):
        return str(v)[:n] if v else ""

    def get_client_ip():
        return request.remote_addr or "0.0.0.0"


# ============================================================
# Config
# ============================================================
BASE_DIR     = Path(__file__).resolve().parent
DB_PATH      = os.environ.get("BRN_DB", "brn_v2_chain.db")
PORT         = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
HOST         = os.environ.get("BRN_EXPLORER_HOST", "127.0.0.1")
CORS_ORIGIN  = os.environ.get("BRN_CORS_ORIGIN", "").strip()
BEHIND_PROXY = os.environ.get("BRN_BEHIND_PROXY", "0") == "1"
VERIFY_TTL   = int(os.environ.get("BRN_VERIFY_TTL", "300"))

# Porta onde o server.py está rodando (para os proxies)
SERVER_PORT  = int(os.environ.get("BRN_WEB_PORT", "5000"))
SERVER_HOST  = os.environ.get("BRN_PROXY_HOST", "127.0.0.1")

BRN_ADDRESS_RE = re.compile(r"^brn1[a-z0-9]{20,90}$")

logging.basicConfig(
    level=os.environ.get("BRN_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("explorer")


# ============================================================
# Flask app
# ============================================================
app = Flask(__name__, static_folder=str(BASE_DIR))

# CORS em tudo (v9.4)
if _HAS_CORS:
    if CORS_ORIGIN:
        CORS(app, origins=[CORS_ORIGIN])
    else:
        # Libera para localhost e Tailscale (100.x.x.x)
        CORS(app, origins="*")


@app.before_request
def _before():
    if request.method == "OPTIONS":
        return ("", 200)


@app.after_request
def _after(resp):
    # CORS legado (caso flask-cors não esteja)
    if CORS_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"] = CORS_ORIGIN
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Vary"] = "Origin"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# ============================================================
# Error handlers
# ============================================================
@app.errorhandler(400)
def _e400(_):
    return jsonify({"error": "bad request"}), 400


@app.errorhandler(404)
def _e404(_):
    return jsonify({"error": "not found"}), 404


@app.errorhandler(429)
def _e429(_):
    return jsonify({"error": "rate limit exceeded"}), 429


@app.errorhandler(500)
def _e500(e):
    log.exception("erro interno: %s", e)
    return jsonify({"error": "internal error"}), 500


@app.errorhandler(Exception)
def _eany(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description}), e.code
    log.exception("excecao: %s", e)
    return jsonify({"error": "internal error"}), 500


# ============================================================
# DB — conexão por thread
# ============================================================
_local = threading.local()


def _db() -> ChainDB:
    db = getattr(_local, "db", None)
    if db is None:
        db = ChainDB(DB_PATH)
        _local.db = db
    return db


# ============================================================
# Helpers
# ============================================================
def _parse_int(name, default, lo, hi):
    raw = request.args.get(name, default)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        abort(400)
    if v < lo or v > hi:
        abort(400)
    return v


def _valid_address(addr):
    return bool(BRN_ADDRESS_RE.match(addr or ""))


# ============================================================
# Rotas — estáticas
# ============================================================
@app.route("/")
def index():
    try:
        return send_from_directory(BASE_DIR, "index.html", max_age=60)
    except Exception:
        return jsonify({"ok": True, "service": "BRN Explorer"})


@app.route("/panel")
@app.route("/panel.html")
def panel():
    """Serve o painel de controle HTML."""
    try:
        return send_from_directory(BASE_DIR, "panel.html", max_age=5)
    except Exception as e:
        return (
            f"<h1>Panel não encontrado</h1>"
            f"<p>Crie o arquivo <code>panel.html</code> em "
            f"<code>{BASE_DIR}</code>.</p>"
            f"<p>Erro: {e}</p>"
        ), 404

@app.route("/monitor")
@app.route("/monitor.html")
def monitor():
    """Serve o monitor de rede HTML."""
    try:
        return send_from_directory(BASE_DIR, "monitor.html", max_age=5)
    except Exception as e:
        return (
            f"<h1>Monitor não encontrado</h1>"
            f"<p>Crie o arquivo <code>monitor.html</code> em "
            f"<code>{BASE_DIR}</code>.</p>"
            f"<p>Erro: {e}</p>"
        ), 404

# ============================================================
# PROXIES para o server.py :5000 (v9.4)
# ------------------------------------------------------------
# Permitem que o painel em :8080 fale com o server em :5000
# sem problemas de CORS.
# ============================================================
import urllib.request
import urllib.error
import json as _json


def _proxy_get(path: str, timeout: float = 5.0):
    """
    Faz GET em http://127.0.0.1:5000<path> e devolve o JSON.
    Retorna (data, status_code) — status_code é 200 se OK.
    """
    url = f"http://{SERVER_HOST}:{SERVER_PORT}{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brn-explorer"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode("utf-8")), 200
    except urllib.error.HTTPError as e:
        try:
            body = _json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {"error": f"HTTP {e.code}"}
        return body, e.code
    except urllib.error.URLError as e:
        return {"error": f"server offline: {e.reason}"}, 503
    except Exception as e:
        return {"error": str(e)}, 503


def _proxy_post(path: str, body: dict, timeout: float = 30.0):
    """POST para o server.py."""
    url = f"http://{SERVER_HOST}:{SERVER_PORT}{path}"
    data = _json.dumps(body).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json",
                     "User-Agent": "brn-explorer"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode("utf-8")), 200
    except urllib.error.HTTPError as e:
        try:
            body_err = _json.loads(e.read().decode("utf-8"))
        except Exception:
            body_err = {"error": f"HTTP {e.code}"}
        return body_err, e.code
    except urllib.error.URLError as e:
        return {"error": f"server offline: {e.reason}"}, 503
    except Exception as e:
        return {"error": str(e)}, 503


@app.route("/api/status-proxy")
@require_rate_limit("status")
def status_proxy():
    data, code = _proxy_get("/api/status")
    return jsonify(data), code


@app.route("/api/chain-info-proxy")
@require_rate_limit("status")
def chain_info_proxy():
    data, code = _proxy_get("/api/chain-info")
    return jsonify(data), code


@app.route("/api/sync-status-proxy")
@require_rate_limit("status")
def sync_status_proxy():
    data, code = _proxy_get("/api/sync/status")
    return jsonify(data), code


@app.route("/api/sync-info-proxy")
@require_rate_limit("status")
def sync_info_proxy():
    data, code = _proxy_get("/api/sync-info")
    return jsonify(data), code


@app.route("/api/miner-status-proxy")
@require_rate_limit("status")
def miner_status_proxy():
    data, code = _proxy_get("/api/miner/status")
    return jsonify(data), code


@app.route("/api/miner/start-proxy", methods=["POST"])
def miner_start_proxy():
    body = request.get_json(force=True, silent=True) or {}
    data, code = _proxy_post("/api/miner/start", body)
    return jsonify(data), code


@app.route("/api/miner/stop-proxy", methods=["POST"])
def miner_stop_proxy():
    data, code = _proxy_post("/api/miner/stop", {})
    return jsonify(data), code


@app.route("/api/peers-score-proxy")
@require_rate_limit("status")
def peers_score_proxy():
    data, code = _proxy_get("/api/peers/score")
    return jsonify(data), code


@app.route("/api/portfolio-proxy/<address>")
@require_rate_limit("portfolio")
def portfolio_proxy(address):
    if not _valid_address(address):
        return jsonify({"error": "endereco invalido"}), 400
    data, code = _proxy_get(f"/api/portfolio/{address}")
    return jsonify(data), code


@app.route("/api/saldo-proxy/<address>")
@require_rate_limit("portfolio")
def saldo_proxy(address):
    if not _valid_address(address):
        return jsonify({"error": "endereco invalido"}), 400
    data, code = _proxy_get(f"/api/saldo/{address}")
    return jsonify(data), code


@app.route("/api/tx-status-proxy/<txid_str>")
@require_rate_limit("status")
def tx_status_proxy(txid_str):
    data, code = _proxy_get(f"/api/tx-status/{txid_str}")
    return jsonify(data), code


@app.route("/api/minhas-txs-proxy/<address>")
@require_rate_limit("portfolio")
def minhas_txs_proxy(address):
    if not _valid_address(address):
        return jsonify({"error": "endereco invalido"}), 400
    data, code = _proxy_get(f"/api/minhas-txs/{address}")
    return jsonify(data), code


# ============================================================
# Rotas — chain (locais, lendo do DB)
# ============================================================
@app.route("/api/stats")
@require_rate_limit("status")
def stats():
    db = _db()
    s = db.get_stats()
    try:
        s["peers_count"] = db.contar_peers(apenas_ativos=False)
    except Exception:
        s["peers_count"] = 0
    return jsonify(s)


@app.route("/api/block/<int:h>")
@require_rate_limit("get_block")
def block(h):
    if h < 0:
        return jsonify({"error": "altura invalida"}), 400
    b = _db().get_block(h)
    if not b:
        return jsonify({"error": "not found"}), 404
    return jsonify(b)


@app.route("/api/latest")
@require_rate_limit("list_blocks")
def latest():
    return jsonify(_db().latest_blocks(n=10))


@app.route("/api/blocks")
@require_rate_limit("list_blocks")
def blocks_paginado():
    start = _parse_int("start", 0, 0, 10 ** 9)
    limit = _parse_int("limit", 20, 1, 100)
    db = _db()
    top = db.height()
    out = []
    for h in range(start, min(start + limit, top + 1)):
        b = db.get_block(h)
        if b:
            out.append({
                "height": b["height"],
                "hash": b["hash"],
                "timestamp": b["timestamp"],
                "txs": len(b["transactions"]),
                "difficulty": b["difficulty"],
            })
    return jsonify({"start": start, "count": len(out), "blocks": out})


@app.route("/api/balance/<address>")
@require_rate_limit("portfolio")
def balance(address):
    if not _valid_address(address):
        return jsonify({"error": "endereco invalido"}), 400
    db = _db()
    return jsonify({
        "address": address,
        "balance": db.balance(address),
        "utxos": db.utxos_for(address),
    })


@app.route("/api/mempool")
@require_rate_limit("status")
def mempool():
    return jsonify(_db().all_mempool(limit=200))


@app.route("/api/peers")
@require_rate_limit("status")
def peers():
    db = _db()
    return jsonify({
        "count": db.contar_peers(apenas_ativos=False),
        "active": db.contar_peers(apenas_ativos=True),
        "peers": db.listar_peers(apenas_ativos=False),
    })


@app.route("/api/events")
@require_rate_limit("status")
def events():
    n = _parse_int("n", 50, 1, 200)
    return jsonify(_db().ultimos_eventos(n))


# ============================================================
# Verify
# ============================================================
_verify_lock = threading.Lock()
_verify_cache = {"ts": 0.0, "result": None}


@app.route("/api/verify")
@require_rate_limit("verify")
def verify():
    with _verify_lock:
        now = time.time()
        if (_verify_cache["result"] is not None
                and now - _verify_cache["ts"] < VERIFY_TTL):
            return jsonify({**_verify_cache["result"], "cached": True})

    from blockchain import Blockchain
    from chain_validator import verify_chain_dict

    bc = Blockchain(db_path=DB_PATH)
    try:
        result = verify_chain_dict(bc)
    finally:
        try:
            bc.db.close()
        except Exception:
            pass

    with _verify_lock:
        _verify_cache["ts"] = time.time()
        _verify_cache["result"] = result
    return jsonify({**result, "cached": False})


# ============================================================
# Mongo (opcional)
# ============================================================
@app.route("/api/mongo/health")
@require_rate_limit("status")
def mongo_health():
    if not _HAS_MONGO or mongo is None:
        return jsonify({"ok": False, "error": "pymongo nao instalado"}), 503
    ok, err = mongo.ping()
    body = {
        "ok": ok,
        "db": mongo.db_name,
        "configured": bool(mongo.uri),
        "last_ping": mongo._last_ping,
    }
    if err:
        body["error"] = err
    return jsonify(body), (200 if ok else 503)


@app.route("/api/mongo/events")
@require_rate_limit("status")
def mongo_events():
    if not _HAS_MONGO or mongo is None:
        return jsonify({"error": "pymongo nao instalado"}), 503
    db = mongo.db()
    if db is None:
        return jsonify({"error": "MongoDB indisponivel"}), 503
    n = _parse_int("n", 50, 1, 500)
    try:
        cursor = db.events.find({}, {"_id": 0}).sort("ts", -1).limit(n)
        return jsonify({"count": n, "events": list(cursor)})
    except Exception as e:
        log.error("Mongo query falhou: %s", e)
        return jsonify({"error": "mongo query failed"}), 503


# ============================================================
# Rendezvous
# ============================================================
_rdv_mem_lock = threading.Lock()
_rdv_mem = {}
RDV_TTL = 300
RDV_MAX = 5000


def _rdv_store():
    if _HAS_MONGO and mongo is not None:
        db = mongo.db()
        if db is not None:
            coll = db.rendezvous
            try:
                coll.create_index("ts", expireAfterSeconds=RDV_TTL, background=True)
                coll.create_index("uuid", unique=True, background=True)
            except Exception:
                pass
            return coll, False
    return None, True


def _rdv_verify_sig(uuid_hex, port, ts, sig_hex):
    if not sig_hex:
        return True
    try:
        from crypto import Ed25519PublicKey
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(uuid_hex))
        pk.verify(bytes.fromhex(sig_hex),
                  (uuid_hex + "|" + str(port) + "|" + str(ts)).encode())
        return True
    except ImportError:
        return True
    except Exception:
        return False


@app.route("/api/rendezvous/announce", methods=["POST"])
@require_rate_limit("rendezvous_announce")
def rdv_announce():
    body = request.get_json(force=True, silent=True) or {}
    uuid = str(body.get("uuid", "")).strip()
    port = body.get("port")
    ts = body.get("ts")
    sig = str(body.get("sig", "")).strip()
    ip_hint = str(body.get("ip_hint", "")).strip()

    if not uuid or not isinstance(port, int) or not isinstance(ts, int):
        return jsonify({"ok": False, "error": "campos ausentes"}), 400
    if not (1 <= port <= 65535):
        return jsonify({"ok": False, "error": "porta invalida"}), 400
    if abs(time.time() - ts) > 300:
        return jsonify({"ok": False, "error": "ts fora de janela"}), 400
    if not _rdv_verify_sig(uuid, port, ts, sig):
        return jsonify({"ok": False, "error": "assinatura invalida"}), 403

    ip = ip_hint or (request.remote_addr or "")
    if not ip:
        return jsonify({"ok": False, "error": "sem IP"}), 400

    now = time.time()
    entry = {
        "uuid": uuid, "ip": ip, "port": port, "ts": now,
        "ua": body.get("ua", "")[:64],
    }
    coll, mem = _rdv_store()
    try:
        if mem:
            with _rdv_mem_lock:
                _rdv_mem[uuid] = entry
                if len(_rdv_mem) > RDV_MAX:
                    cutoff = now - RDV_TTL
                    for k in [k for k, v in _rdv_mem.items() if v["ts"] < cutoff]:
                        del _rdv_mem[k]
                peers = [
                    {"uuid": v["uuid"], "ip": v["ip"], "port": v["port"]}
                    for v in _rdv_mem.values() if v["uuid"] != uuid
                ]
        else:
            coll.update_one({"uuid": uuid}, {"$set": entry}, upsert=True)
            peers = list(coll.find(
                {"ts": {"$gte": now - RDV_TTL}, "uuid": {"$ne": uuid}},
                {"_id": 0, "uuid": 1, "ip": 1, "port": 1},
            ).limit(500))
    except Exception as e:
        log.error("rendezvous announce falhou: %s", e)
        return jsonify({"ok": False, "error": "store error"}), 503

    return jsonify({"ok": True, "peers": peers, "count": len(peers)})


@app.route("/api/rendezvous/peers")
@require_rate_limit("status")
def rdv_peers():
    now = time.time()
    coll, mem = _rdv_store()
    try:
        if mem:
            with _rdv_mem_lock:
                peers = [
                    {"uuid": v["uuid"], "ip": v["ip"], "port": v["port"]}
                    for v in _rdv_mem.values()
                    if v["ts"] >= now - RDV_TTL
                ]
        else:
            peers = list(coll.find(
                {"ts": {"$gte": now - RDV_TTL}},
                {"_id": 0, "uuid": 1, "ip": 1, "port": 1},
            ).limit(500))
    except Exception as e:
        log.error("rdv peers falhou: %s", e)
        return jsonify({"error": "store error"}), 503
    return jsonify({"ok": True, "count": len(peers), "peers": peers})


# ============================================================
# Health
# ============================================================
@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "ts": time.time(),
        "server_port": SERVER_PORT,
        "panel_disponivel": (BASE_DIR / "panel.html").exists(),
    })


# ============================================================
# Shutdown
# ============================================================
@atexit.register
def _shutdown():
    log.info("encerrando explorer...")
    try:
        if _HAS_MONGO and mongo is not None:
            mongo.close()
    except Exception:
        pass
    try:
        close_audit()
    except Exception:
        pass


# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print(f"  BRN Explorer v9.4")
    print(f"  → http://{HOST}:{PORT}")
    print(f"  → Painel: http://{HOST}:{PORT}/panel")
    print(f"  → Proxies para: http://{SERVER_HOST}:{SERVER_PORT}")
    print(f"  DB: {DB_PATH}")
    print(f"  CORS: {'habilitado' if _HAS_CORS else 'desabilitado'}")
    if _HAS_MONGO and mongo is not None:
        print(f"  MongoDB: {'configurado' if mongo.uri else 'nao configurado'}")
    else:
        print(f"  MongoDB: desabilitado (pymongo nao instalado)")
    print("=" * 60)
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
