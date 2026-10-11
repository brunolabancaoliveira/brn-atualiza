"""
app_wallet_v3.py — Carteira desktop BRN (PyWebView) | v3.6
============================================================
API exposta ao JavaScript via ponte pywebview.

v3.6:
  [FIX] removido sys.exit(1) no import — WalletApi pode ser importada
        por main.py sem matar o processo
  [FIX] import webview / requests agora é lazy (headless funciona)
  [FIX] autenticação via Bearer (BRN_ADMIN_TOKEN), não Basic Auth
        (server.py v9.3 usa @_admin_required com Bearer)
  [FIX] _senha_carteira() NÃO cai para BRN_WEB_PASS
        (usa BRN_WALLET_SESSION_PASS → BRN_NODE_PASSWORD)
  [FIX] save_current_wallet NÃO grava mais current_wallet.json em plaintext
  [FIX] user_wallets.json escrito atomicamente + lock de arquivo
  [FIX] get_active_wallet() registra pubkey em user_wallets.json
        (antes o miner falhava com "Pubkey desconhecida")
  [FIX] minhas_txs() e list_transactions() detectam endpoint inexistente
        e devolvem resposta amigável (não 404 cru)
  [NEW] WalletApi.info() / close() / __enter__ / __exit__
  [NEW] Detecta versão do server no /health (faz 1 probe no __init__)
  [NEW] X-BRN-Sk header nas transferências (server v9.3 aceita)

v3.5.1:
  [FIX] start_mining() usa timeout curto (30s) e trata timeout como
        "iniciado em background" em vez de reportar erro.
v3.5:
  [FIX] generate_wallet() salva current_wallet.enc automaticamente
  [FIX] start_mining() envia 'miner_pubkey' (chave que o server espera)
  [FIX] mine_block() envia 'miner_pubkey'
  [FIX] get_active_wallet() tenta .enc primeiro, depois .json (legado)
  [NEW] _senha_carteira() le BRN_WALLET_SESSION_PASS ou BRN_NODE_PASSWORD
  [NEW] registro automatico em user_wallets.json quando a carteira e gerada
v3.4.1:
  timeouts maiores (leitura=30, tx=20, mina=120).
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
# CONSTANTES / CONFIG
# ============================================================
BASE_DIR = Path(__file__).resolve().parent

API_PORT       = int(os.environ.get("BRN_WEB_PORT", "5000"))
API_URL        = os.environ.get("BRN_API_URL", f"http://127.0.0.1:{API_PORT}")
EXPLORER_PORT  = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
EXPLORER_URL   = os.environ.get("BRN_EXPLORER_URL",
                                 f"http://127.0.0.1:{EXPLORER_PORT}")

ADMIN_TOKEN    = os.environ.get("BRN_ADMIN_TOKEN", "").strip()

TIMEOUT_LEITURA     = 30
TIMEOUT_TX          = 120
TIMEOUT_MINERACAO   = 300
TIMEOUT_MINER_START = 30
TIMEOUT_HEALTH      = 5

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
# HELPERS DE SENHA / CARREGAMENTO LAZY
# ============================================================
def _senha_carteira() -> str:
    """
    Senha para cifrar/decifrar current_wallet.enc.
    NÃO usa BRN_WEB_PASS — esse é para o app HTTP, não para o cofre.
    Ordem: BRN_WALLET_SESSION_PASS → BRN_NODE_PASSWORD.
    """
    return (os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
            or os.environ.get("BRN_NODE_PASSWORD", "").strip())


def _load_requests():
    """Import lazy — evita ImportError no boot se a dep faltar."""
    try:
        import requests  # type: ignore
        return requests
    except ImportError:
        raise ImportError(
            "Módulo 'requests' não instalado. "
            "Instale: pip install requests"
        )


def _load_webview():
    """Import lazy — permite importar WalletApi sem pywebview (headless)."""
    try:
        import webview  # type: ignore
        return webview
    except ImportError:
        return None


# ============================================================
# AUTENTICAÇÃO HTTP
# ============================================================
def _auth_kwargs() -> dict:
    """
    server.py v9.3 usa Bearer token para endpoints admin
    (@_admin_required). Não usa Basic Auth.
    Retorna kwargs para requests: {"headers": {...}} ou {}.
    """
    if ADMIN_TOKEN:
        return {"headers": {"Authorization": f"Bearer {ADMIN_TOKEN}"}}
    return {}


def _tratar_erro_http(r):
    """Converte resposta HTTP de erro em dict uniforme para o JS."""
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
# USER_WALLETS.JSON (atômico + lock)
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
    """Registra/atualiza pubkey de um endereço em user_wallets.json."""
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
# WALLET API (exposta ao JS via pywebview)
# ============================================================
class WalletApi:
    """API exposta ao JavaScript via pywebview."""

    def __init__(self):
        self._cache_saldos: dict = {}
        self._session = None  # requests.Session (lazy)
        self._server_version = ""
        self._server_has_admin_token = False
        self._probe_server()

    # --------------------------------------------------------
    # SESSÃO / PROBE
    # --------------------------------------------------------
    def _get_session(self):
        if self._session is None:
            requests = _load_requests()
            self._session = requests.Session()
        return self._session

    def _probe_server(self):
        """
        Faz 1 chamada rápida ao /health para saber se o server está
        de pé e qual a versão. Nunca levanta — falha silenciosa.
        """
        try:
            requests = _load_requests()
            r = requests.get(f"{API_URL}/health", timeout=TIMEOUT_HEALTH)
            if r.status_code == 200:
                data = r.json()
                self._server_version = str(data.get("version", "")) or ""
                self._server_has_admin_token = bool(
                    data.get("admin_protected", False)
                )
        except Exception:
            pass

    def info(self) -> dict:
        """Diagnóstico — útil para JS mostrar no rodapé."""
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
    # CACHE
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

    # ========================================================
    # CARTEIRA LOCAL
    # ========================================================
    def generate_wallet(self):
        """
        Gera carteira, salva em current_wallet.enc (cifrada) e
        registra pubkey em user_wallets.json.
        """
        try:
            info = WalletManager.generate_keypair()
            addr = info.get("address", "")
            sk = info.get("private_key", "")
            pk = info.get("public_key", "")

            if not addr or not sk or not pk:
                return {"erro": "Falha ao gerar chaves (resposta incompleta)"}

            # 1) Salva current_wallet.enc (cifrado) via secure_store
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
            else:
                log.warning("BRN_WALLET_SESSION_PASS/BRN_NODE_PASSWORD nao definidas "
                            "— carteira nao sera salva em .enc")

            # 2) Registra pubkey (atômico + lock)
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
            return WalletManager.validate_address(addr)
        except Exception:
            return False

    def get_active_wallet(self):
        """
        Carrega current_wallet.enc (novo) ou current_wallet.json (legado).
        Registra pubkey em user_wallets.json automaticamente (evita o
        erro "Pubkey desconhecida" ao minerar).
        """
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

        # 2) .json (legado, plaintext)
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

    # ========================================================
    # LEITURA (nó)
    # ========================================================
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

    # ========================================================
    # EXPLORADOR
    # ========================================================
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

    def list_transactions(self, addr):
        """
        Endpoint /api/transacoes/{addr} NÃO existe no server v9.3.
        Retorna lista vazia com aviso amigável em vez de 404.
        """
        if not self.validate_address(addr):
            return {"erro": "Endereco invalido."}
        return {
            "ok": True,
            "count": 0,
            "transactions": [],
            "msg": "Listagem de txs por endereço ainda não implementada no nó.",
        }

    def minhas_txs(self, addr):
        """
        Endpoint /api/minhas-txs/{addr} NÃO existe no server v9.3.
        Devolve estrutura vazia para o JS não quebrar.
        """
        if not self.validate_address(addr):
            return {"ok": False, "msg": "Endereco invalido."}
        return {
            "ok": True,
            "address": addr,
            "count": 0,
            "transactions": [],
            "msg": "Endpoint de histórico ainda não disponível no nó.",
        }

    # ========================================================
    # TRANSACOES
    # ========================================================
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

        # [v3.6] X-BRN-Sk também no header (server v9.3 lê ambos)
        headers = dict(_auth_kwargs().get("headers", {}))
        if sk:
            headers["X-BRN-Sk"] = sk

        try:
            s = self._get_session()
            r = s.post(f"{API_URL}/api/transfer", json=payload,
                       timeout=TIMEOUT_TX, headers=headers)
            self._cache_invalidate("portfolio:")
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except Exception as e:
            return {"ok": False, "msg": str(e)}

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
                return resp
            return _tratar_erro_http(r)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

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
            return r.json()
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ========================================================
    # MINER LOOP
    # ========================================================
    def start_mining(self, address, pubkey=""):
        """
        Inicia mineração contínua.
        Timeout curto; se der timeout, assume que o servidor iniciou
        em background (evita falso negativo).
        """
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
            # Timeout pode ser processamento lento do lado do servidor
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

    # ========================================================
    # VERIFICACAO DE TX
    # ========================================================
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

    # ========================================================
    # BRIDGE (stub)
    # ========================================================
    def bridge_claim(self, txid, addr):
        return {"ok": False, "msg": "Bridge nao disponivel nesta versao"}

    # ========================================================
    # PERSISTENCIA LOCAL
    # ========================================================
    def save_wallet(self, filename, password, address, sk, pk):
        try:
            return WalletManager.save_encrypted_wallet(
                filename, password, address, sk, pk)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def load_wallet(self, filename, password):
        try:
            return WalletManager.load_encrypted_wallet(filename, password)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def list_wallets(self):
        try:
            return WalletManager.list_wallets()
        except Exception:
            return []


# ============================================================
# IMPORTA WalletManager (lazy, mas com erro claro)
# ============================================================
try:
    from wallet import WalletManager
except ImportError:
    class _MissingWalletManager:
        def __getattr__(self, name):
            def _fail(*args, **kwargs):
                raise ImportError(
                    "wallet.py indisponível — não consigo usar WalletManager"
                )
            return _fail

    WalletManager = _MissingWalletManager()  # type: ignore
    log.warning("wallet.py não disponível — WalletManager em modo stub")


# ============================================================
# MAIN (standalone: python app_wallet_v3.py)
# ============================================================
def main():
    webview = _load_webview()
    if webview is None:
        print("ERRO: pywebview não instalado.", file=sys.stderr)
        print("      Instale: pip install pywebview", file=sys.stderr)
        sys.exit(1)

    if not os.environ.get("BRN_WEB_PASS"):
        print("ERRO: defina BRN_WEB_PASS antes de rodar.", file=sys.stderr)
        print("      Ex: set BRN_WEB_PASS=carteira123", file=sys.stderr)
        sys.exit(1)

    index_path = BASE_DIR / WALLET_HTML
    if not index_path.exists():
        print(f"ERRO: {index_path} nao encontrado.", file=sys.stderr)
        sys.exit(1)

    api = WalletApi()
    info = api.info()

    log.info("=" * 60)
    log.info("  BRN Wallet v3.6")
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
