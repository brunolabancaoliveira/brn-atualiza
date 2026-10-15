"""blockchain.py — Núcleo da Blockchain BRN
Versão: 9.1.7-reorg | Data: 14/10/2026
================================================================
v9.1.7-reorg:
  [CHANGE] reorg_to() movido para reorg.py (plugado no fim do arquivo).
  [KEEP]   Todo o resto do v9.1.7 inalterado:
            - Anti-death-spiral (BRN_FORCE_DIFFICULTY, BRN_BLOCK_TIME,
              BRN_DIFF_INTERVAL, BRN_MAX_DIFFICULTY)
            - Auth tag HMAC por bloco (a partir de AUTH_TAG_START_HEIGHT)
            - Checkpoints assinados Ed25519
            - Contratos (deploy/call) via contracts/vm.py
            - Callback on_block_mined
            - cumulative_work_of_chain (necessário para reorg)
================================================================
"""
from __future__ import annotations

import os
import time
import hmac
import hashlib
import orjson

from crypto import double_sha256, sha256
from db import ChainDB


# ============================================================
# METADADOS DA MOEDA
# ============================================================
COIN_NAME = "BrunoCoin"
TICKER = "BRN"
DECIMALS = 8
UNIT = 10 ** DECIMALS
MAX_SUPPLY = 21_000_000 * UNIT
INITIAL_REWARD = 50 * UNIT
HALVING_INTERVAL = 210_000

# ============================================================
# CONSENSO / DIFICULDADE
# ============================================================
BLOCK_TIME = int(os.environ.get("BRN_BLOCK_TIME", "30"))
DIFFICULTY_INTERVAL = int(os.environ.get("BRN_DIFF_INTERVAL", "50"))
MAX_DIFFICULTY = int(os.environ.get("BRN_MAX_DIFFICULTY", "7"))
INITIAL_DIFFICULTY = int(os.environ.get("BRN_MIN_DIFFICULTY", "5"))
FORCE_DIFFICULTY = os.environ.get("BRN_FORCE_DIFFICULTY", "").strip()

MAX_TX_PER_BLOCK = 500
MIN_RELAY_FEE = 1000
MAX_REORG_DEPTH = 100

HASH_BATCH_SIZE = 5000
AUTH_TAG_START_HEIGHT = 6000
CHECKPOINT_INTERVAL = 1000

# ============================================================
# GÊNESE
# ============================================================
GENESIS_PREV = "0" * 64
GENESIS_TIMESTAMP = 1700000000
GENESIS_REWARD = INITIAL_REWARD
GENESIS_ADDRESS = "brn1qxyzk7y0v2j4g0a8d9n5t2k7h4s6w8c9p2e"
GENESIS_PUBKEY = ""

# ============================================================
# DOMÍNIOS DE ASSINATURA
# ============================================================
SIGNING_DOMAIN = b"BRN-TX-v1|"
AUTH_TAG_DOMAIN = b"BRN-BLOCK-AUTH-v1|"
_CORE_V = 1

NETWORK_SECRET = os.environ.get("BRN_NETWORK_SECRET", "").strip()


# ============================================================
# CHECKPOINTS — estado do módulo
# ============================================================
_checkpoints: dict = {}
_checkpoint_priv = None


def set_checkpoints(d: dict) -> None:
    global _checkpoints
    _checkpoints = {str(k): v for k, v in (d or {}).items()}


def set_checkpoint_priv(priv) -> None:
    global _checkpoint_priv
    _checkpoint_priv = priv


def get_checkpoints() -> dict:
    return dict(_checkpoints)


def _verify_checkpoint_for_block(block) -> tuple:
    cp = _checkpoints.get(str(block["height"]))
    if not cp:
        return True, ""

    if cp.get("hash") != block["hash"]:
        return False, (
            f"checkpoint mismatch em #{block['height']} "
            f"(esperado {str(cp.get('hash'))[:16]}..., "
            f"recebido {block['hash'][:16]}...)"
        )

    try:
        from checkpoints import verify_checkpoint
        if not verify_checkpoint(cp):
            return False, f"checkpoint invalido em #{block['height']}"
    except ImportError:
        pass
    except Exception as e:
        return False, f"checkpoint verify erro em #{block['height']}: {e}"

    return True, ""


