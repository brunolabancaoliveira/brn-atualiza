"""
security.py — Camada de segurança HTTP do BRN (v1)
================================================================
Fornece:
  * RateLimiter         — janela deslizante por IP + endpoint
  * AuditLogger         — log JSON-lines de operações sensíveis
  * require_rate_limit  — decorator Flask
  * setup_https         — gera cert autoassinado + roda em HTTPS
  * get_client_ip       — respeita X-Forwarded-For com validação
  * hash_short          — fingerprint seguro de chaves/endereços em logs
================================================================
"""
from __future__ import annotations

import os
import json
import time
import hmac
import hashlib
import socket
import threading
import ipaddress
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import request, jsonify, abort


# ============================================================
# CONFIG
# ============================================================
LOG_FILE = os.environ.get("BRN_AUDIT_LOG", "audit.log")
TRUSTED_PROXIES = set(
    p.strip() for p in
    os.environ.get("BRN_TRUSTED_PROXIES", "").split(",") if p.strip()
)
AUDIT_ENABLED = os.environ.get("BRN_AUDIT_ENABLED", "1") == "1"

# Limites padrão por endpoint (req / 60s por IP)
DEFAULT_RATE_LIMITS = {
    "transfer":      (30,  60),   # 30 req/min
    "mine":          (10,  60),   # 10 req/min
    "faucet":        (5,   60),   # 5 req/min
    "miner_start":   (5,   60),
    "miner_stop":    (20,  60),
    "miner_status":  (120, 60),
    "portfolio":     (120, 60),
    "status":        (120, 60),
    "sync_info":     (60,  60),
    "list_blocks":   (60,  60),
    "get_block":     (120, 60),
    "tx_status":     (120, 60),
    "minhas_txs":    (60,  60),
    "verificar":     (60,  60),
    "hd_create":     (5,   60),
    "hd_derive":     (10,  60),
    "fee_estimate":  (60,  60),
    "work":          (60,  60),
}


# ============================================================
# IP DO CLIENTE (respeita X-Forwarded-For SE proxy é confiável)
# ============================================================
def get_client_ip() -> str:
    """
    Retorna o IP do cliente de forma segura.
    Só confia em X-Forwarded-For se o IP direto estiver em BRN_TRUSTED_PROXIES.
    """
    direct = request.remote_addr or "0.0.0.0"
    if TRUSTED_PROXIES and direct in TRUSTED_PROXIES:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            # Pega o primeiro IP (mais à esquerda = cliente real)
            candidate = xff.split(",")[0].strip()
            try:
                ipaddress.ip_address(candidate)
                return candidate
            except ValueError:
                pass
    return direct


# ============================================================
# RATE LIMITER (janela deslizante em memória)
# ============================================================
class RateLimiter:
    """
    Janela deslizante por (ip, endpoint).
    Thread-safe. Limpa entradas antigas periodicamente.
    """

    def __init__(self, cleanup_every: int = 300):
        self._buckets: dict[tuple, deque] = defaultdict(deque)
        self._lock = threading.RLock()
        self._last_cleanup = time.time()
        self._cleanup_every = cleanup_every

    def check(self, key: str, ip: str, limit: int, window: int) -> tuple[bool, int]:
        """
        Retorna (permitido, retry_after_segundos).
        Se permitido=False, o cliente deve esperar retry_after.
        """
        now = time.time()
        bucket_key = (key, ip)
        with self._lock:
            bucket = self._buckets[bucket_key]
            # Remove expirados
            cutoff = now - window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()

            if len(bucket) >= limit:
                # Retry após o mais antigo expirar
                retry_after = max(1, int(bucket[0] + window - now))
                return False, retry_after

            bucket.append(now)
            self._maybe_cleanup(now)
            return True, 0

    def _maybe_cleanup(self, now: float):
        if now - self._last_cleanup < self._cleanup_every:
            return
        with self._lock:
            for k in list(self._buckets.keys()):
                b = self._buckets[k]
                if not b or (now - b[-1]) > 3600:
                    del self._buckets[k]
            self._last_cleanup = now

    def reset(self, key: str | None = None, ip: str | None = None):
        """Útil em testes."""
        with self._lock:
            if key is None and ip is None:
                self._buckets.clear()
                return
            for k in list(self._buckets.keys()):
                k_key, k_ip = k
                if key and k_key != key:   continue
                if ip  and k_ip  != ip:    continue
                del self._buckets[k]


_limiter = RateLimiter()


