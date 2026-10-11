"""
p2p_secure.py — Handshake autenticado + canal cifrado para BRN P2P (v2)
========================================================================
Híbrido do melhor das duas implementações:

  * ChaCha20-Poly1305 (constante em tempo, sem dependência de AES-NI)
  * Duas chaves por sessão (C2S e S2C) via HKDF com info distintos
  * Assinatura do servidor inclui o eph do cliente (binds a sessão)
  * NodeIdentity persistente e criptografada (PBKDF2 + Fernet)
  * Helpers SecureClient / accept_secure para integração fácil
  * Anti-replay por contador de nonce separado por direção

Protocolo:
  Cliente -> Servidor: PROTO(9) || id_pub_A(32) || eph_A(32) || sig_A(64)
      sig_A = Ed25519(A, PROTO || id_pub_A || eph_A || ROLE_C)

  Servidor -> Cliente: PROTO(9) || id_pub_B(32) || eph_B(32) || sig_B(64)
      sig_B = Ed25519(B, PROTO || id_pub_B || eph_B || eph_A || ROLE_S)
      ^^^ assina TAMBÉM eph_A: resposta amarrada a esta sessão

  shared = X25519(eph_A, eph_B)
  salt   = SHA256(eph_A || eph_B)
  k_c2s  = HKDF(shared, salt, "BRN-P2P-C2S-v2")
  k_s2c  = HKDF(shared, salt, "BRN-P2P-S2C-v2")

  Frame: [len(4, BE)][ciphertext ChaCha20-Poly1305]
"""
from __future__ import annotations

import os
import json
import base64
import struct
import socket
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.fernet import Fernet


# ============================================================
# CONSTANTES
# ============================================================
PROTO       = b"BRN-P2P/2"
ROLE_C      = b"|client"
ROLE_S      = b"|server"
MAX_FRAME   = 4 * 1024 * 1024

HKDF_INFO_C2S = b"BRN-P2P-C2S-v2"
HKDF_INFO_S2C = b"BRN-P2P-S2C-v2"

HANDSHAKE_TIMEOUT = 10.0
RECV_TIMEOUT      = 30.0

DEFAULT_IDENTITY_FILE = "node_identity.enc"
DEFAULT_PASSWORD_ENV  = "BRN_NODE_PASSWORD"


class SecureChannelError(Exception):
    pass


# ============================================================
# HELPERS
# ============================================================
def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise SecureChannelError("conexao fechada antes do fim do frame")
        buf.extend(chunk)
    return bytes(buf)


def _derive_keys(shared: bytes, eph_a: bytes, eph_b: bytes):
    """Deriva 2 chaves (C2S e S2C) a partir do shared secret."""
    salt = hashlib.sha256(eph_a + eph_b).digest()
    k_c2s = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                 info=HKDF_INFO_C2S).derive(shared)
    k_s2c = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                 info=HKDF_INFO_S2C).derive(shared)
    return k_c2s, k_s2c


# ============================================================
# IDENTIDADE PERSISTENTE DO NÓ
# ============================================================
class NodeIdentity:
    """
    Chave Ed25519 do nó. Pode ser persistida cifrada em disco com
    a senha definida em BRN_NODE_PASSWORD.
    """

    def __init__(self, priv: Ed25519PrivateKey):
        self.priv = priv
        self.pub = priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    @property
    def pub_hex(self) -> str:
        return self.pub.hex()

    # -------- Persistência cifrada --------
    def _to_payload(self) -> bytes:
        return self.priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def save(self, path: str, password: str) -> bool:
        try:
            salt = os.urandom(16)
            kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                             salt=salt, iterations=600_000)
            key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
            token = Fernet(key).encrypt(self._to_payload())
            Path(path).write_bytes(base64.b64encode(salt + token))
            return True
        except Exception as e:
            print(f"[NodeIdentity] erro ao salvar: {e}")
            return False

    @classmethod
    def load(cls, path: str, password: str) -> "NodeIdentity | None":
        try:
            blob = base64.b64decode(Path(path).read_bytes())
            salt, token = blob[:16], blob[16:]
            kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                             salt=salt, iterations=600_000)
            key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
            payload = Fernet(key).decrypt(token)
            return cls(Ed25519PrivateKey.from_private_bytes(payload))
        except Exception as e:
            print(f"[NodeIdentity] falha ao carregar: {e}")
            return None

    @classmethod
    def load_or_create(cls,
                       path: str = DEFAULT_IDENTITY_FILE,
                       password_env: str = DEFAULT_PASSWORD_ENV,
                       ) -> "NodeIdentity":
        pw = os.environ.get(password_env, "").strip()
        if pw and Path(path).exists():
            id_ = cls.load(path, pw)
            if id_ is not None:
                return id_

        id_ = cls(Ed25519PrivateKey.generate())
        if pw:
            id_.save(path, pw)
            print(f"[NodeIdentity] nova identidade salva em {path}")
        else:
            print(f"[NodeIdentity] AVISO: {password_env} vazio — identidade efêmera")
        return id_

    def sign(self, data: bytes) -> bytes:
        return self.priv.sign(data)


