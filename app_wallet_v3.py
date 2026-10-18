"""app_wallet_v3.py — Ponte entre a GUI pywebview e o nó BRN (v3.1)"""
from __future__ import annotations
import os
import time
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("wallet-bridge")
BASE_DIR = Path(__file__).resolve().parent
API_BASE = os.environ.get("BRN_API_BASE", "http://127.0.0.1:5000")
API_TIMEOUT = float(os.environ.get("BRN_API_TIMEOUT", "200"))

_POOL_CLIENT = None
_POOL_LOCK = threading.RLock()

def _post(path: str, body: dict, timeout: float = None) -> dict:
    import requests
    url = API_BASE.rstrip("/") + path
    try:
        r = requests.post(url, json=body, timeout=timeout or API_TIMEOUT)
        try: return r.json()
        except Exception: return {"ok": False, "msg": f"HTTP {r.status_code}"}
    except requests.exceptions.ReadTimeout: return {"ok": False, "msg": "Timeout"}
    except requests.exceptions.ConnectionError: return {"ok": False, "msg": "Servidor offline (:5000)"}
    except Exception as e: return {"ok": False, "msg": str(e)}

def _get(path: str, timeout: float = 10.0) -> dict:
    import requests
    url = API_BASE.rstrip("/") + path
    try:
        r = requests.get(url, timeout=timeout)
        try: return r.json()
        except Exception: return {"error": f"HTTP {r.status_code}"}
    except requests.exceptions.ConnectionError: return {"error": "Servidor offline (:5000)"}
    except Exception as e: return {"error": str(e)}

