"""
miner_loop.py — Loop de mineracao do BRN (v6.4)
================================================================
API publica:
    get_miner(chain) -> Miner    (singleton)
    Miner.start(addr, pubkey_hex)
    Miner.stop()
    Miner.status() -> dict

v6.4:
  [FIX] nome do atributo de callback: on_block_mined (sem underscore)
        — antes era _on_block_mined (que nao existe no blockchain.py)
        — resultado: blocos minerados localmente NAO eram propagados P2P
v6.3:
  [NEW] _should_continue() verifica chain._fork_signal
        Se o ChainSyncManager detectar fork, o miner aborta o bloco
        atual e espera o reorg terminar.
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

# Quanto tempo esperar entre checagens do fork signal (em segundos)
FORK_POLL_INTERVAL = float(os.environ.get("BRN_FORK_POLL_INTERVAL", "0.5"))

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
        # v6.3: contadores para debug
        self.fork_aborts = 0
        self.last_fork_at = 0.0

    # ============================================================
    # CONTROLE
    # ============================================================
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
        self.fork_aborts = 0
        self.last_fork_at = 0.0
        self._stop_evt.clear()
        self.running = True

        self._thread = threading.Thread(target=self._loop, daemon=True,
                                         name="Miner")
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

    # ============================================================
    # STATUS
    # ============================================================
    def status(self):
        uptime = 0
        if self.started_at and self.running:
            uptime = int(time.time() - self.started_at)
        elif self.started_at and not self.running:
            uptime = int(time.time() - self.started_at) if self.blocks_mined else 0

        fork_sig = getattr(self.chain, "_fork_signal", None)
        forking = bool(fork_sig and fork_sig.is_set())

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
            # v6.3
            "fork_aborts": self.fork_aborts,
            "last_fork_ago_s": (int(time.time() - self.last_fork_at)
                                if self.last_fork_at else None),
            "fork_in_progress": forking,
        }

    # ============================================================
    # v6.3: SHOULD CONTINUE
    # ============================================================
    def _should_continue(self) -> bool:
        """
        Retorna False se:
          - o miner foi parado (stop_evt)
          - o ChainSyncManager detectou fork (_fork_signal)

        Se detectar fork, registra o abort e loga.
        """
        if self._stop_evt.is_set():
            return False

        fork_sig = getattr(self.chain, "_fork_signal", None)
        if fork_sig is not None and fork_sig.is_set():
            self.fork_aborts += 1
            self.last_fork_at = time.time()
            _log(f"[FORK] abortando bloco atual — reorg em andamento "
                 f"(aborts={self.fork_aborts})")
            return False

        return True

    # ============================================================
    # LOOP
    # ============================================================
    def _loop(self):
        while not self._stop_evt.is_set():
            try:
                # Espera genesis se DB vazio
                if self.chain.db.height() < 0:
                    _log("Aguardando genesis (DB vazio)...")
                    if self._stop_evt.wait(timeout=5):
                        break
                    continue

                if not self.pubkey:
                    _log("Sem pubkey — parando")
                    self.running = False
                    return

                # v6.3: antes de começar um bloco, checa se há fork
                fork_sig = getattr(self.chain, "_fork_signal", None)
                if fork_sig is not None and fork_sig.is_set():
                    # espera o reorg terminar (com timeout de segurança)
                    _log("[FORK] aguardando reorg terminar...")
                    waited = 0.0
                    while (fork_sig.is_set() and waited < 60.0
                           and not self._stop_evt.is_set()):
                        if self._stop_evt.wait(FORK_POLL_INTERVAL):
                            break
                        waited += FORK_POLL_INTERVAL
                    if self._stop_evt.is_set():
                        break
                    if fork_sig.is_set():
                        _log("[FORK] timeout de 60s aguardando reorg — "
                             "retomando mineração mesmo assim")
                    else:
                        _log("[FORK] reorg concluído — retomando mineração")
                    continue

                # Mina o próximo bloco (interrompível)
                block = self.chain.mine_block_interruptible(
                    self.address,
                    self.pubkey,
                    should_continue=self._should_continue,
                )

                if block is None:
                    # Interrompido por stop_evt OU por fork signal
                    if self._stop_evt.is_set():
                        break
                    # Foi fork — volta ao topo do loop para esperar reorg
                    continue

                self.blocks_mined += 1
                h = block.get("height")
                self.last_height = h
                _log(f"OK Bloco #{h} minerado (total={self.blocks_mined})")

                # ------------------------------------------------------------
                # [FIX v6.4] Nome correto do callback: on_block_mined
                # (sem underscore). O blockchain.py já chama este callback
                # automaticamente via _fire_block_mined() dentro do
                # mine_block_interruptible(). Este getattr aqui é apenas
                # um "safety net" — o broadcast real acontece via main.py
                # que liga chain.on_block_mined = p2p.broadcast_block.
                # ------------------------------------------------------------
                try:
                    cb = getattr(self.chain, "on_block_mined", None)
                    if callable(cb):
                        cb(block)
                except Exception as e:
                    _log(f"on_block_mined callback falhou: {e}")

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


# ============================================================
# AUTO-START
# ============================================================
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
