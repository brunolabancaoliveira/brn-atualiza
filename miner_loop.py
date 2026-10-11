"""
miner_loop.py — Loop de mineracao do BRN (v6.2)
================================================================
API publica:
    get_miner(chain) -> Miner    (singleton)
    Miner.start(addr, pubkey_hex)
    Miner.stop()
    Miner.status() -> dict

v6.2:
  [FIX] _ler_carteira_aberta usa secure_store.load_current_wallet
        (current_wallet.enc) com fallback para current_wallet.json
  [FIX] _pubkey_no_db usa db.get_utxos em vez de SQL direto
  [FIX] paths absolutos (BASE_DIR)
v6.1:
  [NEW] status() expoe 'count', 'last_height', 'uptime'
================================================================
"""
import os
import json
import time
import threading
import traceback
from pathlib import Path

try:
    from brn_logger import get_logger
    def _log(msg):
        get_logger("miner").info(msg)
except Exception:
    def _log(msg):
        print(f"[Miner] {msg}", flush=True)

print(f"[Miner] Modulo carregado: {os.path.abspath(__file__)}", flush=True)

MINER_INTERVAL      = int(os.environ.get("BRN_MINER_INTERVAL", "30"))
MINER_AUTO          = os.environ.get("BRN_MINER_AUTO", "1") == "1"

BASE_DIR                 = Path(__file__).resolve().parent
CURRENT_WALLET_ENC_FILE  = str(BASE_DIR / "current_wallet.enc")
CURRENT_WALLET_FILE      = str(BASE_DIR / "current_wallet.json")

_MINER_INSTANCE = None
_MINER_LOCK = threading.Lock()


def get_miner(chain):
    global _MINER_INSTANCE
    with _MINER_LOCK:
        if _MINER_INSTANCE is None:
            _MINER_INSTANCE = Miner(chain)
        else:
            _MINER_INSTANCE.chain = chain
        return _MINER_INSTANCE


def _senha_sessao() -> str:
    return (os.environ.get("BRN_WALLET_SESSION_PASS", "").strip()
            or os.environ.get("BRN_NODE_PASSWORD", "").strip())


def _ler_carteira_aberta():
    """Lê current_wallet.enc (novo) ou current_wallet.json (legado)."""
    # 1) tenta .enc via secure_store
    senha = _senha_sessao()
    if senha:
        try:
            from secure_store import load_current_wallet
            r = load_current_wallet(senha)
            if r and r.get("ok"):
                addr = (r.get("address") or "").strip()
                pub  = (r.get("pub_hex")
                        or r.get("public_key")
                        or r.get("pubkey")
                        or "").strip()
                if addr.startswith("brn1"):
                    return {"address": addr, "pubkey": pub}
        except Exception as e:
            _log(f"_ler_carteira_aberta(.enc) falhou: {e}")

    # 2) fallback: .json legado
    try:
        p = Path(CURRENT_WALLET_FILE)
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                d = json.load(f)
            addr = (d.get("address") or "").strip()
            pub  = (d.get("pub_hex")
                    or d.get("pubkey")
                    or d.get("public_key")
                    or "").strip()
            if addr.startswith("brn1"):
                return {"address": addr, "pubkey": pub}
    except Exception:
        pass

    return {}


def _pubkey_no_db(bc, addr):
    """Fallback: pega pubkey da primeira UTXO da carteira."""
    try:
        utxos = bc.db.get_utxos(addr)
        for u in utxos:
            pk = (u.get("pubkey") or "").strip()
            if pk:
                return pk
    except Exception:
        pass
    return ""


class Miner:
    def __init__(self, chain):
        self.chain = chain
        self._thread = None
        self._stop_evt = threading.Event()
        self.address = ""
        self.pubkey = ""
        self.running = False
        self.blocks_mined = 0
        self.last_error = ""
        self.last_height = None
        self.started_at = 0.0

    def start(self, address, pubkey_hex=""):
        if self.running:
            return False, "ja rodando"
        if not address:
            return False, "endereco obrigatorio"
        if not pubkey_hex:
            pubkey_hex = _pubkey_no_db(self.chain, address)
        if not pubkey_hex:
            msg = f"pubkey desconhecida para {address} (receba uma tx antes)"
            self.last_error = msg
            _log(msg)
            return False, msg

        self.address = address
        self.pubkey = pubkey_hex
        self.blocks_mined = 0
        self.last_error = ""
        self.last_height = None
        self.started_at = time.time()
        self._stop_evt.clear()
        self.running = True

        self._thread = threading.Thread(target=self._loop, daemon=True, name="Miner")
        self._thread.start()
        _log(f"Miner iniciado | addr={address[:16]}... | pub={pubkey_hex[:16]}...")
        return True, "iniciado"

    def stop(self):
        if not self.running:
            return False, "nao estava rodando"
        self._stop_evt.set()
        self.running = False
        _log(f"Miner parado (total={self.blocks_mined})")
        return True, "parado"

    def status(self):
        uptime = 0
        if self.started_at and self.running:
            uptime = int(time.time() - self.started_at)
        elif self.started_at and not self.running:
            uptime = int(time.time() - self.started_at) if self.blocks_mined else 0

        return {
            "running": self.running,
            "address": self.address,
            "pubkey": self.pubkey,
            "blocks_mined": self.blocks_mined,
            "height": self.chain.db.height(),
            "difficulty": self.chain.current_difficulty(),
            "last_error": self.last_error,
            "count": self.blocks_mined,
            "last_height": self.last_height,
            "uptime": uptime,
        }

    def _loop(self):
        while not self._stop_evt.is_set():
            try:
                if self.chain.db.height() < 0:
                    _log("Aguardando genesis (DB vazio)...")
                    if self._stop_evt.wait(timeout=5):
                        break
                    continue

                if not self.pubkey:
                    _log("Sem pubkey — parando")
                    self.running = False
                    return

                block = self.chain.mine_block_interruptible(
                    self.address,
                    self.pubkey,
                    should_continue=lambda: not self._stop_evt.is_set(),
                )

                if block is None:
                    break

                self.blocks_mined += 1
                h = block.get("height")
                self.last_height = h
                _log(f"OK Bloco #{h} minerado (total={self.blocks_mined})")

            except ValueError as e:
                self.last_error = str(e)
                _log(f"Miner erro: {e}")
                self.running = False
                return
            except Exception as e:
                self.last_error = str(e)
                _log(f"Miner exception: {e}")
                traceback.print_exc()
                if self._stop_evt.wait(timeout=3):
                    break

        self.running = False
        _log("Miner loop encerrado")


def loop_mineracao(chain, intervalo=None):
    """Compat com chamadas antigas."""
    if not MINER_AUTO:
        _log("Auto-mineracao DESATIVADA")
        return
    intervalo = intervalo or MINER_INTERVAL
    time.sleep(5)
    m = get_miner(chain)
    w = _ler_carteira_aberta()
    if w.get("address"):
        ok, msg = m.start(w["address"], w.get("pubkey", ""))
        _log(f"auto-start: {ok} ({msg})")
    else:
        _log("auto-start: sem carteira aberta — aguardando current_wallet")


def iniciar_mineracao(chain, intervalo=None):
    t = threading.Thread(target=loop_mineracao, args=(chain, intervalo),
                         daemon=True, name="MinerLoop")
    t.start()
    return t


start_miner = start_mining = iniciar_miner = iniciar_miner_loop = iniciar_mineracao