# ============================================================
# DECORATOR FLASK
# ============================================================
def require_rate_limit(endpoint_key: str):
    """
    Aplica rate limit ao endpoint Flask.
    Limites vêm de DEFAULT_RATE_LIMITS (ou customizados via env).

    Uso:
        @app.route("/api/transfer", methods=["POST"])
        @require_rate_limit("transfer")
        def transfer(): ...
    """
    limit, window = DEFAULT_RATE_LIMITS.get(endpoint_key, (60, 60))

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if request.method == "OPTIONS":
                return ("", 200)
            ip = get_client_ip()
            ok, retry = _limiter.check(endpoint_key, ip, limit, window)
            if not ok:
                audit_log("rate_limit_hit", {
                    "endpoint": endpoint_key,
                    "ip":       ip,
                    "limit":    limit,
                    "window":   window,
                })
                resp = jsonify({
                    "ok":  False,
                    "msg": f"Muitas requisições. Tente em {retry}s.",
                })
                resp.status_code = 429
                resp.headers["Retry-After"] = str(retry)
                return resp
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ============================================================
# AUDIT LOG (JSON-lines, thread-safe, append-only)
# ============================================================
_audit_lock = threading.RLock()
_audit_fp = None


def _open_audit():
    global _audit_fp
    if not AUDIT_ENABLED:
        return None
    if _audit_fp is not None:
        return _audit_fp
    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        _audit_fp = open(LOG_FILE, "a", encoding="utf-8", buffering=1)  # line-buffered
        return _audit_fp
    except Exception as e:
        print(f"[audit] falha ao abrir {LOG_FILE}: {e}")
        return None


def hash_short(value: str, length: int = 12) -> str:
    """Fingerprint seguro para logs — nunca loga a chave inteira."""
    if not value:
        return ""
    h = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return h[:length]


def audit_log(event: str, data: dict | None = None):
    """
    Registra um evento sensível em JSON-lines.
    Uso:
        audit_log("transfer_ok", {"from": addr, "to": to, "amount": 1.5})
    """
    if not AUDIT_ENABLED:
        return
    payload = {
        "ts":     datetime.now(timezone.utc).isoformat(),
        "event":  event,
        "ip":     get_client_ip() if request else "",
        "ua":     (request.headers.get("User-Agent", "")[:120]) if request else "",
        "pid":    os.getpid(),
    }
    if data:
        # Sanitiza strings longas (chaves privadas, hashes completos)
        safe = {}
        for k, v in data.items():
            if isinstance(v, str) and len(v) > 80:
                safe[k] = v[:32] + f"...({len(v)} chars)"
            else:
                safe[k] = v
        payload.update(safe)

    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with _audit_lock:
        fp = _open_audit()
        if fp:
            try:
                fp.write(line + "\n")
                fp.flush()
            except Exception:
                pass
        # Fallback para stderr
        print(f"[audit] {line}", flush=True)


def close_audit():
    global _audit_fp
    with _audit_lock:
        if _audit_fp:
            try: _audit_fp.close()
            except Exception: pass
            _audit_fp = None


# ============================================================
# HTTPS (certificado autoassinado)
# ============================================================
CERT_FILE = os.environ.get("BRN_TLS_CERT", "brn_cert.pem")
KEY_FILE  = os.environ.get("BRN_TLS_KEY",  "brn_key.pem")


def ensure_self_signed_cert(cert_file: str = CERT_FILE,
                            key_file: str = KEY_FILE,
                            hostname: str = "127.0.0.1",
                            days: int = 3650) -> tuple[str, str]:
    """
    Gera um certificado autoassinado se ainda não existir.
    Retorna (cert_path, key_path).
    Requer `cryptography`.
    """
    if os.path.exists(cert_file) and os.path.exists(key_file):
        return cert_file, key_file

    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime as _dt
    except ImportError:
        raise RuntimeError("Instale: pip install cryptography")

    print(f"[https] Gerando certificado autoassinado ({cert_file})...")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "BR"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "BRN Local Node"),
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
    ])

    try:
        san = [x509.IPAddress(ipaddress.ip_address(hostname))]
    except ValueError:
        san = [x509.DNSName(hostname)]
    san.append(x509.DNSName("localhost"))
    san.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))

    now = _dt.datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + _dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    with open(key_file, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    try: os.chmod(key_file, 0o600)
    except Exception: pass

    with open(cert_file, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    print(f"[https] OK: {cert_file} + {key_file}")
    return cert_file, key_file


def https_enabled() -> bool:
    return os.environ.get("BRN_HTTPS", "0") == "1"
