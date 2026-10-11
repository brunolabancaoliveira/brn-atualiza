"""
wallet.py — Carteira BRN (v8 - FINAL)
================================================================
v8:
  - Base v7 intacta (secure_store Argon2id + ChaCha20 + 0600 + atomico)
  - ADDON: current_wallet.enc (carteira ativa CIFRADA, não mais texto puro)
  - ADDON: WalletSession com auto-lock em RAM (15 min)
  - ADDON: copy_to_clipboard - copia endereco/txid/seed 1 clique
  - ADDON: Infisical - puxa BRN_PRIVATE_KEY do cloud criptografado
  - Compatível com .bat de 1 clique

v7: Armazenamento local via secure_store.py
v5: HDWalletManager BIP39 + BIP44
================================================================
"""
import json
import base64
import os
import hashlib
import hmac as hmac_lib
import platform
import subprocess
import sys

from crypto import (
    sha256, pubkey_from_priv, sign_schnorr, verify_schnorr,
    sign_ecdsa, verify_ecdsa, generate_private_key,
)
from bech32 import address_from_pubkey

from secure_store import (
    # ---- v7 (inalterado) ----
    encrypt_blob as _ss_encrypt,
    decrypt_blob as _ss_decrypt,
    MAGIC as _SS_MAGIC,
    # ---- v8 (novo) ----
    WalletSession,
    save_current_wallet   as _ss_save_current,
    load_current_wallet   as _ss_load_current,
    migrate_current_wallet as _ss_migrate_current,
    constant_time_eq,
)

SIG_MODE = "schnorr"


# ============================================================
# ADDON - COPIAR PARA ÁREA DE TRANSFERÊNCIA
# ============================================================
def copy_to_clipboard(texto: str) -> bool:
    """Copia para clipboard - funciona Windows/Linux/Mac sem instalar nada"""
    texto = str(texto).strip()
    if not texto:
        return False
    try:
        import pyperclip
        pyperclip.copy(texto)
        print(f"[wallet] [ok] Copiado: {texto[:12]}...{texto[-6:]}")
        return True
    except Exception:
        pass
    try:
        sistema = platform.system()
        if sistema == "Windows":
            subprocess.run("clip", input=texto.encode('utf-8'), check=True, shell=True)
            return True
        elif sistema == "Darwin":
            subprocess.run("pbcopy", input=texto.encode('utf-8'), check=True)
            return True
        else:
            for cmd in ["xclip -selection clipboard", "xsel --clipboard --input", "wl-copy"]:
                try:
                    subprocess.run(cmd, input=texto.encode('utf-8'), check=True, shell=True)
                    return True
                except Exception:
                    continue
            import tkinter as tk
            r = tk.Tk(); r.withdraw(); r.clipboard_clear(); r.clipboard_append(texto); r.update(); r.destroy()
            return True
    except Exception as e:
        print(f"[wallet] [!] Falha ao copiar: {e}")
        print(f"[wallet] Copie manualmente: {texto}")
        return False


# ============================================================
# ADDON - INFISICAL (puxa chave segura sem expor no GitHub)
# ============================================================
def _get_secret_infisical(secret_name: str):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    try:
        import requests
    except Exception:
        return None

    token      = os.getenv("INFISICAL_TOKEN")
    project_id = os.getenv("INFISICAL_PROJECT_ID")
    env        = os.getenv("INFISICAL_ENV", "dev")

    if not token or not project_id:
        return None
    if not token.startswith("st."):
        return None

    try:
        url = "https://app.infisical.com/api/v3/secrets/raw"
        params = {
            "secretName": secret_name,
            "workspaceId": project_id,
            "environment": env,
            "secretPath": "/",
        }
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json()["secret"]["secretValue"]
    except Exception:
        pass
    return None


def get_secure_brn_key():
    """Tenta Infisical primeiro, depois .env local."""
    for nome in ["BRN_PRIVATE_KEY", "BTC_WIF", "BTC_SEED", "BRN_WIF"]:
        v = _get_secret_infisical(nome)
        if v and len(v) > 10:
            print(f"[wallet] [ok] Chave {nome} carregada do Infisical")
            return v
    for nome in ["BRN_PRIVATE_KEY", "BRN_WIF", "BTC_WIF"]:
        v = os.getenv(nome)
        if v and len(v) > 10 and "SEU_" not in v:
            print(f"[wallet] [ok] Chave {nome} do .env local")
            return v
    return None


