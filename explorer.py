"""
explorer.py — Explorador de blocos BRN (Flask) v9.7
================================================================
- Timeout dos proxies ajustado para 60s.
- Tratamento robusto de erros no SQLite.
- Serve as páginas HTML (index, panel, monitor).
================================================================
"""
from __future__ import annotations
import os
import re
import time
import atexit
import logging
import threading
import urllib.request
import urllib.error
import json as _json
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

try:
    from mongo_client import mongo
    _HAS_MONGO = True
except Exception:
    mongo = None
    _HAS_MONGO = False

try:
    from security import (
        require_rate_limit, audit_log, close_audit,
        hash_short, get_client_ip,
    )
    _HAS_SECURITY = True
except Exception:
    _HAS_SECURITY = False

    def require_rate_limit(name):
        def deco(fn): return fn
        return deco

    def audit_log(event, data=None): pass
    def close_audit(): pass
    def hash_short(v, n=12): return str(v)[:n] if v else ""
    def get_client_ip(): return request.remote_addr or "0.0.0.0"

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("BRN_DB", str(BASE_DIR / "brn_v2_chain.db"))
PORT = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
HOST = os.environ.get("BRN_EXPLORER_HOST", "127.0.0.1")
CORS_ORIGIN = os.environ.get("BRN_CORS_ORIGIN", "").strip()
BEHIND_PROXY = os.environ.get("BRN_BEHIND_PROXY", "0") == "1"

SERVER_PORT = int(os.environ.get("BRN_WEB_PORT", "5000"))
SERVER_HOST = os.environ.get("BRN_PROXY_HOST", "127.0.0.1")

BRN_ADDRESS_RE = re.compile(r"^brn1[a-z0-9]{20,90}$")

logging.basicConfig(
    level=os.environ.get("BRN_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("explorer")

app = Flask(__name__, static_folder=str(BASE_DIR))

if _HAS_CORS:
    if CORS_ORIGIN:
        CORS(app, origins=[CORS_ORIGIN])
    else:
        CORS(app, origins="*")

@app.before_request
def _before():
    if request.method == "OPTIONS":
        return ("", 200)

@app.after_request
def _after(resp):
    if CORS_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"] = CORS_ORIGIN
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Vary"] = "Origin"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp

@app.errorhandler(400)
def _e400(_): return jsonify({"error": "bad request"}), 400

@app.errorhandler(404)
def _e404(_): return jsonify({"error": "not found"}), 404

@app.errorhandler(429)
def _e429(_): return jsonify({"error": "rate limit exceeded"}), 429

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

_local = threading.local()

def _db() -> ChainDB:
    db = getattr(_local, "db", None)
    if db is None:
        db = ChainDB(DB_PATH)
        _local.db = db
    return db

def _parse_int(name, default, lo, hi):
    raw = request.args.get(name, default)
    try: v = int(raw)
    except (TypeError, ValueError): abort(400)
    if v < lo or v > hi: abort(400)
    return v

def _valid_address(addr):
    return bool(BRN_ADDRESS_RE.match(addr or ""))

# ============================================================
# ROTAS HTML (Painel, Monitor, Explorer)
# ============================================================
@app.route("/")
def index():
    try: return send_from_directory(BASE_DIR, "index.html", max_age=60)
    except Exception: return jsonify({"ok": True, "service": "BRN Explorer"})

@app.route("/panel")
@app.route("/panel.html")
def panel():
    try: return send_from_directory(BASE_DIR, "panel.html", max_age=5)
    except Exception as e: return f"<h1>Painel não encontrado</h1><p>Erro: {e}</p>", 404

@app.route("/monitor")
@app.route("/monitor.html")
def monitor():
    try: return send_from_directory(BASE_DIR, "monitor.html", max_age=5)
    except Exception as e: return f"<h1>Monitor não encontrado</h1><p>Erro: {e}</p>", 404

# ============================================================
# PROXIES (Para o server.py na porta 5000)
# ============================================================
def _proxy_get(path: str, timeout: float = 60.0):
    """Faz GET para o server.py e devolve o JSON."""
    url = f"http://{SERVER_HOST}:{SERVER_PORT}{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brn-explorer"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode("utf-8")), 200
    except urllib.error.HTTPError as e:
        try: body = _json.loads(e.read().decode("utf-8"))
        except Exception: body = {"error": f"HTTP {e.code}"}
        return body, e.code
    except urllib.error.URLError as e:
        return {"error": f"server offline: {e.reason}"}, 503
    except Exception as e:
        return {"error": str(e)}, 503

def _proxy_post(path: str, body: dict, timeout: float = 60.0):
    """Faz POST para o server.py."""
    url = f"http://{SERVER_HOST}:{SERVER_PORT}{path}"
    data = _json.dumps(body).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "User-Agent": "brn-explorer"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode("utf-8")), 200
    except urllib.error.HTTPError as e:
        try: body_err = _json.loads(e.read().decode("utf-8"))
        except Exception: body_err = {"error": f"HTTP {e.code}"}
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

@app.route("/api/peers-discovered-proxy")
@require_rate_limit("status")
def peers_discovered_proxy():
    data, code = _proxy_get("/api/peers/discovered")
    return jsonify(data), code

# ============================================================
# ROTAS LOCAIS (Leitura direta do SQLite)
# ============================================================
@app.route("/api/stats")
@require_rate_limit("status")
def stats():
    try:
        db = _db()
        s = db.get_stats()
        s["peers_count"] = db.contar_peers(apenas_ativos=False)
        return jsonify(s)
    except Exception as e:
        log.error(f"Erro em /api/stats: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/latest")
@require_rate_limit("list_blocks")
def latest():
    try:
        return jsonify(_db().latest_blocks(n=50))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/blocks")
@require_rate_limit("list_blocks")
def blocks_paginado():
    try:
        start = _parse_int("start", 0, 0, 10 ** 9)
        limit = _parse_int("limit", 50, 1, 1000)
        db = _db()
        top = db.height()
        out = []
        for h in range(start, min(start + limit, top + 1)):
            b = db.get_block(h)
            if b:
                out.append({
                    "height": b["height"], "hash": b["hash"],
                    "timestamp": b["timestamp"], "txs": len(b["transactions"]),
                    "difficulty": b["difficulty"],
                })
        return jsonify({"start": start, "count": len(out), "blocks": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({
        "ok": True, "ts": time.time(), "server_port": SERVER_PORT,
        "panel_disponivel": (BASE_DIR / "panel.html").exists(),
        "monitor_disponivel": (BASE_DIR / "monitor.html").exists(),
    })

@atexit.register
def _shutdown():
    log.info("encerrando explorer...")
    try:
        if _HAS_MONGO and mongo is not None: mongo.close()
    except Exception: pass
    try: close_audit()
    except Exception: pass

if __name__ == "__main__":
    print("=" * 60)
    print(f"  BRN Explorer v9.7")
    print(f"  → http://{HOST}:{PORT}")
    print(f"  → Painel: http://{HOST}:{PORT}/panel")
    print(f"  → Monitor: http://{HOST}:{PORT}/monitor")
    print(f"  → Proxies para: http://{SERVER_HOST}:{SERVER_PORT}")
    print(f"  DB: {DB_PATH}")
    print("=" * 60)
    app.run(host=HOST, port=PORT, threaded=True, debug=False)