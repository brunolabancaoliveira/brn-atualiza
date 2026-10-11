"""
tracker.py — Tracker HTTP para descoberta de peers BRN (v2)
============================================================
v2:
  - Thread-safe: Lock em todas as operacoes sobre PEERS
  - Cleanup em background (nao depende de /peers ser chamado)
  - Rate limit por IP em /anunciar
  - Validacao de endereco (IP/host + porta)
  - CORS habilitado (para clientes web)
  - Logging estruturado
  - Endpoints uteis: /peers, /stats, /health
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import re
import time
import threading
import logging
from collections import defaultdict

# ============================================================
# CONFIG
# ============================================================
PORT = int(os.environ.get("PORT", "9000"))
TTL = int(os.environ.get("BRN_TRACKER_TTL", "600"))       # 10 min
CLEANUP_INTERVAL = int(os.environ.get("BRN_TRACKER_CLEANUP", "60"))  # 1 min
MAX_PEERS = int(os.environ.get("BRN_TRACKER_MAX_PEERS", "5000"))
RATE_LIMIT = int(os.environ.get("BRN_TRACKER_RATE", "30"))  # req/min por IP
RATE_WINDOW = 60

# Regex para validar enderecos: "host:port"
# Aceita: "1.2.3.4:6001", "meu-host.local:6001", "2001:db8::1:6001"
ADDR_RE = re.compile(
    r"^("
    r"(?:\d{1,3}\.){3}\d{1,3}"          # IPv4
    r"|"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9\-\.]*[a-zA-Z0-9])?"  # hostname
    r"|"
    r"\[[0-9a-fA-F:]+\]"                 # IPv6 em brackets
    r")"
    r":(\d{1,5})$"                       # :porta
)

# ============================================================
# ESTADO
# ============================================================
app = Flask(__name__)
CORS(app)

PEERS = {}                  # addr -> {"last_seen": ts, "first_seen": ts, "hits": n}
PEERS_LOCK = threading.Lock()

_rate_buckets = defaultdict(list)
_rate_lock = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tracker")


# ============================================================
# HELPERS
# ============================================================
def _validar_endereco(addr: str) -> bool:
    """Valida 'host:port'. Aceita IPv4, hostname, IPv6 em brackets."""
    if not addr or len(addr) > 255:
        return False
    m = ADDR_RE.match(addr)
    if not m:
        return False
    try:
        porta = int(m.group(2))
        if porta < 1 or porta > 65535:
            return False
    except (ValueError, IndexError):
        return False
    return True


def _rate_ok(ip: str) -> bool:
    """Rate limit: MAX_REQ por IP em RATE_WINDOW segundos."""
    agora = time.time()
    with _rate_lock:
        bucket = _rate_buckets[ip]
        bucket[:] = [t for t in bucket if agora - t < RATE_WINDOW]
        if len(bucket) >= RATE_LIMIT:
            return False
        bucket.append(agora)
        return True


def _cleanup_loop():
    """Remove peers expirados a cada CLEANUP_INTERVAL segundos."""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            agora = time.time()
            with PEERS_LOCK:
                expirados = [k for k, v in PEERS.items()
                             if agora - v["last_seen"] > TTL]
                for k in expirados:
                    del PEERS[k]
            if expirados:
                log.info(f"cleanup: removidos {len(expirados)} peers expirados "
                         f"(restam {len(PEERS)})")
        except Exception as e:
            log.error(f"cleanup erro: {e}")


# ============================================================
# ROTAS
# ============================================================
@app.route("/anunciar", methods=["POST"])
def anunciar():
    ip = request.remote_addr or "?"
    if not _rate_ok(ip):
        return jsonify({"ok": False, "msg": "rate limit"}), 429

    data = request.get_json(force=True, silent=True) or {}
    addr = (data.get("address") or "").strip()

    if not _validar_endereco(addr):
        return jsonify({"ok": False, "msg": "address invalido (formato host:port)"}), 400

    agora = time.time()
    with PEERS_LOCK:
        if len(PEERS) >= MAX_PEERS and addr not in PEERS:
            return jsonify({"ok": False, "msg": "tracker cheio"}), 503

        if addr in PEERS:
            PEERS[addr]["last_seen"] = agora
            PEERS[addr]["hits"] = PEERS[addr].get("hits", 0) + 1
        else:
            PEERS[addr] = {
                "last_seen": agora,
                "first_seen": agora,
                "hits": 1,
            }
        total = len(PEERS)

    log.info(f"anunciar: {addr} (de {ip}) — total={total}")
    return jsonify({"ok": True, "total": total})


@app.route("/peers", methods=["GET"])
def peers():
    agora = time.time()
    with PEERS_LOCK:
        ativos = {
            k: v for k, v in PEERS.items()
            if agora - v["last_seen"] <= TTL
        }
    return jsonify({
        "peers": list(ativos.keys()),
        "count": len(ativos),
        "ttl": TTL,
    })


@app.route("/stats", methods=["GET"])
def stats():
    agora = time.time()
    with PEERS_LOCK:
        total = len(PEERS)
        ativos = sum(1 for v in PEERS.values()
                     if agora - v["last_seen"] <= TTL)
    return jsonify({
        "total_registrados": total,
        "ativos": ativos,
        "ttl": TTL,
        "max_peers": MAX_PEERS,
        "uptime_s": int(agora - _START_TS),
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "ts": time.time()})


@app.route("/", methods=["GET"])
def raiz():
    agora = time.time()
    with PEERS_LOCK:
        ativos = sum(1 for v in PEERS.values()
                     if agora - v["last_seen"] <= TTL)
    return (
        f"BRN Tracker — {ativos} peers ativos "
        f"(TTL={TTL}s)\n"
        f"Endpoints:\n"
        f"  POST /anunciar   {{\"address\": \"ip:porta\"}}\n"
        f"  GET  /peers      lista de peers\n"
        f"  GET  /stats      estatisticas\n"
        f"  GET  /health     healthcheck\n"
    ), 200, {"Content-Type": "text/plain; charset=utf-8"}


# ============================================================
# MAIN
# ============================================================
_START_TS = time.time()

if __name__ == "__main__":
    # Sobe thread de cleanup
    t = threading.Thread(target=_cleanup_loop, daemon=True, name="Cleanup")
    t.start()

    log.info(f"🚀 BRN Tracker — http://0.0.0.0:{PORT}")
    log.info(f"   TTL={TTL}s | Cleanup={CLEANUP_INTERVAL}s | "
             f"Max peers={MAX_PEERS} | Rate={RATE_LIMIT}/min")

    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
