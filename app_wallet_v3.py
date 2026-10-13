"""
app_wallet_v3.py — Carteira desktop BRN (PyWebView) | v3.7
============================================================
API exposta ao JavaScript via ponte pywebview.

v3.7:
  [FIX] minhas_txs() agora chama /api/minhas-txs (endpoint real)
  [FIX] list_transactions() também usa o endpoint real
  [NEW] cache local das últimas txs (evita spam de requisições)
  [NEW] timeout configurável via env
v3.6:
  [FIX] removido sys.exit(1) no import
  [FIX] import webview / requests lazy
  [FIX] autenticação via Bearer (BRN_ADMIN_TOKEN)
  [FIX] _senha_carteira() NÃO cai para BRN_WEB_PASS
  [FIX] user_wallets.json escrito atomicamente
  [NEW] WalletApi.info() / close() / __enter__ / __exit__
  [NEW] Probe de versão do server no /health
v3.5.1:
  start_mining() usa timeout curto (30s)
v3.5:
  generate_wallet() salva current_wallet.enc automaticamente
============================================================
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================
BASE_DIR = Path(__file__).resolve().parent

API_PORT       = int(os.environ.get("BRN_WEB_PORT", "5000"))
API_URL        = os.environ.get("BRN_API_URL", f"http://127.0.0.1:{API_PORT}")
EXPLORER_PORT  = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
EXPLORER_URL   = os.environ.get("BRN_EXPLORER_URL",
                                 f"http://127.0.0.1:{EXPLORER_PORT}")

ADMIN_TOKEN    = os.environ.get("BRN_ADMIN_TOKEN", "").strip()

TIMEOUT_LEITURA     = int(os.environ.get("BRN_WALLET_TIMEOUT_LEITURA", "30"))
TIMEOUT_TX          = int(os.environ.get("BRN_WALLET_TIMEOUT_TX", "120"))
TIMEOUT_MINERACAO   = int(os.environ.get("BRN_WALLET_TIMEOUT_MINERACAO", "300"))
TIMEOUT_MINER_START = int(os.environ.get("BRN_WALLET_TIMEOUT_MINER_START", "30"))
TIMEOUT_HEALTH      = int(os.environ.get("BRN_WALLET_TIMEOUT_HEALTH", "5"))

CACHE_TTL          = 5
WALLET_HTML        = "index_wallet.html"
CURRENT_WALLET_JSON = str(BASE_DIR / "current_wallet.json")
USER_WALLETS_FILE   = str(BASE_DIR / "user_wallets.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("brn.wallet")


# ============================================================
# HELPERS
# ============================================================
def _senha_carteira() -> str:
    """Senha para cifrar/decifrar current_wallet.enc.
    NÃO usa BRN_WEB_PASS — esse é para o app HTTP, não para o cofre."""
    return (os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
            or os.environ.get("BRN_NODE_PASSWORD", "").strip())


def _load_requests():
    try:
        import requests  # type: ignore
        return requests
    except ImportError:
        raise ImportError(
            "Módulo 'requests' não instalado. "
            "Instale: pip install requests"
        )


def _load_webview():
    try:
        import webview  # type: ignore
        return webview
    except ImportError:
        return None


def _auth_kwargs() -> dict:
    """Retorna headers com Bearer token se BRN_ADMIN_TOKEN definido."""
    if ADMIN_TOKEN:
        return {"headers": {"Authorization": f"Bearer {ADMIN_TOKEN}"}}
    return {}


def _tratar_erro_http(r):
    if r.status_code == 400:
        try:
            data = r.json()
            return {"ok": False, "msg": data.get("msg")
                    or data.get("error", "Requisicao invalida.")}
        except Exception:
            return {"ok": False, "msg": "Requisicao invalida (HTTP 400)."}
    if r.status_code == 401:
        return {"ok": False, "msg": "Nao autorizado (HTTP 401). "
                                    "Defina BRN_ADMIN_TOKEN se o server exigir."}
    if r.status_code == 403:
        return {"ok": False, "msg": "Acesso negado (HTTP 403)."}
    if r.status_code == 404:
        return {"ok": False, "msg": "Endpoint nao encontrado (HTTP 404)."}
    if r.status_code == 429:
        return {"ok": False, "msg": "Muitas requisicoes."}
    if r.status_code >= 500:
        return {"ok": False, "msg": f"Erro no servidor (HTTP {r.status_code})."}
    try:
        return r.json()
    except Exception:
        return {"ok": False, "msg": f"Resposta invalida (HTTP {r.status_code})."}


# ============================================================
# USER_WALLETS.JSON
# ============================================================
_wallets_lock = threading.Lock()


def _carregar_wallets() -> dict:
    if not os.path.exists(USER_WALLETS_FILE):
        return {}
    try:
        with open(USER_WALLETS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _salvar_wallets_atomico(w: dict) -> bool:
    with _wallets_lock:
        try:
            tmp = f"{USER_WALLETS_FILE}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(w, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, USER_WALLETS_FILE)
            return True
        except Exception as e:
            log.warning(f"_salvar_wallets_atomico falhou: {e}")
            return False


def _registrar_pubkey(addr: str, pk: str) -> bool:
    if not addr or not pk:
        return False
    try:
        w = _carregar_wallets()
        if w.get(addr, {}).get("public_key") == pk:
            return True
        w[addr] = {"public_key": pk}
        return _salvar_wallets_atomico(w)
    except Exception as e:
        log.warning(f"_registrar_pubkey falhou: {e}")
        return False


# ============================================================
# WALLET API
# ============================================================
class WalletApi:
    def __init__(self):
        self._cache_saldos: dict = {}
        self._cache_txs: dict = {}
        self._cache_txs_lock = threading.Lock()
        self._session = None
        self._server_version = ""
        self._server_has_admin_token = False
        self._probe_server()

    # --------------------------------------------------------
    # SESSÃO
    # --------------------------------------------------------
    def _get_session(self):
        if self._session is None:
            requests = _load_requests()
            self._session = requests.Session()
        return self._session

    def _probe_server(self):
        try:
            requests = _load_requests()
            r = requests.get(f"{API_URL}/health", timeout=TIMEOUT_HEALTH)
            if r.status_code == 200:
                data = r.json()
                self._server_version = str(data.get("version", "")) or ""
                self._server_has_admin_token = bool(
                    data.get("admin_protected", False)
                )
                mode = data.get("mode", "")
                if mode:
                    self._server_version += f" ({mode})"
        except Exception:
            pass

    def info(self) -> dict:
        senha_ok = bool(_senha_carteira())
        return {
            "api_url":       API_URL,
            "explorer_url":  EXPLORER_URL,
            "admin_token":   bool(ADMIN_TOKEN),
            "server_version": self._server_version or "(sem resposta)",
            "server_admin_protected": self._server_has_admin_token,
            "senha_carteira_definida": senha_ok,
            "wallet_html":   WALLET_HTML,
            "base_dir":      str(BASE_DIR),
        }

    def close(self):
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # --------------------------------------------------------
    # CACHE SALDOS
    # --------------------------------------------------------
    def _cache_get(self, key):
        if key in self._cache_saldos:
            ts, val = self._cache_saldos[key]
            if time.time() - ts < CACHE_TTL:
                return val
        return None

    def _cache_set(self, key, val):
        self._cache_saldos[key] = (time.time(), val)

    def _cache_invalidate(self, prefix=""):
        for k in list(self._cache_saldos.keys()):
            if k.startswith(prefix):
                del self._cache_saldos[k]

    # --------------------------------------------------------
    # CARTEIRA
    # --------------------------------------------------------
    def generate_wallet(self):
        try:
            from wallet import WalletManager
            info = WalletManager.generate_keypair()
            addr = info.get("address", "")
            sk = info.get("private_key", "")
            pk = info.get("public_key", "")

            if not addr or not sk or not pk:
                return {"erro": "Falha ao gerar chaves (resposta incompleta)"}

            # Salva current_wallet.enc
            senha = _senha_carteira()
            if senha:
                try:
                    from secure_store import save_current_wallet
                    r = save_current_wallet(senha, {
                        "address": addr,
                        "private_key": sk,
                        "public_key": pk,
                        "pub_hex": pk,
                    })
                    if r.get("ok"):
                        log.info("current_wallet.enc salvo")
                    else:
                        log.warning(f"save_current_wallet: {r.get('msg')}")
                except Exception as e:
                    log.warning(f"save_current_wallet falhou: {e}")

            # Registra pubkey
            _registrar_pubkey(addr, pk)

            return {
                "address": addr,
                "private_key": sk,
                "public_key": pk,
            }
        except Exception as e:
            log.exception("generate_wallet falhou")
            return {"erro": str(e)}

    def validate_address(self, addr):
        try:
            from wallet import WalletManager
            return WalletManager.validate_address(addr)
        except Exception:
            return False

    def get_active_wallet(self):
        senha = _senha_carteira()

        # 1) .enc
        if senha:
            try:
                from secure_store import load_current_wallet
                r = load_current_wallet(senha)
                if r and r.get("ok"):
                    addr = r.get("address", "")
                    pk = (r.get("public_key") or r.get("pubkey")
                          or r.get("pub_hex") or "")
                    if addr and pk:
                        _registrar_pubkey(addr, pk)
                    return {
                        "ok": True,
                        "address": addr,
                        "public_key": pk,
                        "private_key": r.get("private_key", ""),
                    }
            except Exception as e:
                log.debug(f"load_current_wallet falhou: {e}")

        # 2) .json (legado)
        try:
            p = Path(CURRENT_WALLET_JSON)
            if not p.exists():
                return {"ok": False, "msg": "Nenhuma carteira ativa"}
            data = json.loads(p.read_text(encoding="utf-8"))
            addr = data.get("address", "")
            pk = data.get("public_key") or data.get("pubkey") or ""
            if addr and pk:
                _registrar_pubkey(addr, pk)
            return {
                "ok": True,
                "address": addr,
                "public_key": pk,
                "private_key": data.get("private_key", ""),
            }
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # --------------------------------------------------------
    # LEITURA
    # --------------------------------------------------------
    def portfolio(self, addr):
        if not self.validate_address(addr):
            return {"erro": "Endereco invalido."}
        cached = self._cache_get(f"portfolio:{addr}")
        if cached:
            return cached
        try:
            s = self._get_session()
            r = s.get(f"{API_URL}/api/portfolio/{addr}",
                      timeout=TIMEOUT_LEITURA, **_auth_kwargs())
            if r.status_code != 200:
                return _tratar_erro_http(r)
            data = r.json().get("portfolio", {})
            self._cache_set(f"portfolio:{addr}", data)
            return data
        except Exception as e:
            return {"erro": str(e)}

    def node_status(self):
        try:
            s = self._get_session()
            r = s.get(f"{API_URL}/api/status", timeout=TIMEOUT_LEITURA,
                      **_auth_kwargs())
            if r.status_code == 200:
                return {"ok": True, **r.json()}
            return _tratar_erro_http(r)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def sync_info(self):
        try:
            s = self._get_session()
            r = s.get(f"{API_URL}/api/sync-info", timeout=TIMEOUT_LEITURA,
                      **_auth_kwargs())
            if r.status_code != 200:
                return {"erro": f"HTTP {r.status_code}"}
            return r.json()
        except Exception as e:
            return {"erro": str(e)}

    def chain_info(self):
        try:
            s = self._get_session()
            r = s.get(f"{API_URL}/api/chain-info", timeout=TIMEOUT_LEITURA,
                      **_auth_kwargs())
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return {"ok": True, **r.json()}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # --------------------------------------------------------
    # EXPLORER
    # --------------------------------------------------------
    def list_blocks(self, start=0, limit=15):
        try:
            s = self._get_session()
            r = s.get(f"{EXPLORER_URL}/api/latest", timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            blocos = r.json() or []
            if not isinstance(blocos, list):
                return {"erro": "Resposta invalida do explorer."}
            return {"ok": True,
                    "blocks": blocos[:max(1, min(int(limit), 100))]}
        except Exception as e:
            return {"erro": str(e)}

    def get_block(self, height):
        try:
            s = self._get_session()
            r = s.get(f"{EXPLORER_URL}/api/block/{int(height)}",
                      timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return {"ok": True, "block": r.json()}
        except Exception as e:
            return {"erro": str(e)}

    # --------------------------------------------------------
    # MINHAS TRANSAÇÕES (v3.7 — chama endpoint real)
    # --------------------------------------------------------
    def minhas_txs(self, addr, force: bool = False):
        """
        Retorna histórico completo de transações do endereço.
        v3.7: chama /api/minhas-txs (endpoint real do server v9.4).
        """
        if not self.validate_address(addr):
            return {"ok": False, "msg": "Endereco invalido."}

        cache_key = f"txs:{addr}"
        if not force:
            with self._cache_txs_lock:
                if cache_key in self._cache_txs:
                    ts, val = self._cache_txs[cache_key]
                    if time.time() - ts < CACHE_TTL:
                        return val

        try:
            s = self._get_session()
            r = s.get(f"{API_URL}/api/minhas-txs/{addr}",
                      timeout=TIMEOUT_LEITURA, **_auth_kwargs())
            if r.status_code == 200:
                data = r.json()
                with self._cache_txs_lock:
                    self._cache_txs[cache_key] = (time.time(), data)
                return data
            return _tratar_erro_http(r)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def list_transactions(self, addr):
        """
        Compat com chamadas antigas. Delega para minhas_txs.
        """
        r = self.minhas_txs(addr)
        if r.get("ok"):
            return {"ok": True,
                    "count": r.get("count", 0),
                    "transactions": r.get("transactions", [])}
        return r

    def _invalidate_txs_cache(self, addr: str = "") -> None:
        with self._cache_txs_lock:
            if not addr:
                self._cache_txs.clear()
            else:
                for k in list(self._cache_txs.keys()):
                    if k == f"txs:{addr}":
                        del self._cache_txs[k]

    # --------------------------------------------------------
    # TRANSFER
    # --------------------------------------------------------
    def transfer(self, sender, to, asset_id, amount, sk, pk):
        if not self.validate_address(sender):
            return {"ok": False, "msg": "Remetente invalido."}
        if not self.validate_address(to):
            return {"ok": False, "msg": "Destinatario invalido."}
        if sender == to:
            return {"ok": False, "msg": "Nao pode enviar para o mesmo endereco."}
        if asset_id != "BRN":
            return {"ok": False, "msg": "Apenas BRN."}
        try:
            amount_f = float(amount)
        except (ValueError, TypeError):
            return {"ok": False, "msg": "Valor invalido."}
        if amount_f <= 0:
            return {"ok": False, "msg": "Valor deve ser positivo."}
        if amount_f < 0.00001:
            return {"ok": False, "msg": "Valor muito pequeno (minimo 0.00001)."}

        payload = {
            "type": "transfer", "asset_id": asset_id,
            "from": sender, "to": to, "amount": amount_f,
            "public_key": pk, "private_key": sk,
            "nonce": int(time.time() * 1000),
        }

        headers = dict(_auth_kwargs().get("headers", {}))
        if sk:
            headers["X-BRN-Sk"] = sk

        try:
            s = self._get_session()
            r = s.post(f"{API_URL}/api/transfer", json=payload,
                       timeout=TIMEOUT_TX, headers=headers)
            self._cache_invalidate("portfolio:")
            self._invalidate_txs_cache(sender)
            self._invalidate_txs_cache(to)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # --------------------------------------------------------
    # MINERAÇÃO
    # --------------------------------------------------------
    def mine_block(self, addr, pk=""):
        if not self.validate_address(addr):
            return {"ok": False, "msg": "Endereco invalido."}
        try:
            payload = {
                "validator_address": addr,
                "miner_pubkey": pk or "",
                "pubkey": pk or "",
            }
            s = self._get_session()
            r = s.post(f"{API_URL}/api/mine", json=payload,
                       timeout=TIMEOUT_MINERACAO, **_auth_kwargs())
            if r.status_code == 200:
                resp = r.json()
                if resp.get("ok"):
                    self._cache_invalidate(f"portfolio:{addr}")
                    self._invalidate_txs_cache(addr)
                return resp
            return _tratar_erro_http(r)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def start_mining(self, address, pubkey=""):
        try:
            payload = {
                "validator_address": address,
                "miner_pubkey": pubkey or "",
                "pubkey": pubkey or "",
                "public_key": pubkey or "",
            }
            s = self._get_session()
            r = s.post(f"{API_URL}/api/miner/start", json=payload,
                       timeout=TIMEOUT_MINER_START, **_auth_kwargs())
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except Exception as e:
            msg = str(e)
            if "timed out" in msg.lower() or "timeout" in msg.lower():
                log.warning("start_mining: timeout; assumindo background")
                return {
                    "ok": True,
                    "assumed": True,
                    "msg": ("Servidor demorou para responder, mas mineração "
                            "deve estar rodando. Verifique o status no painel."),
                }
            return {"ok": False, "msg": msg}

    def stop_mining(self):
        try:
            s = self._get_session()
            r = s.post(f"{API_URL}/api/miner/stop",
                       timeout=TIMEOUT_LEITURA, **_auth_kwargs())
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def mining_status(self):
        try:
            s = self._get_session()
            r = s.get(f"{API_URL}/api/miner/status",
                      timeout=TIMEOUT_LEITURA, **_auth_kwargs())
            if r.status_code != 200:
                return {"ok": False, "running": False,
                        "msg": f"HTTP {r.status_code}"}
            return r.json()
        except Exception as e:
            return {"ok": False, "running": False, "msg": str(e)}

    # --------------------------------------------------------
    # TX STATUS / VERIFICAR
    # --------------------------------------------------------
    def tx_status(self, txid_str):
        try:
            s = self._get_session()
            r = s.get(f"{API_URL}/api/tx-status/{txid_str}",
                      timeout=TIMEOUT_LEITURA, **_auth_kwargs())
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def verificar_recebimento(self, addr, txid_str):
        if not self.validate_address(addr):
            return {"ok": False, "msg": "Endereco invalido."}
        try:
            s = self._get_session()
            r = s.get(
                f"{API_URL}/api/verificar-recebimento/{addr}/{txid_str}",
                timeout=TIMEOUT_LEITURA, **_auth_kwargs())
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # --------------------------------------------------------
    # FAUCET
    # --------------------------------------------------------
    def call_faucet(self, addr, sk, pk):
        if not self.validate_address(addr):
            return {"ok": False, "msg": "Endereco invalido."}
        try:
            payload = {
                "address": addr,
                "public_key": pk,
                "miner_pubkey": pk,
                "private_key": sk,
            }
            s = self._get_session()
            r = s.post(f"{API_URL}/api/faucet", json=payload,
                       timeout=TIMEOUT_TX, **_auth_kwargs())
            if r.status_code != 200:
                return _tratar_erro_http(r)
            self._cache_invalidate(f"portfolio:{addr}")
            self._invalidate_txs_cache(addr)
            return r.json()
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # --------------------------------------------------------
    # SALVAR / CARREGAR
    # --------------------------------------------------------
    def save_wallet(self, filename, password, address, sk, pk):
        try:
            from wallet import WalletManager
            return WalletManager.save_encrypted_wallet(
                filename, password, address, sk, pk)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def load_wallet(self, filename, password):
        try:
            from wallet import WalletManager
            r = WalletManager.load_encrypted_wallet(filename, password)
            if r.get("ok"):
                _registrar_pubkey(r.get("address", ""),
                                  r.get("public_key", ""))
                senha = _senha_carteira()
                if senha:
                    try:
                        from secure_store import save_current_wallet
                        save_current_wallet(senha, {
                            "address": r.get("address", ""),
                            "private_key": r.get("private_key", ""),
                            "public_key": r.get("public_key", ""),
                            "pub_hex": r.get("public_key", ""),
                        })
                    except Exception as e:
                        log.warning(f"save_current_wallet falhou: {e}")
            return r
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def list_wallets(self):
        try:
            from wallet import WalletManager
            return WalletManager.list_wallets()
        except Exception:
            return []


# ============================================================
# MAIN
# ============================================================
def main():
    webview = _load_webview()
    if webview is None:
        print("ERRO: pywebview não instalado.", file=sys.stderr)
        print("      Instale: pip install pywebview", file=sys.stderr)
        sys.exit(1)

    if not os.environ.get("BRN_WEB_PASS"):
        print("ERRO: defina BRN_WEB_PASS antes de rodar.", file=sys.stderr)
        sys.exit(1)

    index_path = BASE_DIR / WALLET_HTML
    if not index_path.exists():
        print(f"ERRO: {index_path} nao encontrado.", file=sys.stderr)
        sys.exit(1)

    api = WalletApi()
    info = api.info()

    log.info("=" * 60)
    log.info("  BRN Wallet v3.7")
    log.info(f"  API_URL      : {info['api_url']}")
    log.info(f"  EXPLORER_URL : {info['explorer_url']}")
    log.info(f"  HTML         : {index_path.name}")
    log.info(f"  Server ver   : {info['server_version']}")
    log.info(f"  Admin token  : {'SIM' if info['admin_token'] else 'não'}")
    log.info(f"  Senha carteira definida: {info['senha_carteira_definida']}")
    log.info("=" * 60)

    webview.create_window(
        "BRN RWA - Carteira Digital",
        url=index_path.resolve().as_uri(),
        js_api=api,
        width=1020,
        height=880,
        min_size=(820, 640),
        background_color="#0d1117",
    )

    try:
        webview.start(debug=False)
    except Exception as e:
        log.error(f"Falha ao iniciar webview: {e}")
        try:
            api.close()
        except Exception:
            pass
        sys.exit(1)
    finally:
        api.close()

    log.info("Ate logo.")


if __name__ == "__main__":
    main()
