"""
p2p_cipher.py — Camada de cifra opcional para P2P BRN (v1)
================================================================
Envolve um socket TCP e adiciona ChaCha20-Poly1305 sobre os bytes
já assinados pelo p2p_auth.py. NÃO substitui o p2p_auth — soma.

Estratégia de negociação:
  1. Cliente envia: MAGIC_HELLO(8) | eph_pub(32)
  2. Servidor responde: MAGIC_OK(8) | eph_pub(32) | flag_ok(1)
     - flag_ok=1 → cifra ativa
     - flag_ok=0 → servidor não suporta; conexão continua em claro
  3. Ambos derivam chave via X25519+HKDF
  4. Todo byte subsequente é cifrado

Compatibilidade: se o servidor não responder MAGIC_OK (nó antigo),
o cliente cai para modo claro automaticamente.
"""
from __future__ import annotations

import os
import struct
import socket
import hashlib
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)

MAGIC_HELLO = b"BRNCH-HE"   # 8 bytes
MAGIC_OK    = b"BRNCH-OK"   # 8 bytes
HKDF_INFO   = b"BRN-P2P-CIPHER-v1"
MAX_FRAME   = 16 * 1024 * 1024


def cipher_enabled() -> bool:
    """Liga/desliga cifra via env (default: ligada)."""
    return os.environ.get("BRN_P2P_CIPHER", "1") == "1"


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("conexao fechada")
        buf.extend(chunk)
    return bytes(buf)


def _derive(shared: bytes, eph_a: bytes, eph_b: bytes) -> bytes:
    salt = hashlib.sha256(eph_a + eph_b).digest()
    return HKDF(algorithm=hashes.SHA256(), length=32,
                salt=salt, info=HKDF_INFO).derive(shared)


class CipherSocket:
    """
    Wrapper de socket. API idêntica (recv/sendall/close) — transparente
    para o resto do p2p_unified.py.
    """

    def __init__(self, sock: socket.socket,
                 send_key: bytes | None = None,
                 recv_key: bytes | None = None):
        self.sock = sock
        self.send_key = send_key
        self.recv_key = recv_key
        self.tx_nonce = 0
        self.rx_nonce = 0
        self.cipher_on = send_key is not None and recv_key is not None

    # --------- API compatível ---------
    def settimeout(self, t):
        self.sock.settimeout(t)

    def sendall(self, data: bytes):
        if not self.cipher_on:
            self.sock.sendall(data)
            return
        n = self.tx_nonce.to_bytes(12, "big")
        self.tx_nonce += 1
        ct = ChaCha20Poly1305(self.send_key).encrypt(n, data, None)
        self.sock.sendall(struct.pack(">I", len(ct)) + ct)

    def recv(self, bufsize: int) -> bytes:
        """
        Nota: para o wrapper ser transparente, armazenamos bytes
        decifrados num buffer interno. O recv() devolve até `bufsize`
        bytes do buffer (ou tenta ler mais se vazio).
        """
        if not self.cipher_on:
            return self.sock.recv(bufsize)

        # Se tem buffer interno, devolve
        if getattr(self, "_buf", None):
            chunk = self._buf[:bufsize]
            self._buf = self._buf[bufsize:]
            return chunk

        # Lê o próximo frame
        hdr = _recv_exact(self.sock, 4)
        (ln,) = struct.unpack(">I", hdr)
        if ln > MAX_FRAME:
            raise ConnectionError(f"frame grande: {ln}")
        ct = _recv_exact(self.sock, ln)
        n = self.rx_nonce.to_bytes(12, "big")
        self.rx_nonce += 1
        try:
            pt = ChaCha20Poly1305(self.recv_key).decrypt(n, ct, None)
        except Exception:
            raise ConnectionError("falha ao decifrar (tag invalida)")
        self._buf = pt
        chunk = self._buf[:bufsize]
        self._buf = self._buf[bufsize:]
        return chunk

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ============================================================
# HANDSHAKE
# ============================================================
def wrap_client(sock: socket.socket, timeout: float = 5.0):
    """
    Cliente: envia HELLO, espera OK. Se o servidor não suportar,
    devolve CipherSocket em modo claro (compatível).
    """
    if not cipher_enabled():
        return CipherSocket(sock)

    try:
        sock.settimeout(timeout)
        eph = X25519PrivateKey.generate()
        eph_pub = eph.public_key().public_bytes_raw()
        sock.sendall(MAGIC_HELLO + eph_pub)

        try:
            resp_magic = _recv_exact(sock, 8)
        except (ConnectionError, socket.timeout):
            return CipherSocket(sock)  # servidor antigo

        if resp_magic != MAGIC_OK:
            # Servidor antigo — cai para claro
            return CipherSocket(sock)

        s_eph_pub = _recv_exact(sock, 32)
        flag = _recv_exact(sock, 1)
        if flag != b"\x01":
            return CipherSocket(sock)

        shared = eph.exchange(X25519PublicKey.from_public_bytes(s_eph_pub))
        k_c2s = _derive(shared, eph_pub, s_eph_pub)
        k_s2c = _derive(shared, s_eph_pub, eph_pub)
        return CipherSocket(sock, send_key=k_c2s, recv_key=k_s2c)
    except Exception:
        return CipherSocket(sock)


def wrap_server(conn: socket.socket, timeout: float = 5.0):
    """
    Servidor: espera HELLO. Se o cliente não mandar, devolve socket
    em modo claro (compatível com clientes antigos).
    """
    if not cipher_enabled():
        return CipherSocket(conn)

    try:
        conn.settimeout(timeout)
        magic = _recv_exact(conn, 8)
        if magic != MAGIC_HELLO:
            # Cliente antigo — devolve os bytes já lidos junto
            # para o código seguinte processar normalmente
            return CipherSocket(conn), magic
        c_eph_pub = _recv_exact(conn, 32)

        eph = X25519PrivateKey.generate()
        eph_pub = eph.public_key().public_bytes_raw()
        conn.sendall(MAGIC_OK + eph_pub + b"\x01")

        shared = eph.exchange(X25519PublicKey.from_public_bytes(c_eph_pub))
        k_c2s = _derive(shared, c_eph_pub, eph_pub)
        k_s2c = _derive(shared, eph_pub, c_eph_pub)
        return CipherSocket(conn, send_key=k_s2c, recv_key=k_c2s), b""
    except Exception:
        return CipherSocket(conn), b""
