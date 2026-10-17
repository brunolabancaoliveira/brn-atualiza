"""
server.py — Backend HTTP do nó BRN (v9.9.2-Bootstrap)
================================================================
v9.9.2:
  [FIX] miner/status retorna bootstrap_status e rede_descentralizada
v9.9.1:
  [FIX] Adicionado /api/blocks e /api/block/<height>
================================================================
"""
from __future__ import annotations

import os
import time
import json
import atexit
import hmac
import logging
import threading
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from functools import wraps
from pathlib import Path

from flask import Flask, request, jsonify
from flask_cors import CORS

from wallet import Wallet, WalletManager, HDWalletManager
from blockchain import Blockchain, make_coinbase, txid as calc_txid, signing_hash

try:
    from blockchain import MIN_RELAY_FEE
except ImportError:
    MIN_RELAY_FEE = 1000

try:
    from security import (
        require_rate_limit, audit_log, close_audit,
        hash_short, get_client_ip,
    )
    _HAS_SECURITY = True
except ImportError:
    _HAS_SECURITY = False
    def require_rate_limit(name):
        def deco(fn): return fn
        return deco
    def audit_log(event, data=None): pass
    def close_audit(): pass
    def hash_short(v, n=12): return str(v)[:n] if v else ""
    def get_client_ip(): return request.remote_addr or "0.0.0.0"

log = logging.getLogger("server")
BASE_DIR = Path(__file__).resolve().parent

WEB_HOST = os.environ.get("BRN_WEB_HOST", "127.0.0.1")
BEHIND_PROXY = os.environ.get("BRN_BEHIND_PROXY", "0") == "1"
CORS_ORIGINS = [o.strip() for o in os.environ.get("BRN_CORS_ORIGIN", "").split(",") if o.strip()]
ADMIN_TOKEN = os.environ.get("BRN_ADMIN_TOKEN", "").strip()

WALLETS_FILE = str(BASE_DIR / "user_wallets.json")
CURRENT_WALLET_FILE = str(BASE_DIR / "current_wallet.json")

CACHE_TTL_S = 5

# ============================================================
# INJEÇÕES (Chain, P2P, Pool, Sync)
# ============================================================
_CHAIN = None
CHAIN_LOCK = threading.RLock()

def set_chain(chain) -> None:
    global _CHAIN
    with CHAIN_LOCK: _CHAIN = chain
    log.info("server: chain injetada (height=%s)", chain.db.height())

def _get_chain():
    global _CHAIN
    if _CHAIN is not None: return _CHAIN
    with CHAIN_LOCK:
        if _CHAIN is None:
            from blockchain import Blockchain as _BC
            _CHAIN = _BC(str(BASE_DIR / "brn_v2_chain.db"))
        return _CHAIN

class _LazyChain:
    def __getattr__(self, name): return getattr(_get_chain(), name)
CHAIN = _LazyChain()

_P2P_MANAGER = None
_P2P_LOCK = threading.Lock()
def set_p2p_manager(p2p) -> None:
    global _P2P_MANAGER
    with _P2P_LOCK: _P2P_MANAGER = p2p
    log.info("server: P2PManager injetado" if p2p else "server: P2PManager removido")
def _get_p2p_manager(): return _P2P_MANAGER

_POOL_MANAGER = None
_POOL_LOCK = threading.Lock()
def set_pool_manager(pool) -> None:
    global _POOL_MANAGER
    with _POOL_LOCK: _POOL_MANAGER = pool
    log.info("server: PoolManager injetado" if pool else "server: PoolManager removido")
def _get_pool_manager(): return _POOL_MANAGER

_SYNC_MANAGER = None
_SYNC_LOCK = threading.Lock()
def set_sync_manager(sync) -> None:
    global _SYNC_MANAGER
    with _SYNC_LOCK: _SYNC_MANAGER = sync
def _get_sync_manager(): return _SYNC_MANAGER

# L2
try:
    from l2_manager import L2Manager
    import btc_config
    L2_ENABLED = True
except ImportError:
    L2_ENABLED = False
    L2Manager = None
    btc_config = None

