"""
miner_loop.py — Loop de mineracao do BRN (v6.7)
================================================================
v6.7:
  [FIX] _loop() agora LIMPA chain._fork_signal imediatamente ao
        detectá-lo, em vez de esperar 60s.
        Isto é essencial para o miner Solo: quando um bloco novo
        chega via P2P (accept_block → _fork_signal.set()), o miner
        aborta a tentativa atual em <500ms e recomeça no novo topo.
        Antes, ficava até 60s bloqueado a olhar para o sinal.
  [NEW] Log "[SOLO] ... novo bloco/reorg detectado" em vez de
        "[FORK] abortando" (mais preciso).
v6.6:
  [NEW] Hashrate em tempo real (janela movel 10s)
v6.5:
  [FIX] Removido callback DUPLICADO de on_block_mined
v6.4:
  [FIX] status() expoe fork_aborts
v6.3:
  [NEW] _should_continue() verifica chain._fork_signal
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

# Janela movel para hashrate (segundos)
HASHRATE_WINDOW    = float(os.environ.get("BRN_HASHRATE_WINDOW", "10"))
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
                pub  = (r.get("pub_hex")
                        or r.get("public_key")
                        or r.get("pubkey")
                        or "").strip()
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
    try:
        utxos = bc.db.get_utxos(addr)
        for u in utxos:
            pk = (u.get("pubkey") or "").strip()
            if pk:
                return pk
    except Exception:
        pass
    return ""


# ============================================================
# HASHRATE METER (janela movel)
# ============================================================
class HashrateMeter:
    """
    Mede hashrate com janela movel.
    Uso:
        meter = HashrateMeter(window=10.0)
        meter.add(total_hashes)   # total acumulado
        rate = meter.rate()       # H/s
    """
    def __init__(self, window: float = 10.0):
        self.window = float(window)
        self._samples = []          # [(ts, total_hashes)]
        self._lock = threading.RLock()

    def add(self, total_hashes: int):
        now = time.time()
        with self._lock:
            self._samples.append((now, int(total_hashes)))
            cutoff = now - self.window
            self._samples = [(t, h) for (t, h) in self._samples if t >= cutoff]

    def rate(self) -> float:
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            t0, h0 = self._samples[0]
            t1, h1 = self._samples[-1]
            dt = t1 - t0
            if dt <= 0:
                return 0.0
            return max(0.0, (h1 - h0) / dt)

    def reset(self):
        with self._lock:
            self._samples.clear()


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

        # hashrate
        self.hashrate_meter = HashrateMeter(window=HASHRATE_WINDOW)
        self.hashes_total = 0                 # acumulado da sessao
        self._hashes_this_attempt = 0         # da tentativa atual
        self._last_attempt_hashes = 0
        self._attempt_t0 = 0.0
        self.last_hashrate = 0.0
        self._block_times = []                # tempos dos ultimos blocos

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

        # reset hashrate
        self.hashes_total = 0
        self._hashes_this_attempt = 0
        self._last_attempt_hashes = 0
        self._attempt_t0 = 0.0
        self.last_hashrate = 0.0
        self._block_times = []
        self.hashrate_meter.reset()

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
    # HASH RATE
    # ============================================================
    def _on_progress(self, nonce: int, hashes_done: int):
        """
        Callback chamado pelo blockchain.py a cada HASH_BATCH_SIZE.
        `hashes_done` = hashes feitos na tentativa atual.
        """
        now = time.time()
        if self._attempt_t0 == 0.0:
            self._attempt_t0 = now

        self._hashes_this_attempt = hashes_done

        delta = hashes_done - self._last_attempt_hashes
        if delta > 0:
            self.hashes_total += delta
            self._last_attempt_hashes = hashes_done
            self.hashrate_meter.add(self.hashes_total)
            self.last_hashrate = self.hashrate_meter.rate()

    def _reset_attempt(self):
        self._attempt_t0 = 0.0
        self._hashes_this_attempt = 0
        self._last_attempt_hashes = 0

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

        hashrate = self.last_hashrate

        avg_s_per_block = 0.0
        if self._block_times:
            avg_s_per_block = sum(self._block_times) / len(self._block_times)

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
            # --- hashrate ---
            "hashrate": round(hashrate, 2),
            "hashrate_khs": round(hashrate / 1000.0, 3),
            "hashes_total": int(self.hashes_total),
            "hashes_this_attempt": int(self._hashes_this_attempt),
            "avg_s_per_block": round(avg_s_per_block, 2),
            # --- fork ---
            "fork_aborts": self.fork_aborts,
            "last_fork_ago_s": (int(time.time() - self.last_fork_at)
                                if self.last_fork_at else None),
            "fork_in_progress": forking,
        }

    # ============================================================
    # SHOULD CONTINUE
    # ============================================================
    def _should_continue(self) -> bool:
        if self._stop_evt.is_set():
            return False

        fork_sig = getattr(self.chain, "_fork_signal", None)
        if fork_sig is not None and fork_sig.is_set():
            self.fork_aborts += 1
            self.last_fork_at = time.time()
            _log(f"[SOLO] abortando bloco atual — novo bloco/reorg "
                 f"detetado (aborts={self.fork_aborts})")
            return False

        return True

    # ============================================================
    # LOOP
    # ============================================================
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

                # ------------------------------------------------------------
                # [FIX v6.7] Se o fork_signal está ligado, LIMPA-O
                # imediatamente e recomeça. NÃO espera 60s.
                #
                # Porquê: o blockchain.accept_block() agora ativa
                # _fork_signal a CADA bloco aceite (v9.1.10). Isso
                # significa que o sinal aparece sempre que um bloco
                # novo chega via P2P. Se o miner esperasse 60s a
                # "aguardar reorg", atrasaria a mineração do próximo
                # bloco sem motivo. O correto é: o sinal já cumpriu a
                # sua função (fazer o PoW abortar). Limpa e recomeça.
                # ------------------------------------------------------------
                fork_sig = getattr(self.chain, "_fork_signal", None)
                if fork_sig is not None and fork_sig.is_set():
                    _log("[SOLO] novo topo detectado — limpando sinal "
                         "e recomeçando no bloco seguinte...")
                    try:
                        fork_sig.clear()
                    except Exception:
                        pass
                    # Reavalia altura e recomeça
                    continue

                # Nova tentativa
                self._reset_attempt()
                attempt_t0 = time.time()

                block = self.chain.mine_block_interruptible(
                    self.address,
                    self.pubkey,
                    should_continue=self._should_continue,
                    progress_cb=self._on_progress,
                )

                if block is None:
                    if self._stop_evt.is_set():
                        break
                    # Foi abortado (por stop ou por fork signal).
                    # Volta ao topo: se foi fork, o sinal será limpo lá.
                    continue

                self.blocks_mined += 1
                h = block.get("height")
                self.last_height = h

                dt = time.time() - attempt_t0
                self._block_times.append(dt)
                if len(self._block_times) > 20:
                    self._block_times = self._block_times[-20:]

                _log(f"OK Bloco #{h} minerado em {dt:.2f}s "
                     f"(total={self.blocks_mined}, "
                     f"hashrate={self.last_hashrate/1000:.1f} kH/s)")

                # NOTA: NÃO chamar chain.on_block_mined aqui.
                # O blockchain.py v9.1.8+ já propaga via _fire_block_mined().

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
    t = threading.Thread(target=loop_mineracao, args=(chain, intervalo),
                         daemon=True, name="MinerLoop")
    t.start()
    return t


start_miner = start_mining = iniciar_miner = iniciar_miner_loop = iniciar_mineracao