def _maybe_sign_checkpoint(block) -> None:
    if _checkpoint_priv is None:
        return
    try:
        h = int(block["height"])
    except Exception:
        return
    if h == 0 or h % CHECKPOINT_INTERVAL != 0:
        return
    try:
        from checkpoints import sign_checkpoint, save_checkpoints
        cp = sign_checkpoint(h, block["hash"], _checkpoint_priv)
        if not cp:
            return
        _checkpoints[str(h)] = cp
        save_checkpoints(_checkpoints)
        print(f"[CHECKPOINT] #{h} assinado e salvo")
    except ImportError:
        pass
    except Exception as e:
        print(f"[CHECKPOINT] falha ao assinar #{h}: {e}")


# ============================================================
# AUTH TAG
# ============================================================
def compute_auth_tag(height: int, block_hash_hex: str,
                     secret: str = None) -> str:
    sec = secret if secret is not None else NETWORK_SECRET
    if not sec:
        return ""
    msg = AUTH_TAG_DOMAIN + f"{int(height)}:{block_hash_hex}".encode()
    return hmac.new(sec.encode(), msg, hashlib.sha256).hexdigest()


def verify_auth_tag(height: int, block_hash_hex: str, tag: str,
                    secret: str = None) -> bool:
    expected = compute_auth_tag(height, block_hash_hex, secret)
    if not expected or not tag:
        return False
    return hmac.compare_digest(expected, tag)


# ============================================================
# PoW / DIFICULDADE
# ============================================================
def block_hash(prev_hash, merkle, timestamp, nonce, difficulty) -> str:
    header = f"{prev_hash}{merkle}{timestamp}{nonce}{difficulty}"
    return double_sha256(header.encode()).hex()


def target_from_difficulty(difficulty: int) -> int:
    return int("0" * int(difficulty) + "f" * (64 - int(difficulty)), 16)


def meets_difficulty(h_hex: str, difficulty: int) -> bool:
    try:
        return int(h_hex, 16) <= target_from_difficulty(difficulty)
    except Exception:
        return False


def work_from_difficulty(difficulty) -> int:
    return 16 ** int(difficulty)


def compute_merkle_root(txids: list) -> str:
    if not txids:
        return "0" * 64
    layer = [bytes.fromhex(t) for t in txids]
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [sha256(layer[i] + layer[i + 1])
                 for i in range(0, len(layer), 2)]
    return layer[0].hex()


# ============================================================
# SERIALIZAÇÃO DE TX
# ============================================================
def _tx_core(tx: dict) -> dict:
    inputs = [
        {"txid": i["txid"], "vout": i["vout"], "pubkey": i.get("pubkey", "")}
        for i in tx["inputs"]
    ]
    outputs = [
        {"address": o["address"], "amount": o["amount"],
         "pubkey": o.get("pubkey", "")}
        for o in tx["outputs"]
    ]
    core = {
        "v": _CORE_V,
        "inputs": inputs,
        "outputs": outputs,
        "timestamp": tx["timestamp"],
        "locktime": tx.get("locktime", 0),
        "nonce": tx.get("nonce", 0),
    }
    if "data" in tx and tx["data"] is not None:
        core["data"] = tx["data"]
    if "height" in tx:
        core["height"] = tx["height"]
    return core


def _serialize_core(tx: dict) -> bytes:
    return orjson.dumps(_tx_core(tx), option=orjson.OPT_SORT_KEYS)


def txid(tx: dict) -> str:
    return double_sha256(_serialize_core(tx)).hex()


def signing_hash(tx: dict) -> bytes:
    return double_sha256(SIGNING_DOMAIN + _serialize_core(tx))