# ============================================================
# WALLET SIMPLES (chave unica)
# ============================================================
class Wallet:
    def __init__(self, private_key_hex: str | None = None):
        if private_key_hex:
            self.priv = bytes.fromhex(private_key_hex)
        else:
            self.priv = generate_private_key()
        self.pub     = pubkey_from_priv(self.priv)
        self.address = address_from_pubkey(self.pub)

    @property
    def priv_hex(self) -> str:
        return self.priv.hex()

    @property
    def pub_hex(self) -> str:
        return self.pub.hex()

    def private_key_hex(self) -> str:
        return self.priv_hex

    def public_key_hex(self) -> str:
        return self.pub_hex

    # --------------------------------------------------------
    # COPIAR
    # --------------------------------------------------------
    def copy_address(self) -> str:
        copy_to_clipboard(self.address)
        print(f"[wallet] Endereço copiado: {self.address}")
        return self.address

    def copy_pubkey(self) -> str:
        copy_to_clipboard(self.pub_hex)
        print(f"[wallet] Pubkey copiada")
        return self.pub_hex

    # --------------------------------------------------------
    # ASSINATURA
    # --------------------------------------------------------
    def sign(self, msg_hash: bytes) -> str:
        if SIG_MODE == "schnorr":
            return sign_schnorr(self.priv, msg_hash).hex()
        return sign_ecdsa(self.priv, msg_hash).hex()

    @staticmethod
    def verify(msg_hash: bytes, sig_hex: str, pub_hex: str) -> bool:
        try:
            sig = bytes.fromhex(sig_hex)
            pub = bytes.fromhex(pub_hex)
            if SIG_MODE == "schnorr":
                return verify_schnorr(pub, sig, msg_hash)
            return verify_ecdsa(pub, sig, msg_hash)
        except Exception:
            return False

    # --------------------------------------------------------
    # SERIALIZACAO
    # --------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "private_key": self.priv_hex,
            "address":     self.address,
            "pubkey":      self.pub_hex,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Wallet":
        return cls(private_key_hex=d["private_key"])

    # --------------------------------------------------------
    # CIFRAGEM LOCAL
    # --------------------------------------------------------
    def export_encrypted(self, password: str) -> str:
        data = json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True).encode("utf-8")
        blob = _ss_encrypt(data, password)
        return base64.b64encode(blob).decode("ascii")

    @classmethod
    def import_encrypted(cls, blob_b64: str, password: str) -> "Wallet":
        raw = base64.b64decode(blob_b64)
        if raw.startswith(_SS_MAGIC):
            data = json.loads(_ss_decrypt(raw, password).decode("utf-8"))
        else:
            data = cls._import_legacy_fernet(raw, password)
        return cls.from_dict(data)

    @staticmethod
    def _import_legacy_fernet(raw: bytes, password: str) -> dict:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        salt, token = raw[:16], raw[16:]
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000)
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        return json.loads(Fernet(key).decrypt(token).decode("utf-8"))

    def is_legacy_format(self, blob_b64: str) -> bool:
        try:
            raw = base64.b64decode(blob_b64)
            return not raw.startswith(_SS_MAGIC)
        except Exception:
            return False


