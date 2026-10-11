"""
checkpoints.py — Checkpoints assinados do BRN
============================================================
A cada CHECKPOINT_INTERVAL blocos, o no-origem assina o hash do bloco
com sua chave Ed25519. Todos os nos verificam a assinatura antes de
aceitar blocos naquela altura.

Efeito: reorg nao pode violar um checkpoint. Um atacante com 100x
mais hashrate ainda nao consegue reescrever a cadeia — os checkpoints
sao imutaveis.

Formato de checkpoints.json:
{
  "1000": {"height":1000, "hash":"...", "sig":"...", "pub":"...", "ts":123},
  "2000": {...}
}
"""
import os
import json
import time
import threading

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

CHECKPOINT_FILE = "checkpoints.json"
CHECKPOINT_DOMAIN = b"BRN-CHECKPOINT-v1|"

_lock = threading.Lock()


def _payload(height, block_hash):
    return CHECKPOINT_DOMAIN + f"{int(height)}:{block_hash}".encode()


def load_checkpoints() -> dict:
    """Le checkpoints.json (dict height_str -> {height,hash,sig,pub,ts})."""
    if not os.path.exists(CHECKPOINT_FILE):
        return {}
    try:
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_checkpoints(d: dict) -> bool:
    """Grava atomicamente (temp + replace)."""
    with _lock:
        try:
            tmp = CHECKPOINT_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2, sort_keys=True)
            os.replace(tmp, CHECKPOINT_FILE)
            return True
        except Exception as e:
            print(f"[CHECKPOINT] erro ao salvar: {e}")
            return False


def sign_checkpoint(height: int, block_hash: str, priv) -> dict:
    """Assina o checkpoint. 'priv' e uma Ed25519PrivateKey."""
    if priv is None:
        return {}
    try:
        sig = priv.sign(_payload(height, block_hash)).hex()
        pub = priv.public_key().public_bytes_raw().hex()
        return {
            "height": int(height),
            "hash": block_hash,
            "sig": sig,
            "pub": pub,
            "ts": int(time.time()),
        }
    except Exception as e:
        print(f"[CHECKPOINT] erro ao assinar: {e}")
        return {}


def verify_checkpoint(cp: dict) -> bool:
    """Verifica a assinatura Ed25519 do checkpoint."""
    if not isinstance(cp, dict):
        return False
    try:
        height = int(cp["height"])
        block_hash = str(cp["hash"])
        sig = bytes.fromhex(cp["sig"])
        pub = bytes.fromhex(cp["pub"])
    except Exception:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(
            sig, _payload(height, block_hash)
        )
        return True
    except Exception:
        return False


def merge_checkpoints(local: dict, remote: dict) -> dict:
    """
    Une dois dicts. Em conflito de altura, mantem o local
    (o remoto so completa alturas que ainda nao temos).
    """
    merged = dict(remote or {})
    merged.update(local or {})
    return merged
