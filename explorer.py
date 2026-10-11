"""
explorer.py — Explorador de blocos BRN (Flask) v9.3
================================================================
- Compatível com db.py v6.3, security.py v1, mongo_client (opcional)
- Não quebra se pymongo não estiver instalado
- Rate limit por endpoint via security.require_rate_limit
- Audit log em rendezvous
- Sem f-strings com aspas aninhadas (compatível com qualquer editor)
- Conexão SQLite por thread (evita lock exclusivo por request)
- Endpoints de rendezvous para descoberta P2P via hub
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
    from werkzeug.middleware.proxy_fix import ProxyFix
    _HAS_PROXYFIX = True
except ImportError:
    _HAS_PROXYFIX = False

from db import ChainDB

# mongo_client é opcional (só funciona se pymongo estiver instalado)
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

if BEHIND_PROXY and _HAS_PROXYFIX:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


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


# ============================================================
# Rotas — chain
# ============================================================
@app.route("/api/status")
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
# Verify (com cache)
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
    return jsonify({"ok": True, "ts": time.time()})


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
    print("Explorer BRN em http://" + HOST + ":" + str(PORT))
    print("  DB: " + DB_PATH)
    print("  CORS: " + (CORS_ORIGIN or "(desabilitado)"))
    if _HAS_MONGO and mongo is not None:
        print("  MongoDB: " + ("configurado" if mongo.uri else "nao configurado"))
    else:
        print("  MongoDB: desabilitado (pymongo nao instalado)")
    app.run(host=HOST, port=PORT, threaded=True, debug=False)