# ============================================================
# CANAL SEGURO
# ============================================================
class SecureChannel:
    def __init__(self, send_key: bytes, recv_key: bytes):
        self.send_aead = ChaCha20Poly1305(send_key)
        self.recv_aead = ChaCha20Poly1305(recv_key)
        self.tx_nonce = 0
        self.rx_nonce = 0

    def _nonce(self, i: int) -> bytes:
        return i.to_bytes(12, "big")

    def send(self, sock: socket.socket, data: bytes):
        n = self._nonce(self.tx_nonce)
        self.tx_nonce += 1
        ct = self.send_aead.encrypt(n, data, None)
        sock.sendall(struct.pack(">I", len(ct)) + ct)

    def recv(self, sock: socket.socket) -> bytes:
        hdr = _recv_exact(sock, 4)
        (ln,) = struct.unpack(">I", hdr)
        if ln > MAX_FRAME + 16:
            raise SecureChannelError(f"frame grande demais: {ln}")
        ct = _recv_exact(sock, ln)
        n = self._nonce(self.rx_nonce)
        self.rx_nonce += 1
        try:
            return self.recv_aead.decrypt(n, ct, None)
        except Exception:
            raise SecureChannelError("falha ao decifrar (tag invalida)")

    def close(self, sock: socket.socket | None = None):
        if sock:
            try: sock.close()
            except Exception: pass


# ============================================================
# HANDSHAKE
# ============================================================
def handshake_client(sock: socket.socket,
                     my_id: NodeIdentity,
                     expected_pub_hex: str | None = None,
                     timeout: float = HANDSHAKE_TIMEOUT,
                     ) -> tuple[SecureChannel, str]:
    sock.settimeout(timeout)

    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes_raw()
    id_pub = my_id.pub

    sig = my_id.sign(PROTO + id_pub + eph_pub + ROLE_C)
    sock.sendall(PROTO + id_pub + eph_pub + sig)

    if _recv_exact(sock, len(PROTO)) != PROTO:
        raise SecureChannelError("proto invalido (server)")
    peer_id_pub  = _recv_exact(sock, 32)
    peer_eph_pub = _recv_exact(sock, 32)
    peer_sig     = _recv_exact(sock, 64)

    # Verifica assinatura do servidor — INCLUI o nosso eph_pub
    try:
        Ed25519PublicKey.from_public_bytes(peer_id_pub).verify(
            peer_sig,
            PROTO + peer_id_pub + peer_eph_pub + eph_pub + ROLE_S,
        )
    except Exception as e:
        raise SecureChannelError(f"assinatura do servidor invalida: {e}")

    peer_hex = peer_id_pub.hex()
    if expected_pub_hex is not None and peer_hex != expected_pub_hex:
        raise SecureChannelError(
            f"identidade do peer mudou: "
            f"esperado={expected_pub_hex[:16]}..., recebido={peer_hex[:16]}..."
        )

    shared = eph.exchange(X25519PublicKey.from_public_bytes(peer_eph_pub))
    k_c2s, k_s2c = _derive_keys(shared, eph_pub, peer_eph_pub)

    sock.settimeout(RECV_TIMEOUT)
    # Cliente envia com k_c2s, recebe com k_s2c
    return SecureChannel(send_key=k_c2s, recv_key=k_s2c), peer_hex


def handshake_server(sock: socket.socket,
                     my_id: NodeIdentity,
                     timeout: float = HANDSHAKE_TIMEOUT,
                     ) -> tuple[SecureChannel, str]:
    sock.settimeout(timeout)

    if _recv_exact(sock, len(PROTO)) != PROTO:
        raise SecureChannelError("proto invalido (client)")
    peer_id_pub  = _recv_exact(sock, 32)
    peer_eph_pub = _recv_exact(sock, 32)
    peer_sig     = _recv_exact(sock, 64)

    try:
        Ed25519PublicKey.from_public_bytes(peer_id_pub).verify(
            peer_sig,
            PROTO + peer_id_pub + peer_eph_pub + ROLE_C,
        )
    except Exception as e:
        raise SecureChannelError(f"assinatura do cliente invalida: {e}")

    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes_raw()
    id_pub = my_id.pub

    # IMPORTANTE: assina TAMBÉM o eph do cliente — binds a sessão
    sig = my_id.sign(PROTO + id_pub + eph_pub + peer_eph_pub + ROLE_S)
    sock.sendall(PROTO + id_pub + eph_pub + sig)

    shared = eph.exchange(X25519PublicKey.from_public_bytes(peer_eph_pub))
    k_c2s, k_s2c = _derive_keys(shared, peer_eph_pub, eph_pub)

    sock.settimeout(RECV_TIMEOUT)
    # Servidor recebe com k_c2s, envia com k_s2c
    return SecureChannel(send_key=k_s2c, recv_key=k_c2s), peer_id_pub.hex()


# ============================================================
# CLIENTE ONE-SHOT
# ============================================================
class SecureClient:
    """Conecta → handshake → 1 request → 1 response → fecha."""

    @staticmethod
    def request(host: str, port: int, payload: bytes,
                identity: NodeIdentity,
                timeout: float = 5.0,
                expected_pub_hex: str | None = None) -> bytes | None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, port))
            ch, _ = handshake_client(s, identity, expected_pub_hex, timeout)
            ch.send(s, payload)
            resp = ch.recv(s)
            ch.close(s)
            return resp
        except Exception:
            return None


# ============================================================
# SERVIDOR — helper para o accept
# ============================================================
def accept_secure(conn: socket.socket, identity: NodeIdentity,
                  expected_pub_hex: str | None = None,
                  timeout: float = HANDSHAKE_TIMEOUT,
                  ) -> SecureChannel | None:
    """
    Envolve uma conexão já aceita em um SecureChannel autenticado.
    Retorna None se o handshake falhar.
    """
    try:
        ch, peer_hex = handshake_server(conn, identity, timeout)
        if expected_pub_hex and peer_hex != expected_pub_hex:
            ch.close(conn)
            return None
        return ch
    except Exception:
        try: conn.close()
        except Exception: pass
        return None