class WalletApi:
    def __init__(self):
        log.info("WalletApi v3.1 iniciada (API_BASE=%s)", API_BASE)

    # ========================================================
    # NÓ / CHAIN
    # ========================================================
    def node_status(self) -> dict:
        try:
            s = _get("/api/status", timeout=5.0)
            if "error" in s and not s.get("height"):
                return {"ok": False, "msg": s.get("error", "erro")}
            return {
                "ok": True,
                "height": s.get("height", 0),
                "mempool": s.get("mempool", 0),
                "peers": s.get("peers", 0),
                "utxos": s.get("utxos", 0),
                "difficulty": s.get("difficulty", 0),
                "reward": s.get("reward", 0),
                "tip_hash": s.get("tip_hash", ""),
                "l2_enabled": s.get("l2_enabled", False),
                "pool_enabled": s.get("pool_enabled", False),
                "fila_tamanho": s.get("fila_tamanho", 0),
                "rede_descentralizada": s.get("rede_descentralizada", False),
            }
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def sync_info(self) -> dict:
        try:
            s = _get("/api/sync-info", timeout=5.0)
            if "erro" in s: return {"erro": s["erro"]}
            return s
        except Exception as e:
            return {"erro": str(e)}

    def list_blocks(self, start: int = 0, limit: int = 15) -> dict:
        try:
            s = _get(f"/api/blocks?start={int(start)}&limit={int(limit)}")
            if "error" in s: return {"erro": s["error"]}
            return {"blocks": s.get("blocks", []), "start": s.get("start", 0), "count": s.get("count", 0)}
        except Exception as e:
            return {"erro": str(e)}

    def get_block(self, height: int) -> dict:
        try:
            s = _get(f"/api/block/{int(height)}")
            if "error" in s: return {"erro": s["error"]}
            return {"block": s}
        except Exception as e:
            return {"erro": str(e)}

    # ========================================================
    # CARTEIRA
    # ========================================================
    def generate_wallet(self) -> dict:
        try:
            r = _post("/api/nova-carteira", {})
            if not r.get("success"): return {"erro": r.get("error", "falha")}
            return {"address": r["address"], "private_key": r["private_key"], "public_key": r["public_key"]}
        except Exception as e:
            return {"erro": str(e)}

    def portfolio(self, address: str) -> dict:
        try:
            s = _get(f"/api/portfolio/{address}", timeout=8.0)
            if "error" in s: return {"erro": s["error"]}
            return s.get("portfolio", {"BRN": 0, "KYC": 0})
        except Exception as e:
            return {"erro": str(e)}

    def get_active_wallet(self) -> dict:
        try:
            senha = (os.environ.get("BRN_WALLET_SESSION_PASS", "").strip() or os.environ.get("BRN_NODE_PASSWORD", "").strip())
            if not senha: return {"ok": False, "msg": "sem senha"}
            try:
                from secure_store import load_current_wallet
                r = load_current_wallet(senha)
                if r and r.get("ok"):
                    return {"ok": True, "address": r.get("address", ""), "private_key": r.get("private_key", ""), "public_key": (r.get("public_key") or r.get("pubkey") or r.get("pub_hex") or "")}
            except Exception: pass
            return {"ok": False, "msg": "sem carteira ativa"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def save_wallet(self, name: str, password: str, address: str, sk: str, pk: str) -> dict:
        try:
            from wallet import WalletManager
            return WalletManager.save_encrypted_wallet(name, password, address, sk, pk)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def list_wallets(self) -> list:
        try:
            from wallet import WalletManager
            return WalletManager.list_wallets()
        except Exception:
            return []

    def load_wallet(self, name: str, password: str) -> dict:
        try:
            from wallet import WalletManager
            return WalletManager.load_encrypted_wallet(name, password)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ========================================================
    # TRANSAÇÕES
    # ========================================================
    def transfer(self, from_addr: str, to_addr: str, asset: str, amount, sk: str, pk: str) -> dict:
        try:
            return _post("/api/transfer", {"from": from_addr, "to": to_addr, "asset_id": asset or "BRN", "amount": str(amount), "private_key": sk, "pubkey": pk})
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def minhas_txs(self, address: str) -> dict:
        try: return _get(f"/api/minhas-txs/{address}", timeout=15.0)
        except Exception as e: return {"ok": False, "msg": str(e)}

    def list_transactions(self, address: str) -> dict:
        try: return _get(f"/api/transacoes/{address}", timeout=15.0)
        except Exception as e: return {"erro": str(e)}

    def tx_status(self, txid: str) -> dict:
        try: return _get(f"/api/tx-status/{txid}")
        except Exception as e: return {"ok": False, "msg": str(e)}

    def verificar_recebimento(self, address: str, txid: str) -> dict:
        try: return _get(f"/api/verificar-recebimento/{address}/{txid}")
        except Exception as e: return {"ok": False, "msg": str(e)}

    # ========================================================
    # MINERAÇÃO LOCAL
    # ========================================================
    def start_mining(self, address: str, pubkey: str) -> dict:
        try: return _post("/api/miner/start", {"address": address, "miner_pubkey": pubkey})
        except Exception as e: return {"ok": False, "msg": str(e)}

    def stop_mining(self) -> dict:
        try: return _post("/api/miner/stop", {})
        except Exception as e: return {"ok": False, "msg": str(e)}

    def mining_status(self) -> dict:
        try: return _get("/api/miner/status")
        except Exception as e: return {"running": False, "error": str(e)}

    # ========================================================
    # MINERAÇÃO EM POOL
    # ========================================================
    def pool_start_mining(self, address: str, pubkey: str) -> dict:
        global _POOL_CLIENT
        with _POOL_LOCK:
            if not address or not pubkey: return {"ok": False, "msg": "address e pubkey obrigatorios"}
            try: from miner_pool_client import PoolClient
            except ImportError as e: return {"ok": False, "msg": f"miner_pool_client indisponivel: {e}"}
            
            if _POOL_CLIENT is not None and _POOL_CLIENT.is_running():
                if _POOL_CLIENT.address == address: return {"ok": True, "msg": "ja minerando", "miner_id": _POOL_CLIENT.miner_id}
                try: _POOL_CLIENT.stop_mining()
                except Exception: pass
            
            try:
                _POOL_CLIENT = PoolClient(address=address, pubkey=pubkey, pool_url=API_BASE)
                ok, msg = _POOL_CLIENT.start_mining()
                if not ok: return {"ok": False, "msg": msg}
                return {"ok": True, "msg": "Pool miner iniciado", "miner_id": _POOL_CLIENT.miner_id}
            except Exception as e: return {"ok": False, "msg": str(e)}

    def pool_stop_mining(self) -> dict:
        global _POOL_CLIENT
        with _POOL_LOCK:
            if _POOL_CLIENT is None: return {"ok": False, "msg": "nao esta a minerar"}
            try: 
                ok, msg = _POOL_CLIENT.stop_mining()
                return {"ok": ok, "msg": msg}
            except Exception as e: return {"ok": False, "msg": str(e)}

    def pool_status(self) -> dict:
        global _POOL_CLIENT
        with _POOL_LOCK:
            if _POOL_CLIENT is None: return {"ok": True, "running": False, "hashrate": 0.0, "shares_accepted": 0}
            try: return _POOL_CLIENT.status()
            except Exception as e: return {"ok": False, "msg": str(e)}

    def pool_stats(self) -> dict:
        try: return _get("/api/pool/stats", timeout=5.0)
        except Exception as e: return {"ok": False, "msg": str(e)}

    def pool_miners(self) -> dict:
        try: return _get("/api/pool/miners", timeout=5.0)
        except Exception as e: return {"ok": False, "msg": str(e)}

    # ========================================================
    # FAUCET / BRIDGE
    # ========================================================
    def call_faucet(self, address: str, sk: str, pk: str) -> dict:
        try: return _post("/api/faucet", {"address": address, "private_key": sk, "pubkey": pk})
        except Exception as e: return {"ok": False, "msg": str(e)}

    def bridge_claim(self, txid: str, address: str) -> dict:
        try: return _post("/api/l2/claim", {"btc_txid": txid, "buyer_brn": address})
        except Exception as e: return {"ok": False, "msg": str(e)}

    # ========================================================
    # UTIL
    # ========================================================
    def ping(self) -> dict:
        try: return _get("/health", timeout=3.0)
        except Exception as e: return {"ok": False, "msg": str(e)}