# ============================================================
# WALLET MANAGER
# ============================================================
class WalletManager:
    WALLETS_DIR = os.environ.get("BRN_WALLETS_DIR", "wallets")

    # ---------- Diretório ----------
    @staticmethod
    def _garantir_dir():
        os.makedirs(WalletManager.WALLETS_DIR, exist_ok=True)

    # ---------- Geração ----------
    @staticmethod
    def generate_keypair() -> dict:
        w = Wallet()
        return {"address": w.address, "private_key": w.priv_hex, "public_key": w.pub_hex}

    # ---------- Validação ----------
    @staticmethod
    def validate_address(addr: str) -> bool:
        try:
            from bech32 import validate_address as _v
            return _v(addr)
        except Exception:
            return isinstance(addr, str) and addr.startswith("brn1") and len(addr) > 20

    # ---------- Escrita atômica (texto) ----------
    @staticmethod
    def _atomic_write(path: str, content: str) -> None:
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            try: os.unlink(tmp)
            except Exception: pass
            raise
        os.replace(tmp, path)
        try: os.chmod(path, 0o600)
        except Exception: pass

    # ---------- Carteiras nomeadas (wallets/<nome>.wallet) ----------
    @staticmethod
    def save_encrypted_wallet(filename: str, password: str,
                              address: str, sk: str, pk: str) -> dict:
        try:
            WalletManager._garantir_dir()
            if not filename.endswith(".wallet"):
                filename += ".wallet"
            path = os.path.join(WalletManager.WALLETS_DIR, filename)
            w = Wallet(private_key_hex=sk)
            blob_b64 = w.export_encrypted(password)
            WalletManager._atomic_write(path, blob_b64)
            return {"ok": True, "msg": f"Carteira salva em {path}", "path": path}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    @staticmethod
    def load_encrypted_wallet(filename: str, password: str,
                              auto_migrate: bool = True) -> dict:
        try:
            if not filename.endswith(".wallet"):
                filename += ".wallet"
            path = os.path.join(WalletManager.WALLETS_DIR, filename)
            with open(path, "r", encoding="utf-8") as f:
                blob_b64 = f.read()
            w = Wallet.import_encrypted(blob_b64, password)
            if auto_migrate and w.is_legacy_format(blob_b64):
                try:
                    new_blob = w.export_encrypted(password)
                    WalletManager._atomic_write(path, new_blob)
                    print(f"[wallet] migrado para formato v8: {path}")
                except Exception as e:
                    print(f"[wallet] aviso: falha ao migrar {path}: {e}")
            return {"ok": True, "address": w.address,
                    "private_key": w.priv_hex, "public_key": w.pub_hex}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    @staticmethod
    def list_wallets() -> list:
        try:
            WalletManager._garantir_dir()
            out = []
            for fname in sorted(os.listdir(WalletManager.WALLETS_DIR)):
                if not fname.endswith(".wallet"):
                    continue
                path = os.path.join(WalletManager.WALLETS_DIR, fname)
                out.append({
                    "filename": fname,
                    "size": os.path.getsize(path),
                    "modified": os.path.getmtime(path),
                })
            return out
        except Exception:
            return []

    @staticmethod
    def copy_wallet_address(filename: str, password: str) -> bool:
        res = WalletManager.load_encrypted_wallet(filename, password)
        if res.get("ok"):
            return copy_to_clipboard(res["address"])
        return False

    # ============================================================
    # v8: CARTEIRA ATIVA CIFRADA (current_wallet.enc)
    # ============================================================
    @staticmethod
    def save_current(address: str, sk: str, pk: str, password: str) -> dict:
        """Grava a carteira ativa CIFRADA (substitui current_wallet.json)."""
        return _ss_save_current(password, {
            "address":     address,
            "private_key": sk,
            "public_key":  pk,
        })

    @staticmethod
    def load_current(password: str) -> dict:
        """Carrega a carteira ativa cifrada."""
        return _ss_load_current(password)

    @staticmethod
    def migrate_legacy_current(password: str) -> dict:
        """Migra current_wallet.json legado → current_wallet.enc."""
        return _ss_migrate_current(password)

    # ============================================================
    # v8: SESSÃO EM RAM COM AUTO-LOCK
    # ============================================================
    _session: "WalletSession | None" = None

    @classmethod
    def session(cls) -> WalletSession:
        if cls._session is None:
            cls._session = WalletSession(ttl=15 * 60)
        return cls._session

    @classmethod
    def unlock_session(cls, password: str) -> dict:
        """Carrega a carteira cifrada e coloca em RAM com TTL de 15 min."""
        r = cls.load_current(password)
        if not r.get("ok"):
            return r
        cls.session().unlock({
            "address":     r.get("address", ""),
            "private_key": r.get("private_key", ""),
            "public_key":  r.get("public_key", ""),
        })
        return {"ok": True, "seconds_until_lock": cls.session().seconds_until_lock()}

    @classmethod
    def lock_session(cls) -> None:
        cls.session().lock()

    @classmethod
    def session_wallet(cls) -> dict | None:
        return cls.session().get()

    @classmethod
    def session_seconds_left(cls) -> int:
        return cls.session().seconds_until_lock()