# ============================================================
# HOT LOOP PoW
# ============================================================
def _pow_search(prev_hash, merkle, ts, diff, nonce_start=0,
                should_continue=None, progress_cb=None):
    prefix = f"{prev_hash}{merkle}{ts}".encode()
    diff_bytes = str(diff).encode()
    target_int = int("0" * int(diff) + "f" * (64 - int(diff)), 16)
    sha = hashlib.sha256

    nonce = nonce_start
    hashes_done = 0
    while True:
        for _ in range(HASH_BATCH_SIZE):
            data = prefix + str(nonce).encode() + diff_bytes
            h = sha(sha(data).digest()).digest()
            if int.from_bytes(h, "big") <= target_int:
                return nonce, h.hex(), hashes_done + 1
            nonce += 1
        hashes_done += HASH_BATCH_SIZE
        time.sleep(0)
        if should_continue is not None and not should_continue():
            return None, None, hashes_done
        if progress_cb is not None:
            try:
                progress_cb(nonce, hashes_done)
            except Exception:
                pass


# ============================================================
# COINBASE / GÊNESE
# ============================================================
def make_coinbase(address: str, pubkey_hex: str, height: int,
                  reward: int) -> dict:
    """Constrói um coinbase. pubkey_hex é obrigatória."""
    if not pubkey_hex:
        raise ValueError("make_coinbase: pubkey_hex é obrigatória")
    cb = {
        "txid": "",
        "inputs": [{
            "txid": "0" * 64,
            "vout": 0xFFFFFFFF,
            "pubkey": "",
            "signature": "",
        }],
        "outputs": [{
            "address": address,
            "amount": int(reward),
            "pubkey": pubkey_hex,
        }],
        "timestamp": int(time.time()),
        "locktime": 0,
        "height": int(height),
        "nonce": 0,
    }
    cb["txid"] = txid(cb)
    return cb


def build_genesis() -> dict:
    cb = {
        "txid": "",
        "inputs": [{
            "txid": "0" * 64,
            "vout": 0xFFFFFFFF,
            "pubkey": "",
            "signature": "",
        }],
        "outputs": [{
            "address": GENESIS_ADDRESS,
            "amount": GENESIS_REWARD,
            "pubkey": GENESIS_PUBKEY,
        }],
        "timestamp": GENESIS_TIMESTAMP,
        "locktime": 0,
        "height": 0,
        "nonce": 0,
    }
    cb["txid"] = txid(cb)
    merkle = compute_merkle_root([cb["txid"]])
    nonce = 0
    while True:
        h = block_hash(GENESIS_PREV, merkle, GENESIS_TIMESTAMP, nonce, 1)
        if h.startswith("0"):
            break
        nonce += 1
    g = {
        "height": 0,
        "hash": h,
        "prev_hash": GENESIS_PREV,
        "timestamp": GENESIS_TIMESTAMP,
        "nonce": nonce,
        "merkle": merkle,
        "difficulty": 1,
        "transactions": [cb],
        "cumulative_work": work_from_difficulty(1),
    }
    tag = compute_auth_tag(0, h)
    if tag:
        g["auth_tag"] = tag
    return g


GENESIS_BLOCK = build_genesis()