_l2_mgr = None
_l2_lock = threading.Lock()
def _get_l2():
    global _l2_mgr
    if not L2_ENABLED: return None
    with _l2_lock:
        if _l2_mgr is None:
            try: _l2_mgr = L2Manager(_get_chain().db, _get_chain())
            except Exception: _l2_mgr = None
        return _l2_mgr

# ============================================================
# APP
# ============================================================
app = Flask(__name__)
if BEHIND_PROXY:
    try:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    except ImportError: pass
if CORS_ORIGINS: CORS(app, origins=CORS_ORIGINS)

def _require_admin() -> bool:
    if not ADMIN_TOKEN: return True
    hdr = request.headers.get("Authorization", "")
    if not hdr.startswith("Bearer "): return False
    return hmac.compare_digest(hdr[7:], ADMIN_TOKEN)

def _admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _require_admin(): return jsonify({"ok": False, "msg": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper

# Cache
_cache_saldos: dict = {}
_cache_lock = threading.Lock()
def _saldo_cache_get(addr):
    with _cache_lock:
        if addr in _cache_saldos:
            ts, val = _cache_saldos[addr]
            if time.time() - ts < CACHE_TTL_S: return val
    return None
def _saldo_cache_set(addr, val):
    with _cache_lock: _cache_saldos[addr] = (time.time(), val)
def _saldo_cache_invalidate(addr):
    with _cache_lock: _cache_saldos.pop(addr, None)

# Wallets JSON
_wallets_lock = threading.Lock()
def carregar_wallets() -> dict:
    if not os.path.exists(WALLETS_FILE): return {}
    try:
        with open(WALLETS_FILE, encoding="utf-8") as f: return json.load(f)
    except Exception: return {}
def salvar_wallets(w: dict) -> None:
    with _wallets_lock:
        tmp = f"{WALLETS_FILE}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(w, f, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, WALLETS_FILE)
def _registrar_pubkey(addr, pk):
    if not addr or not pk: return
    try:
        wallets = carregar_wallets()
        if wallets.get(addr, {}).get("public_key") != pk:
            wallets[addr] = {"public_key": pk}
            salvar_wallets(wallets)
    except Exception: pass

def auto_registrar_carteira_atual() -> dict:
    senha = (os.environ.get("BRN_WALLET_SESSION_PASS", "").strip() or os.environ.get("BRN_NODE_PASSWORD", "").strip())
    if not senha: return {"ok": False, "msg": "senhas vazias"}
    try:
        from secure_store import load_current_wallet
        result = load_current_wallet(senha)
        if result and result.get("ok"):
            addr = (result.get("address") or "").strip()
            pk = (result.get("public_key") or result.get("pubkey") or result.get("pub_hex") or "").strip()
            if addr and pk:
                _registrar_pubkey(addr, pk)
                return {"ok": True, "msg": f"registrado: {addr[:16]}..."}
    except Exception: pass
    return {"ok": False, "msg": "sem carteira"}

def _pubkey_from_db_or_payload(addr: str, payload_pubkey: str = "", payload_sk: str = "") -> str:
    if payload_pubkey: return payload_pubkey.strip()
    if payload_sk:
        try:
            _w = Wallet(private_key_hex=payload_sk.strip())
            if _w.address == addr: return _w.pub_hex
        except Exception: pass
    try:
        wallets = carregar_wallets()
        entry = wallets.get(addr) or {}
        pk = (entry.get("public_key") or entry.get("pubkey") or "").strip()
        if pk: return pk
    except Exception: pass
    try:
        for u in _get_chain().db.get_utxos(addr):
            pk = (u.get("pubkey") or "").strip()
            if pk: return pk
    except Exception: pass
    return ""

def _find_tx_in_mempool(txid_str: str):
    try: return _get_chain().db.get_mempool_tx(txid_str)
    except Exception: return None
def _find_tx_in_chain(txid_str: str):
    try: return _get_chain().db.get_tx_by_txid(txid_str)
    except Exception: return None, None

# ============================================================
# ROTAS BÁSICAS E CARTEIRA
# ============================================================
@app.route("/api/nova-carteira", methods=["POST"])
def nova_carteira():
    try:
        w = Wallet()
        wallets = carregar_wallets()
        wallets[w.address] = {"public_key": w.pub_hex}
        salvar_wallets(wallets)
        return jsonify({"success": True, "address": w.address, "private_key": w.priv_hex, "public_key": w.pub_hex})
    except Exception as e: return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/portfolio/<address>", methods=["GET"])
def portfolio(address):
    try:
        cached = _saldo_cache_get(address)
        if cached is None:
            utxos = _get_chain().db.get_utxos(address)
            cached = sum(u["amount"] for u in utxos)
            _saldo_cache_set(address, cached)
        return jsonify({"portfolio": {"BRN": cached / 10**8, "KYC": 0}})
    except Exception as e: return jsonify({"portfolio": {}, "error": str(e)}), 500

@app.route("/api/transfer", methods=["POST"])
def transfer():
    try:
        data = request.get_json(force=True) or {}
        sender = (data.get("from") or "").strip()
        to = (data.get("to") or "").strip()
        amount_raw = data.get("amount")
        sk = (request.headers.get("X-BRN-Sk", "") or data.get("private_key", ""))

        if not WalletManager.validate_address(sender) or not WalletManager.validate_address(to):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400

        try: amount_dec = Decimal(str(amount_raw).strip().replace(",", "."))
        except Exception: return jsonify({"ok": False, "msg": "Valor invalido."}), 400

        amount_sats = int(amount_dec.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN) * Decimal(10**8))
        if amount_sats <= 0: return jsonify({"ok": False, "msg": "Valor <= 0."}), 400

        try: w = Wallet(private_key_hex=sk)
        except Exception: return jsonify({"ok": False, "msg": "Chave privada invalida."}), 400
        if w.address != sender: return jsonify({"ok": False, "msg": "Chave privada nao corresponde."}), 400

        FEE = int(MIN_RELAY_FEE)
        chain = _get_chain()
        with CHAIN_LOCK:
            utxos = sorted(chain.db.get_utxos(sender), key=lambda u: u["amount"], reverse=True)
            total, escolhidos = 0, []
            for u in utxos:
                escolhidos.append(u); total += u["amount"]
                if total >= amount_sats + FEE: break
            if total < amount_sats + FEE: return jsonify({"ok": False, "msg": "Saldo insuficiente."}), 400

            troco = total - amount_sats - FEE
            outputs = [{"address": to, "amount": amount_sats, "pubkey": ""}]
            if troco > 0: outputs.append({"address": sender, "amount": troco, "pubkey": ""})

            inputs = [{"txid": u["txid"], "vout": u["vout"], "pubkey": w.pub_hex, "signature": ""} for u in escolhidos]
            nonce = chain.db.get_nonce_for_pubkey(w.pub_hex)
            tx = {"txid": "", "inputs": inputs, "outputs": outputs, "timestamp": int(time.time()), "locktime": 0, "nonce": nonce}

            h = signing_hash(tx)
            for inp in tx["inputs"]: inp["signature"] = w.sign(h)
            tx["txid"] = calc_txid(tx)

            ok, msg = chain.submit_tx(tx)
        if not ok: return jsonify({"ok": False, "msg": msg}), 400

        p2p = _get_p2p_manager()
        if p2p: p2p.broadcast_tx(tx)
        _saldo_cache_invalidate(sender); _saldo_cache_invalidate(to)
        return jsonify({"ok": True, "txid": tx["txid"], "amount_brn": str(amount_dec), "msg": "Aceita na mempool."})
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500

# ============================================================
# BLOCOS E EXPLORER (Fix 404)
# ============================================================
@app.route("/api/blocks", methods=["GET"])
def api_get_blocks():
    try:
        start = int(request.args.get("start", 0))
        limit = min(max(int(request.args.get("limit", 15)), 1), 50)
        blocks = _get_chain().db.get_blocks_range(start, start + limit)
        return jsonify({"blocks": blocks, "start": start, "count": len(blocks)})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/block/<int:height>", methods=["GET"])
def api_get_block(height):
    try:
        block = _get_chain().db.get_block(height)
        if block: return jsonify(block)
        return jsonify({"error": "Bloco nao encontrado"}), 404
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/latest", methods=["GET"])
def api_latest_blocks():
    try: return jsonify(_get_chain().db.latest_blocks(10))
    except Exception as e: return jsonify({"error": str(e)}), 500
# ============================================================
# MINERAÇÃO (LOCAL E BOOTSTRAP)
# ============================================================
@app.route("/api/miner/start", methods=["POST"])
def miner_start():
    try:
        data = request.get_json(force=True) or {}
        addr = (data.get("validator_address") or data.get("address") or "").strip()
        if not WalletManager.validate_address(addr):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400

        pubkey = _pubkey_from_db_or_payload(addr, data.get("pubkey", ""), data.get("private_key", ""))
        if not pubkey:
            return jsonify({"ok": False, "msg": "Pubkey desconhecida."}), 400

        from miner_loop import get_miner
        m = get_miner(_get_chain())
        ok, msg = m.start(addr, pubkey)
        if not ok: return jsonify({"ok": False, "msg": msg, **m.status()}), 400
        return jsonify({"ok": True, "msg": "Minerador iniciado.", **m.status()})
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500

@app.route("/api/miner/stop", methods=["POST"])
def miner_stop():
    try:
        from miner_loop import get_miner
        m = get_miner(_get_chain())
        ok, msg = m.stop()
        return jsonify({"ok": ok, "msg": msg, **m.status()})
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500

@app.route("/api/miner/status", methods=["GET"])
def miner_status():
    try:
        from miner_loop import get_miner
        m = get_miner(_get_chain())
        st = m.status()
        return jsonify({
            "running": st.get("running", False),
            "address": st.get("address", ""),
            "pubkey": st.get("pubkey", ""),
            "blocks_mined": st.get("blocks_mined", 0),
            "count": st.get("count", st.get("blocks_mined", 0)),
            "last_height": st.get("last_height"),
            "uptime": st.get("uptime", 0),
            "consecutive_failures": st.get("consecutive_failures", 0),
            "hashrate": st.get("hashrate", 0.0),
            "hashes_done": st.get("hashes_done", 0),
            "eta_seconds": st.get("eta_seconds"),
            "height": _get_chain().db.height(),
            "difficulty": _get_chain().current_difficulty(),
            "last_error": st.get("last_error", ""),
            "fork_aborts": st.get("fork_aborts", 0),
            # [NOVO] Campos de Bootstrap
            "bootstrap_status": st.get("bootstrap_status", ""),
            "rede_descentralizada": st.get("rede_descentralizada", False),
        })
    except Exception as e:
        return jsonify({"running": False, "error": str(e)}), 200

# ============================================================
# POOL DE MINERAÇÃO
# ============================================================
@app.route("/api/pool/stats", methods=["GET"])
def pool_stats():
    pool = _get_pool_manager()
    if pool is None: return jsonify({"ok": False, "msg": "pool indisponivel"}), 503
    try: return jsonify(pool.stats())
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500

@app.route("/api/pool/miners", methods=["GET"])
def pool_miners():
    pool = _get_pool_manager()
    if pool is None: return jsonify({"ok": False, "msg": "pool indisponivel"}), 503
    try: return jsonify({"ok": True, "miners": pool.miners_list()})
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500

@app.route("/api/pool/register", methods=["POST"])
def pool_register():
    pool = _get_pool_manager()
    if pool is None: return jsonify({"ok": False, "msg": "pool indisponivel"}), 503
    try:
        data = request.get_json(force=True) or {}
        r = pool.register(data.get("address",""), data.get("pubkey",""), miner_id=data.get("miner_id"))
        return jsonify(r), (200 if r.get("ok") else 400)
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500

@app.route("/api/pool/work", methods=["POST"])
def pool_work():
    pool = _get_pool_manager()
    if pool is None: return jsonify({"ok": False, "msg": "pool indisponivel"}), 503
    try:
        data = request.get_json(force=True) or {}
        r = pool.get_work(data.get("miner_id",""))
        return jsonify(r), (200 if r.get("ok") else 404)
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500

@app.route("/api/pool/submit", methods=["POST"])
def pool_submit():
    pool = _get_pool_manager()
    if pool is None: return jsonify({"ok": False, "msg": "pool indisponivel"}), 503
    try:
        data = request.get_json(force=True) or {}
        r = pool.submit_share(data.get("miner_id",""), int(data.get("nonce",0)), data.get("hash",""))
        return jsonify(r), (200 if r.get("ok") else 400)
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500

# ============================================================
# REDE E SINC
# ============================================================
@app.route("/api/chain-info", methods=["GET"])
def chain_info():
    try:
        chain = _get_chain()
        return jsonify({"success": True, "name": "BrunoCoin", "ticker": "BRN", "height": chain.db.height(), "tip_hash": chain.db.tip_hash(), "reward": chain.current_reward(chain.db.height() + 1), "difficulty": chain.current_difficulty()})
    except Exception as e: return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/status", methods=["GET"])
def status():
    try:
        chain = _get_chain()
        with CHAIN_LOCK:
            return jsonify({
                "name": "BrunoCoin", "ticker": "BRN", "height": chain.db.height(), "tip_hash": chain.db.tip_hash(),
                "utxos": chain.db.count_utxos(), "mempool": len(chain.db.all_mempool(limit=10000)),
                "peers": chain.db.contar_peers(apenas_ativos=True), "reward": chain.current_reward(chain.db.height() + 1),
                "difficulty": chain.current_difficulty(), "l2_enabled": L2_ENABLED, "pool_enabled": _POOL_MANAGER is not None,
            })
    except Exception as e: return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/sync-info", methods=["GET"])
@app.route("/api/sync/status", methods=["GET"])
def sync_info():
    sync = _get_sync_manager()
    if sync:
        try: data = sync.get_status()
        except Exception: data = {}
    else:
        local_h = _get_chain().db.height()
        data = {"local_height": local_h, "target_height": local_h, "synced": True, "syncing": False, "progress": 100.0}

    local_h = int(data.get("local_height", 0))
    target_h = int(data.get("target_height", local_h))
    progress = float(data.get("progress", 100.0 if local_h >= target_h else 0.0))
    
    return jsonify({
        "ok": True, "synced": data.get("synced", True), "syncing": data.get("syncing", False),
        "progress": round(progress, 2), "local_height": local_h, "target_height": target_h,
        "eta_legivel": data.get("eta_legivel", ""), "active_peer": data.get("active_peer"),
        "peers_known": int(data.get("peers_known", 0)), "last_error": data.get("last_error", ""),
    }), 200

@app.route("/api/peers/discovered", methods=["GET"])
def peers_discovered():
    try:
        rows = _get_chain().db.listar_peers(apenas_ativos=False)
        out, seen = [], set()
        for p in rows:
            addr = (p.get("address") or "").strip()
            if not addr: continue
            ip = addr.rsplit(":", 1)[0] if ":" in addr else addr
            if not ip or ip.startswith("127.") or ip in seen: continue
            seen.add(ip)
            out.append({"ip": ip, "address": addr, "node_id": (p.get("node_id") or "")[:16], "height": int(p.get("height", 0) or 0), "last_seen": int(p.get("last_seen", 0) or 0), "score": int(p.get("score", 0) or 0)})
        out.sort(key=lambda x: x["last_seen"], reverse=True)
        return jsonify({"ok": True, "count": len(out), "peers": out})
    except Exception as e: return jsonify({"ok": False, "msg": str(e)}), 500

@app.route("/api/peers/score", methods=["GET"])
def peers_score():
    try:
        return jsonify({"success": True, "peers": _get_chain().db.listar_peers(apenas_ativos=False), "banned": _get_chain().db.listar_peers_maus(score_min=-100)})
    except Exception as e: return jsonify({"success": False, "error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    try:
        return jsonify({"ok": True, "height": _get_chain().db.height(), "chain_injected": _CHAIN is not None, "p2p_injected": _P2P_MANAGER is not None, "pool_injected": _POOL_MANAGER is not None, "version": "9.9.2"})
    except Exception as e: return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/", methods=["GET"])
def index():
    return jsonify({"name": "BRN Node API", "version": "9.9.2"})

@atexit.register
def _shutdown():
    try: close_audit()
    except Exception: pass

if __name__ == "__main__":
    port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    print(f"BRN Server v9.9.2 (Bootstrap Ready)")
    app.run(host=WEB_HOST, port=port, debug=False, threaded=True)