# ============================================================
# HD WALLET (BIP39 + BIP44)
# ============================================================
class HDWalletManager:
    PURPOSE = 44
    COIN_TYPE = 0
    ACCOUNT = 0
    CHANGE = 0
    _SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

    @staticmethod
    def _m():
        try:
            from mnemonic import Mnemonic
            return Mnemonic("english")
        except ImportError:
            raise ImportError("Instale: python -m pip install mnemonic")

    @staticmethod
    def create(strength=128):
        m = HDWalletManager._m()
        mnemonic_phrase = m.generate(strength=strength)
        return HDWalletManager.from_mnemonic(mnemonic_phrase, index=0)

    @staticmethod
    def from_mnemonic(mnemonic_phrase, index=0, passphrase=""):
        m = HDWalletManager._m()
        if not m.check(mnemonic_phrase):
            raise ValueError("Mnemonico invalido")
        seed = m.to_seed(mnemonic_phrase, passphrase=passphrase)
        I = hmac_lib.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
        master_key = int.from_bytes(I[:32], "big")
        master_chain = I[32:]
        path = [
            HDWalletManager.PURPOSE + 0x80000000,
            HDWalletManager.COIN_TYPE + 0x80000000,
            HDWalletManager.ACCOUNT + 0x80000000,
            HDWalletManager.CHANGE,
            index,
        ]
        key = master_key
        chain = master_chain
        for child in path:
            if child >= 0x80000000:
                data = b"\x00" + key.to_bytes(32, "big") + child.to_bytes(4, "big")
            else:
                pub = pubkey_from_priv(key.to_bytes(32, "big"))
                data = pub + child.to_bytes(4, "big")
            I = hmac_lib.new(chain, data, hashlib.sha512).digest()
            key = (int.from_bytes(I[:32], "big") + key) % HDWalletManager._SECP256K1_N
            if key == 0:
                raise ValueError("derivacao BIP32 produziu chave invalida")
            chain = I[32:]
        w = Wallet(private_key_hex=key.to_bytes(32, "big").hex())
        return {
            "mnemonic":    mnemonic_phrase,
            "address":     w.address,
            "private_key": w.priv_hex,
            "public_key":  w.pub_hex,
            "index":       index,
            "path": f"m/44'/{HDWalletManager.COIN_TYPE}'/0'/0/{index}",
        }

    @staticmethod
    def derive_many(mnemonic_phrase, count=5):
        if count < 1 or count > 100:
            raise ValueError("count deve estar entre 1 e 100")
        return [HDWalletManager.from_mnemonic(mnemonic_phrase, index=i) for i in range(count)]

    @staticmethod
    def validate_mnemonic(mnemonic_phrase):
        try:
            m = HDWalletManager._m()
            return m.check(mnemonic_phrase)
        except Exception:
            return False


# ============================================================
# HELPERS DE TRANSACAO
# ============================================================
def sign_transaction(wallet: Wallet, tx: dict) -> dict:
    from blockchain import signing_hash, txid
    tx = dict(tx)
    pub_hex = wallet.pub_hex
    new_inputs = []
    for inp in tx["inputs"]:
        ni = dict(inp)
        ni["pubkey"] = pub_hex
        ni.setdefault("signature", "")
        new_inputs.append(ni)
    tx["inputs"] = new_inputs
    h = signing_hash(tx)
    sig_hex = wallet.sign(h)
    for inp in tx["inputs"]:
        inp["signature"] = sig_hex
    tx["txid"] = txid(tx)
    return tx


def verify_transaction(tx: dict) -> tuple:
    try:
        from blockchain import signing_hash
        h = signing_hash(tx)
        for i, inp in enumerate(tx.get("inputs", [])):
            sig = inp.get("signature", "")
            pk = inp.get("pubkey", "")
            if not sig or not pk:
                return False, f"input[{i}] sem assinatura ou pubkey"
            if not Wallet.verify(h, sig, pk):
                return False, f"input[{i}] assinatura invalida"
        return True, "ok"
    except Exception as e:
        return False, str(e)


def sign_and_verify(wallet: Wallet, tx: dict) -> tuple:
    signed = sign_transaction(wallet, tx)
    ok, msg = verify_transaction(signed)
    return signed, ok, msg


# ============================================================
# FUNÇÕES RÁPIDAS PARA .BAT (1 CLIQUE)
# ============================================================
def quick_copy_address():
    """Usado pelo .bat: python wallet.py --copy"""
    sk = get_secure_brn_key()
    if sk:
        w = Wallet(private_key_hex=sk)
        copy_to_clipboard(w.address)
        print(w.address)
        return w.address

    try:
        pw = os.getenv("BRN_WALLET_SESSION_PASS", "").strip()
        if pw:
            res = WalletManager.load_current(pw)
            if res.get("ok"):
                copy_to_clipboard(res["address"])
                print(res["address"])
                return res["address"]
    except Exception:
        pass

    print("[wallet] Nenhuma carteira encontrada")
    return None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="BRN Wallet v8")
    parser.add_argument("--copy", action="store_true", help="Copia endereço")
    parser.add_argument("--copy-txid", type=str, help="Copia TXID")
    parser.add_argument("--migrate-current", metavar="SENHA",
                        help="Migra current_wallet.json para .enc (UMA VEZ)")
    args = parser.parse_args()

    if args.copy:
        quick_copy_address()
    elif args.copy_txid:
        copy_to_clipboard(args.copy_txid)
        print(f"TXID copiado: {args.copy_txid}")
    elif args.migrate_current:
        r = WalletManager.migrate_legacy_current(args.migrate_current)
        print(r)
    else:
        print("BRN Wallet v8 - OK")
        sk = get_secure_brn_key()
        if sk:
            w = Wallet(private_key_hex=sk)
            print(f"Endereço Infisical: {w.address}")
            w.copy_address()