# ============================================================
# BLOCKCHAIN
# ============================================================
class Blockchain:
    def __init__(self, db_path="brn_v2_chain.db",
                 genesis_address=None, genesis_pubkey=None,
                 auto_genesis=True, network_secret=None,
                 require_auth_tag=None):
        self.db = ChainDB(db_path)
        self.genesis_expected_hash = None
        self.network_secret = (network_secret
                               if network_secret is not None
                               else NETWORK_SECRET)
        if require_auth_tag is None:
            self.require_auth_tag = bool(self.network_secret)
        else:
            self.require_auth_tag = bool(require_auth_tag)

        self.on_block_mined = None

        if not auto_genesis:
            self.genesis_expected_hash = GENESIS_BLOCK["hash"]
            return

        if self.db.height() < 0:
            g = GENESIS_BLOCK
            if genesis_address:
                g = self._genesis_with_address(
                    genesis_address,
                    genesis_pubkey if genesis_pubkey is not None
                    else GENESIS_PUBKEY,
                )
            self.db.add_block(g)
            self.db.apply_tx(g["transactions"][0], 0, coinbase=True)
            self.db.set_meta("genesis_hash", g["hash"])

    @staticmethod
    def _genesis_with_address(address: str, pubkey_hex: str = "") -> dict:
        cb = {
            "txid": "",
            "inputs": [{
                "txid": "0" * 64, "vout": 0xFFFFFFFF,
                "pubkey": "", "signature": "",
            }],
            "outputs": [{
                "address": address, "amount": GENESIS_REWARD,
                "pubkey": pubkey_hex,
            }],
            "timestamp": GENESIS_TIMESTAMP,
            "locktime": 0, "height": 0, "nonce": 0,
        }
        cb["txid"] = txid(cb)
        merkle = compute_merkle_root([cb["txid"]])
        nonce = 0
        while True:
            h = block_hash(GENESIS_PREV, merkle,
                           GENESIS_TIMESTAMP, nonce, 1)
            if h.startswith("0"):
                break
            nonce += 1
        g = {
            "height": 0, "hash": h, "prev_hash": GENESIS_PREV,
            "timestamp": GENESIS_TIMESTAMP, "nonce": nonce,
            "merkle": merkle, "difficulty": 1, "transactions": [cb],
            "cumulative_work": work_from_difficulty(1),
        }
        tag = compute_auth_tag(0, h)
        if tag:
            g["auth_tag"] = tag
        return g

    def set_network_secret(self, secret: str) -> None:
        self.network_secret = (secret or "").strip()
        if self.require_auth_tag is False:
            self.require_auth_tag = bool(self.network_secret)

    # ============================================================
    # RECOMPENSA / DIFICULDADE / WORK
    # ============================================================
    def current_reward(self, height: int) -> int:
        halvings = height // HALVING_INTERVAL
        if halvings >= 64:
            return 0
        return INITIAL_REWARD >> halvings

    def current_difficulty(self) -> int:
        if FORCE_DIFFICULTY:
            try:
                v = int(FORCE_DIFFICULTY)
                return max(1, min(20, v))
            except ValueError:
                print(f"[difficulty] BRN_FORCE_DIFFICULTY inválido: "
                      f"{FORCE_DIFFICULTY!r}")

        h = self.db.height()
        if h < DIFFICULTY_INTERVAL:
            return INITIAL_DIFFICULTY

        start = self.db.get_block(h - DIFFICULTY_INTERVAL + 1)
        end = self.db.get_block(h)
        if not start or not end:
            return INITIAL_DIFFICULTY

        if end["difficulty"] > MAX_DIFFICULTY:
            return MAX_DIFFICULTY

        actual = max(1, end["timestamp"] - start["timestamp"])
        expected = BLOCK_TIME * DIFFICULTY_INTERVAL
        prev = end["difficulty"]
        new = int(prev * expected / actual)
        new = max(prev // 4, min(prev * 4, new))
        new = max(1, min(MAX_DIFFICULTY, new))
        return new

    def cumulative_work(self) -> int:
        total = 0
        top = self.db.height()
        for h in range(top + 1):
            b = self.db.get_block(h)
            if b:
                total += work_from_difficulty(b["difficulty"])
        return total

    def cumulative_work_of_chain(self, blocks: list) -> int:
        return sum(work_from_difficulty(b["difficulty"]) for b in blocks)

    def get_latest_height(self) -> int:
        return self.db.height()

    def get_transaction_block_height(self, txid_str: str) -> int:
        return self.db.get_tx_block_height(txid_str)

    def count_confirmations(self, txid_str: str) -> int:
        tx_height = self.get_transaction_block_height(txid_str)
        if tx_height == -1:
            return 0
        return self.get_latest_height() - tx_height

    # ============================================================
    # FEE
    # ============================================================
    def estimate_fee(self, priority: str = "medium") -> int:
        try:
            stats = self.db.mempool_stats()
        except Exception:
            return MIN_RELAY_FEE
        count = stats.get("count", 0)
        fees = stats.get("fees", [])
        if count == 0 or not fees:
            return MIN_RELAY_FEE
        fees_sorted = sorted(fees, reverse=True)
        n = len(fees_sorted)
        if priority == "high":
            idx = max(0, n // 10)
            return max(MIN_RELAY_FEE, fees_sorted[idx] * 2)
        elif priority == "low":
            idx = min(n - 1, (n * 9) // 10)
            return max(MIN_RELAY_FEE, fees_sorted[idx])
        else:
            idx = n // 2
            return max(MIN_RELAY_FEE, fees_sorted[idx])

    def tx_fee(self, tx: dict) -> int:
        in_sum = 0
        for inp in tx["inputs"]:
            u = self.db.get_utxo(inp["txid"], inp["vout"])
            if u:
                in_sum += u["amount"]
        return in_sum - sum(o["amount"] for o in tx["outputs"])

    # ============================================================
    # CONTRATOS
    # ============================================================
    def _validate_contract_tx(self, tx: dict) -> tuple:
        tx_data = tx.get("data") or {}
        t = tx_data.get("type")
        if t == "deploy":
            code = tx_data.get("code")
            if not isinstance(code, dict):
                return False, "deploy sem 'code' (dict)"
            try:
                from contracts import ContractVM
                return ContractVM.validate(code)
            except ImportError:
                return False, "modulo contracts.py nao encontrado"
            except Exception as e:
                return False, f"erro validando contrato: {e}"
        if t == "call":
            cid = tx_data.get("contract_id")
            if not cid or not isinstance(cid, str):
                return False, "call sem 'contract_id'"
            if not cid.startswith("ctr1"):
                return False, "contract_id deve começar com 'ctr1'"
            if not self.db.contract_get(cid):
                return False, f"contrato {cid[:16]}... nao existe"
            return True, "ok"
        return False, f"tipo de contrato desconhecido: {t}"

    def _apply_contract_tx(self, tx: dict) -> dict:
        tx_data = tx.get("data") or {}
        t = tx_data.get("type")
        from contracts import deploy as _deploy, call as _call
        if t == "deploy":
            owner = ""
            if tx.get("outputs"):
                owner = tx["outputs"][0].get("address", "") or ""
            r = _deploy(self, owner, tx_data.get("code") or {},
                        metadata=tx_data.get("metadata"))
            if not r.get("ok"):
                raise ValueError(f"deploy falhou: {r.get('msg')}")
            return r
        if t == "call":
            caller = ""
            if tx.get("outputs"):
                caller = tx["outputs"][0].get("address", "") or ""
            r = _call(self, tx_data.get("contract_id", ""),
                      caller, tx_data.get("args"))
            if not r.get("ok"):
                raise ValueError(f"call falhou: {r.get('msg')}")
            return r
        raise ValueError(f"tipo de contrato desconhecido: {t}")

    # ============================================================
    # VALIDAÇÃO DE TX
    # ============================================================
    def validate_tx(self, tx: dict, from_mempool: bool = False) -> tuple:
        from wallet import Wallet

        if not tx.get("inputs") or not tx.get("outputs"):
            return False, "tx sem inputs/outputs"

        if tx["inputs"][0]["txid"] == "0" * 64:
            return False, "coinbase invalida"

        if tx.get("txid") != txid(tx):
            return False, "txid invalido"

        tx_data = tx.get("data") or {}
        if isinstance(tx_data, dict) and tx_data.get("type") in ("deploy",
                                                                 "call"):
            ok_ctr, msg_ctr = self._validate_contract_tx(tx)
            if not ok_ctr:
                return False, f"contrato: {msg_ctr}"

        for inp in tx["inputs"]:
            if not inp.get("pubkey"):
                return False, "input sem pubkey"

        tx_nonce = tx.get("nonce", 0)
        for inp in tx["inputs"]:
            pk = inp["pubkey"]
            expected = self.db.get_nonce_for_pubkey(pk)
            if tx_nonce != expected:
                return False, (
                    f"nonce invalido para {pk[:16]}... "
                    f"(esperado {expected}, recebido {tx_nonce})"
                )

        in_sum = 0
        seen = set()
        for inp in tx["inputs"]:
            key = (inp["txid"], inp["vout"])
            if key in seen:
                return False, "input duplicado"
            seen.add(key)

            u = self.db.get_utxo(inp["txid"], inp["vout"])
            if not u:
                return False, "UTXO inexistente"

            stored_pk = (u["pubkey"] or "").strip()
            if stored_pk and set(stored_pk) != {"0"}:
                if inp["pubkey"] != stored_pk:
                    return False, (
                        f"pubkey mismatch: input={inp['pubkey'][:16]}... "
                        f"utxo={stored_pk[:16]}"
                    )
            else:
                try:
                    from bech32 import address_from_pubkey
                    derived = address_from_pubkey(
                        bytes.fromhex(inp["pubkey"]))
                except Exception:
                    return False, "input pubkey invalida"
                if derived != u["address"]:
                    return False, (
                        f"address mismatch: deriva {derived[:16]}... "
                        f"utxo {u['address'][:16]}..."
                    )
            in_sum += u["amount"]

        out_sum = sum(o["amount"] for o in tx["outputs"])
        if out_sum > in_sum:
            return False, "outputs > inputs"
        if in_sum - out_sum < MIN_RELAY_FEE:
            return False, "fee abaixo do minimo"

        sig_hash = signing_hash(tx)
        for inp in tx["inputs"]:
            if not Wallet.verify(sig_hash, inp.get("signature", ""),
                                 inp["pubkey"]):
                return False, "assinatura invalida"

        return True, "ok"

    def submit_tx(self, tx: dict) -> tuple:
        if self.db.has_mempool(tx["txid"]):
            return False, "ja na mempool"
        ok, msg = self.validate_tx(tx)
        if not ok:
            return False, msg
        fee = self.tx_fee(tx)
        if fee < 0:
            return False, "fee negativa"
        if not self.db.add_mempool(tx, fee):
            return False, "falha na mempool"
        return True, tx["txid"]

    # ============================================================
    # VALIDAÇÃO DE BLOCO
    # ============================================================
    def validate_block(self, block: dict, prev_block=None) -> tuple:
        if block["height"] == 0 and prev_block is None:
            if (self.genesis_expected_hash
                    and block["hash"] != self.genesis_expected_hash):
                return False, (
                    f"genesis nao corresponde ao esperado\n"
                    f"  esperado: {self.genesis_expected_hash[:16]}...\n"
                    f"  recebido: {block['hash'][:16]}..."
                )

        expected_prev = (prev_block["hash"] if prev_block
                         else self.db.tip_hash())
        if block["prev_hash"] != expected_prev:
            return False, "prev_hash incorreto"

        expected_height = ((prev_block["height"] + 1) if prev_block
                           else self.db.height() + 1)
        if block["height"] != expected_height:
            return False, "altura invalida"

        if not meets_difficulty(block["hash"], block["difficulty"]):
            return False, "PoW invalido"

        h = block_hash(block["prev_hash"], block["merkle"],
                       block["timestamp"], block["nonce"],
                       block["difficulty"])
        if h != block["hash"]:
            return False, "hash incorreto"

        calc_merkle = compute_merkle_root(
            [t["txid"] for t in block["transactions"]])
        if calc_merkle != block["merkle"]:
            return False, "merkle incorreto"

        ok_cp, msg_cp = _verify_checkpoint_for_block(block)
        if not ok_cp:
            return False, msg_cp

        if self.require_auth_tag:
            if not self.network_secret:
                return False, "no exige auth_tag mas sem BRN_NETWORK_SECRET"
            if block["height"] > AUTH_TAG_START_HEIGHT:
                tag = block.get("auth_tag")
                if not tag:
                    return False, "bloco sem auth_tag"
                if not verify_auth_tag(block["height"], block["hash"], tag,
                                       self.network_secret):
                    return False, "auth_tag invalido (segredo errado?)"
            else:
                tag = block.get("auth_tag")
                if tag:
                    if not verify_auth_tag(block["height"], block["hash"],
                                           tag, self.network_secret):
                        return False, "auth_tag invalido em bloco legado"

        cb = block["transactions"][0]
        if cb["inputs"][0]["txid"] != "0" * 64:
            return False, "primeira tx nao e coinbase"
        if cb.get("txid") != txid(cb):
            return False, "coinbase txid invalido"

        if block["height"] > 0:
            for out in cb["outputs"]:
                if not out.get("pubkey"):
                    return False, "coinbase output sem pubkey"

        for inp in cb["inputs"]:
            if inp.get("signature"):
                return False, "coinbase input com assinatura"

        reward = self.current_reward(block["height"])
        fees = 0
        for i, t in enumerate(block["transactions"][1:], 1):
            if t["txid"] != txid(t):
                return False, "txid invalido"
            ok_tx, msg_tx = self.validate_tx(t)
            if not ok_tx:
                return False, f"tx #{i}: {msg_tx}"
            fees += self.tx_fee(t)

        total_cb = sum(o["amount"] for o in cb["outputs"])
        if total_cb > reward + fees:
            return False, "coinbase acima do permitido"

        return True, "ok"

    # ============================================================
    # ACEITAÇÃO DE BLOCO
    # ============================================================
    def accept_block(self, block: dict) -> tuple:
        prev = self.db.get_block_by_hash(block["prev_hash"])
        ok, msg = self.validate_block(block, prev)
        if not ok:
            return False, msg

        self.db.add_block(block)
        _maybe_sign_checkpoint(block)

        for i, t in enumerate(block["transactions"]):
            coinbase = (i == 0)
            self.db.apply_tx(t, block["height"], coinbase=coinbase)
            if i > 0:
                self.db.remove_mempool(t["txid"])
            if not coinbase:
                tx_data = t.get("data") or {}
                if (isinstance(tx_data, dict)
                        and tx_data.get("type") in ("deploy", "call")):
                    try:
                        self._apply_contract_tx(t)
                    except Exception as e:
                        print(f"[CONTRACT] tx {t['txid'][:16]}... falhou: {e}")

        return True, block["hash"]

    # ============================================================
    # MINERAÇÃO
    # ============================================================
    def _attach_auth_tag(self, block: dict) -> dict:
        tag = compute_auth_tag(block["height"], block["hash"],
                               self.network_secret)
        if tag:
            block["auth_tag"] = tag
        return block

    def _fire_block_mined(self, block: dict) -> None:
        cb = self.on_block_mined
        if cb is None:
            return
        try:
            cb(block)
        except Exception as e:
            print(f"[on_block_mined] callback falhou: {e}")

    def mine_block(self, miner_address: str, miner_pubkey: str) -> dict:
        if not miner_pubkey:
            raise ValueError("mine_block: miner_pubkey é obrigatória")
        return self.mine_block_interruptible(miner_address, miner_pubkey, None)

    def mine_block_interruptible(self, miner_address: str,
                                 miner_pubkey: str,
                                 should_continue=None,
                                 progress_cb=None) -> dict:
        if not miner_pubkey:
            raise ValueError(
                "mine_block_interruptible: miner_pubkey é obrigatória")

        height = self.db.height() + 1
        reward = self.current_reward(height)
        diff = self.current_difficulty()

        cb = make_coinbase(miner_address, miner_pubkey, height, reward)
        selected = self.db.all_mempool(limit=MAX_TX_PER_BLOCK - 1)
        txs = [cb] + selected

        merkle = compute_merkle_root([t["txid"] for t in txs])
        prev_hash = self.db.tip_hash()
        ts = int(time.time())

        nonce, h_hex, _ = _pow_search(
            prev_hash, merkle, ts, diff,
            should_continue=should_continue,
            progress_cb=progress_cb,
        )
        if nonce is None:
            return None

        block = {
            "height": height,
            "hash": h_hex,
            "prev_hash": prev_hash,
            "timestamp": ts,
            "nonce": nonce,
            "merkle": merkle,
            "difficulty": diff,
            "transactions": txs,
        }
        self._attach_auth_tag(block)

        ok, _msg = self.accept_block(block)
        if ok:
            self._fire_block_mined(block)
            return block
        return None

    # ============================================================
    # SNAPSHOT
    # ============================================================
    def state_snapshot(self) -> dict:
        return {
            "height": self.get_latest_height(),
            "tip_hash": self.db.tip_hash(),
            "difficulty": self.current_difficulty(),
            "cumulative_work": self.cumulative_work(),
            "mempool": self.db.mempool_count(),
            "checkpoints": len(_checkpoints),
        }


# ============================================================
# PLUGIN DO REORG (reorg.py)
# ------------------------------------------------------------
# Plugado depois da classe para manter o reorg.py como módulo
# independente. Substitui qualquer reorg_to() embutido.
# ============================================================
try:
    from reorg import reorg_to as _reorg_to, find_fork_point as _find_fork

    def _reorg_to_method(self, new_blocks, max_depth=MAX_REORG_DEPTH,
                         allow_genesis_reorg=False):
        res = _reorg_to(self, new_blocks, max_depth, allow_genesis_reorg)
        return res.to_tuple()

    def _find_fork_method(self, remote_blocks, max_depth=1000):
        return _find_fork(self, remote_blocks, max_depth)

    Blockchain.reorg_to = _reorg_to_method
    Blockchain.find_fork_point = _find_fork_method
    print("reorg.py plugado em Blockchain")
except ImportError:
    print("reorg.py nao encontrado (reorg desabilitado)")


# ============================================================
# PLUGIN DO CHAIN_VALIDATOR
# ============================================================
try:
    from chain_validator import verify_chain as _verify_ext

    def _verify_chain_method(self, full=True):
        return _verify_ext(self)

    Blockchain.verify_chain = _verify_chain_method
    Blockchain.verify_chain_dict = lambda self: _verify_ext(self).to_dict()
    print("chain_validator.py plugado em Blockchain")
except ImportError:
    print("chain_validator.py nao encontrado")


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import tempfile

    print("=" * 60)
    print(" blockchain.py — Auto-teste")
    print("=" * 60)

    os.environ.setdefault("BRN_NETWORK_SECRET",
                          "segredo-de-teste-blockchain-1234567")

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "test.db")

        print("\n[1] Instanciação + genesis:")
        bc = Blockchain(path, auto_genesis=True)
        print(f"    altura: {bc.db.height()}")
        assert bc.db.height() == 0
        print("    [ok]")

        print("\n[2] current_reward (halving):")
        for h in (0, 210_000, 420_000):
            print(f"    #{h:<8} → {bc.current_reward(h)/UNIT:.4f} BRN")
        assert bc.current_reward(0) == INITIAL_REWARD
        assert bc.current_reward(210_000) == INITIAL_REWARD // 2
        print("    [ok]")

        print(f"\n[3] current_difficulty: {bc.current_difficulty()}")
        print(f"[4] cumulative_work: {bc.cumulative_work():,}")

        print("\n[5] Mineração de 3 blocos:")
        addr = GENESIS_ADDRESS
        pub = "02" + "aa" * 32
        for i in range(3):
            blk = bc.mine_block(addr, pub)
            assert blk is not None
            print(f"    #{blk['height']} diff={blk['difficulty']} "
                  f"hash={blk['hash'][:16]}...")
        assert bc.db.height() == 3
        print("    [ok]")

        print(f"\n[6] Saldo: {bc.db.balance(addr)/UNIT:.4f} BRN")
        print("\n[7] state_snapshot:")
        for k, v in bc.state_snapshot().items():
            print(f"    {k}: {v}")

        print("\n[8] reorg_to (plugin):")
        if hasattr(bc, "reorg_to"):
            ok, msg = bc.reorg_to([])
            print(f"    reorg vazio: ok={ok} msg={msg}")
            assert not ok
            print("    [ok]")
        else:
            print("    [skip] reorg.py nao plugado")

        print("\n[9] verify_chain (plugin):")
        if hasattr(bc, "verify_chain"):
            try:
                r = bc.verify_chain()
                valid = getattr(r, "valid", bool(r))
                print(f"    verify_chain: valid={valid}")
            except Exception as e:
                print(f"    verify_chain erro: {e}")
        else:
            print("    [skip] chain_validator nao plugado")

        bc.db.close()

    print("\n" + "=" * 60)
    print(" blockchain.py OK.")
    print("=" * 60)