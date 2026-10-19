"""
miner_loop.py — Loop de mineracao do BRN (v6.8 - Bootstrap + Hashrate)
================================================================
v6.8:
  [FIX] Calcula e expõe o hashrate e hashes_done no status()
        (resolve o 0.00 kH/s na interface)
v6.7:
  [NEW] Fase de Bootstrap:
        - Cliente nao minera se rede_descentralizada == False
        - Cliente envia P2P "join_mining_queue" para o validador
        - Aguarda ate que 20 mineradores entrem na rede
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
IS_ORIGIN           = os.environ.get("BRN_IS_ORIGIN", "0") == "1"

BASE_DIR                 = Path(__file__).resolve().parent
CURRENT_WALLET_ENC_FILE  = str(BASE_DIR / "current_wallet.enc")
CURRENT_WALLET_FILE      = str(BASE_DIR / "current_wallet.json")

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
    senha = _senha_sessao()
    if senha:
        try:
            from secure_store import load_current_wallet
            r = load_current_wallet(senha)
            if r and r.get("ok"):
                addr = (r.get("address") or "").strip()
                pub  = (r.get("pub_hex") or r.get("public_key") or r.get("pubkey") or "").strip()
                if addr.startswith("brn1"):
                    return {"address": addr, "pubkey": pub}
        except Exception as e:
            _log(f"_ler_carteira_aberta(.enc) falhou: {e}")

    try:
        p = Path(CURRENT_WALLET_FILE)
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                d = json.load(f)
            addr = (d.get("address") or "").strip()
            pub  = (d.get("pub_hex") or d.get("pubkey") or d.get("public_key") or "").strip()
            if addr.startswith("brn1"):
                return {"address": addr, "pubkey": pub}
    except Exception:
        pass

    return {}


def _pubkey_no_db(bc, addr):
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
        self.fork_aborts = 0
        self.last_fork_at = 0.0
        
        # [NOVO v6.8] Contadores de Hashrate
        self.hashrate = 0.0
        self.hashes_done = 0

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
        self.hashrate = 0.0
        self.hashes_done = 0
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

        fork_sig = getattr(self.chain, "_fork_signal", None)
        forking = bool(fork_sig and fork_sig.is_set())

        status_msg = ""
        if not self.chain.rede_descentralizada and not IS_ORIGIN:
            status_msg = f"Aguardando Fila ({len(self.chain.fila_mineracao)}/20)"
        elif not self.chain.rede_descentralizada and IS_ORIGIN:
            status_msg = "Validador (Bootstrap)"

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
            "fork_aborts": self.fork_aborts,
            "last_fork_ago_s": (int(time.time() - self.last_fork_at) if self.last_fork_at else None),
            "fork_in_progress": forking,
            "bootstrap_status": status_msg,
            "rede_descentralizada": self.chain.rede_descentralizada,
            # [NOVO v6.8] Retornando o Hashrate
            "hashrate": self.hashrate,
            "hashes_done": self.hashes_done
        }

    def _should_continue(self) -> bool:
        if self._stop_evt.is_set():
            return False
        fork_sig = getattr(self.chain, "_fork_signal", None)
        if fork_sig is not None and fork_sig.is_set():
            self.fork_aborts += 1
            self.last_fork_at = time.time()
            _log(f"[FORK] abortando bloco atual — reorg em andamento")
            return False
        return True

    # ============================================================
    # Le a resposta do servidor P2P e atualiza o estado
    # ============================================================
    def _enviar_para_fila(self):
        try:
            from p2p_unified import P2PClient
            from brn_config import get_config
            
            cfg = get_config()
            peers = cfg.get("bootstrap_peers", [])
            
            msg = {
                "type": "join_mining_queue",
                "address": self.address,
                "pubkey": self.pubkey
            }
            
            for peer in peers:
                if ":" in peer:
                    ip, port = peer.split(":")
                    resp, _ = P2PClient.send_message(ip, int(port), msg)
                    
                    if resp and resp.get("type") == "queue_ack":
                        if resp.get("decentralized"):
                            _log("[BOOTSTRAP] O Validador confirmou que a rede descentralizou! Iniciando mineracao...")
                            self.chain.rede_descentralizada = True
                        else:
                            tam = resp.get("queue_size", 0)
                            _log(f"Fila atualizada pelo Validador: {tam}/20")
                    else:
                        _log(f"Pedido de fila enviado para {ip}:{port} (sem resposta ack)")
        except Exception as e:
            _log(f"Falha ao enviar para fila: {e}")

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

                # ============================================================
                # FASE DE BOOTSTRAP
                # ============================================================
                if not IS_ORIGIN and not self.chain.rede_descentralizada:
                    self._enviar_para_fila()
                    _log(f"Aguardando 20 mineradores... ({len(self.chain.fila_mineracao)}/20 na fila)")
                    if self._stop_evt.wait(timeout=15):
                        break
                    continue

                # Se chegou aqui, o Validador está minerando OU a rede já descentralizou
                fork_sig = getattr(self.chain, "_fork_signal", None)
                if fork_sig is not None and fork_sig.is_set():
                    waited = 0.0
                    while (fork_sig.is_set() and waited < 60.0 and not self._stop_evt.is_set()):
                        if self._stop_evt.wait(FORK_POLL_INTERVAL):
                            break
                        waited += FORK_POLL_INTERVAL
                    if self._stop_evt.is_set(): break
                    continue

                # [NOVO v6.8] Callback para calcular hashrate
                t_start_mine = time.time()
                def _report_progress(nonce, hashes_done):
                    self.hashes_done = hashes_done
                    elapsed = time.time() - t_start_mine
                    if elapsed > 0:
                        self.hashrate = hashes_done / elapsed

                block = self.chain.mine_block_interruptible(
                    self.address,
                    self.pubkey,
                    should_continue=self._should_continue,
                    progress_cb=_report_progress,
                )

                if block is None:
                    if self._stop_evt.is_set(): break
                    continue

                self.blocks_mined += 1
                h = block.get("height")
                self.last_height = h
                self.hashrate = 0.0 # Reseta ao achar o bloco
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

# ============================================================
# AUTO-START
# ============================================================
def loop_mineracao(chain, intervalo=None):
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
    t = threading.Thread(target=loop_mineracao, args=(chain, intervalo), daemon=True, name="MinerLoop")
    t.start()
    return t

start_miner = start_mining = iniciar_miner = iniciar_miner_loop = iniciar_mineracao
