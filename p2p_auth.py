"""
p2p_auth.py — Autenticação Ed25519 para o P2P BRN (v3)
================================================================
Fornece:
  - auth_enabled()         → bool: autenticação ligada?
  - auth_required()        → bool: autenticação obrigatória?
  - build_auth(msg=...)    → dict: assina o hash do payload
  - verify_auth(auth,msg=) → (bool, str): verifica assinatura + hash + replay
  - set_node_id_priv(priv) → registra chave privada do nó
  - reload_config()        → relê env sem reiniciar o processo

Modos (env BRN_P2P_AUTH):
  "off"      → desliga autenticação
  "optional" → aceita peers sem auth (default)
  "required" → rejeita peers sem auth

Anti-replay:
  - Cada auth tem `ts` (timestamp Unix) e `nonce` (hex aleatório)
  - Janela máxima: BRN_P2P_AUTH_WINDOW segundos (default 30)
  - Cache LRU: BRN_P2P_NONCE_CACHE entradas (default 10000)
  - Nonces repetidos na janela são rejeitados

Anti payload-swap:
  - Payload serializado canonicamente (JSON sort_keys, sem espaços)
  - SHA256 do payload entra na mensagem assinada
  - Se um MITM trocar o payload mantendo `_auth`, o hash muda e falha

v3:
  [FIX] config lida lazy — reload_config() relê env sem reiniciar
  [FIX] ordem: nonce consumido SÓ depois da assinatura válida
        (antes: nonce era queimado por atacante com sig inválida)
  [FIX] nonce vazio agora rejeitado (antes era aceito como "sem replay")
  [FIX] limites de tamanho em pub/sig/nonce/msg_hash (anti-DoS de memória)
  [FIX] Ed25519PublicKey importado de crypto OU cryptography (fallback)
  [FIX] _msg_hash levanta em erro de serialização (antes retornava 0*64)
  [NEW] anti-DoS: bloqueia pubkey após N falhas de assinatura em janela
  [NEW] info() com config resolvida
  [NEW] CLI: --status / --info / --test / --self-sign

Env:
  BRN_P2P_AUTH             off|optional|required (default optional)
  BRN_P2P_AUTH_WINDOW      janela em segundos (default 30)
  BRN_P2P_NONCE_CACHE      tamanho do cache LRU (default 10000)
  BRN_P2P_FAIL_THRESHOLD   falhas antes de bloquear pubkey (default 10)
  BRN_P2P_FAIL_BLOCK_S     duração do bloqueio em segundos (default 300)
================================================================
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from typing import Optional

log = logging.getLogger("p2p_auth")


# ============================================================
# CONFIG (lazy — reload_config() relê)
# ============================================================
_CONFIG: dict = {}


def reload_config() -> dict:
    """
    Lê env vars novamente. Chamado no import e pode ser chamado
    manualmente se o usuário mudar env em runtime.
    """
    mode_raw = (os.environ.get("BRN_P2P_AUTH", "optional") or "").strip().lower()
    if mode_raw not in ("off", "optional", "required"):
        log.warning("BRN_P2P_AUTH inválido ('%s') — usando 'optional'", mode_raw)
        mode_raw = "optional"

    try:
        window = int(os.environ.get("BRN_P2P_AUTH_WINDOW", "30"))
        if window < 5 or window > 3600:
            raise ValueError
    except (ValueError, TypeError):
        log.warning("BRN_P2P_AUTH_WINDOW inválido — usando 30")
        window = 30

    try:
        cache_size = int(os.environ.get("BRN_P2P_NONCE_CACHE", "10000"))
        if cache_size < 100:
            raise ValueError
    except (ValueError, TypeError):
        log.warning("BRN_P2P_NONCE_CACHE inválido — usando 10000")
        cache_size = 10000

    try:
        fail_threshold = int(os.environ.get("BRN_P2P_FAIL_THRESHOLD", "10"))
    except (ValueError, TypeError):
        fail_threshold = 10

    try:
        fail_block_s = int(os.environ.get("BRN_P2P_FAIL_BLOCK_S", "300"))
    except (ValueError, TypeError):
        fail_block_s = 300

    _CONFIG.clear()
    _CONFIG.update({
        "mode": mode_raw,
        "window": window,
        "cache_size": cache_size,
        "fail_threshold": fail_threshold,
        "fail_block_s": fail_block_s,
    })
    return dict(_CONFIG)


# Chama no import (mas pode ser chamado de novo depois)
reload_config()


# ============================================================
# CONSTANTES
# ============================================================
_DOMAIN = b"BRN-P2P-AUTH-v3|"

# Limites anti-DoS
MAX_PUB_LEN       = 128       # hex: 64 chars (32 bytes)
MAX_SIG_LEN       = 256       # hex: 128 chars (64 bytes)
MAX_NONCE_LEN     = 128       # hex: 32 chars (16 bytes)
MAX_MSG_HASH_LEN  = 128       # hex: 64 chars (sha256)
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024   # 4 MB


# ============================================================
# IMPORTAÇÃO DE Ed25519PublicKey (com fallback)
# ============================================================
_Ed25519PublicKey = None
_CRYPTO_SOURCE = ""

try:
    from crypto import Ed25519PublicKey as _Ed25519PublicKey
    _CRYPTO_SOURCE = "crypto"
except Exception:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey as _Ed25519PublicKey,
        )
        _CRYPTO_SOURCE = "cryptography"
    except Exception:
        _Ed25519PublicKey = None
        _CRYPTO_SOURCE = "(indisponível)"
        log.error("p2p_auth: Ed25519PublicKey indisponível — "
                  "verificação de assinatura vai falhar")


# ============================================================
# ESTADO GLOBAL
# ============================================================
_node_id_priv = None
_node_id_pub_hex = ""
_node_lock = threading.RLock()

# Cache LRU de nonces
_nonce_cache: "OrderedDict[str, float]" = OrderedDict()
_nonce_lock = threading.RLock()

# Anti-DoS: rastreia falhas por pubkey
# pub_hex -> {"count": int, "first_ts": float, "blocked_until": float}
_fail_tracker: dict = {}
_fail_lock = threading.RLock()


# ============================================================
# CONFIGURAÇÃO PÚBLICA
# ============================================================
def auth_enabled() -> bool:
    """Autenticação está ligada?"""
    return _CONFIG.get("mode") in ("optional", "required")


def auth_required() -> bool:
    """Autenticação é obrigatória?"""
    return _CONFIG.get("mode") == "required"


def set_node_id_priv(priv) -> None:
    """Registra a chave privada Ed25519 deste nó."""
    global _node_id_priv, _node_id_pub_hex
    with _node_lock:
        _node_id_priv = priv
        try:
            _node_id_pub_hex = priv.public_key().public_bytes_raw().hex()
        except Exception:
            _node_id_pub_hex = ""
    if _node_id_pub_hex:
        log.info("p2p_auth: chave registrada pub=%s...", _node_id_pub_hex[:16])
    else:
        log.warning("p2p_auth: chave privada registrada mas pubkey falhou")


def get_node_id_pub() -> str:
    """Retorna a pubkey hex do próprio nó."""
    return _node_id_pub_hex


# ============================================================
# HELPERS INTERNOS
# ============================================================
def _canonical_json(obj) -> bytes:
    """Serialização canônica para assinatura."""
    return json.dumps(
        obj,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")


def _msg_hash(msg) -> str:
    """
    SHA256 hex do payload canônico.
    Remove '_auth' se presente (não assina a própria assinatura).

    Levanta ValueError se não conseguir serializar (antes retornava
    "0"*64, mascarando bugs).
    """
    if msg is None:
        return "0" * 64
    if not isinstance(msg, dict):
        raise ValueError("msg deve ser dict ou None")
    clean = {k: v for k, v in msg.items() if k != "_auth"}
    raw = _canonical_json(clean)
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload grande demais: {len(raw)}B")
    return hashlib.sha256(raw).hexdigest()


def _sign(data: bytes) -> str:
    """Assina com a chave privada do nó. Retorna hex."""
    priv = _node_id_priv
    if priv is None:
        raise RuntimeError("p2p_auth: chave privada não registrada")
    sig = priv.sign(data)
    if isinstance(sig, (bytes, bytearray)):
        return sig.hex()
    return str(sig)


def _verify_sig(pub_hex: str, sig_hex: str, data: bytes) -> bool:
    """Verifica assinatura Ed25519. Aceita pubkey e sig em hex."""
    if _Ed25519PublicKey is None:
        return False
    if not pub_hex or not sig_hex:
        return False
    try:
        pub_bytes = bytes.fromhex(pub_hex)
        sig_bytes = bytes.fromhex(sig_hex)
    except (ValueError, TypeError):
        return False

    if len(pub_bytes) != 32 or len(sig_bytes) != 64:
        return False

    try:
        pk = _Ed25519PublicKey.from_public_bytes(pub_bytes)
        pk.verify(sig_bytes, data)
        return True
    except Exception:
        return False


# ============================================================
# ANTI-DOS (falhas por pubkey)
# ============================================================
def _is_blocked(pub_hex: str) -> bool:
    if not pub_hex:
        return False
    now = time.time()
    with _fail_lock:
        entry = _fail_tracker.get(pub_hex)
        if not entry:
            return False
        return entry.get("blocked_until", 0) > now


def _record_failure(pub_hex: str) -> None:
    if not pub_hex:
        return
    now = time.time()
    threshold = _CONFIG.get("fail_threshold", 10)
    block_s = _CONFIG.get("fail_block_s", 300)

    with _fail_lock:
        entry = _fail_tracker.get(pub_hex)
        if entry is None or (now - entry.get("first_ts", 0)) > block_s:
            _fail_tracker[pub_hex] = {
                "count": 1,
                "first_ts": now,
                "blocked_until": 0,
            }
            return
        entry["count"] += 1
        if entry["count"] >= threshold:
            entry["blocked_until"] = now + block_s
            log.warning("p2p_auth: pubkey %s... bloqueada por %ds "
                        "(%d falhas)", pub_hex[:16], block_s, entry["count"])


def _clear_failures(pub_hex: str) -> None:
    if not pub_hex:
        return
    with _fail_lock:
        _fail_tracker.pop(pub_hex, None)


def _prune_fail_tracker() -> None:
    """Remove entradas antigas do tracker de falhas."""
    now = time.time()
    ttl = _CONFIG.get("fail_block_s", 300) * 3
    with _fail_lock:
        for k in list(_fail_tracker.keys()):
            entry = _fail_tracker[k]
            if (now - entry.get("first_ts", 0)) > ttl and \
               entry.get("blocked_until", 0) < now:
                del _fail_tracker[k]


# ============================================================
# CACHE DE NONCES
# ============================================================
def _nonce_seen(nonce: str, ts: int) -> bool:
    """
    Retorna True se o nonce JÁ FOI VISTO (replay).
    NÃO insere — só consulta.
    """
    if not nonce:
        return True

    window = _CONFIG.get("window", 30)
    bucket = ts // max(1, window)
    key = f"{nonce}|{bucket}"

    now = time.time()
    with _nonce_lock:
        cutoff = now - (window * 2)
        while _nonce_cache:
            k, t = next(iter(_nonce_cache.items()))
            if t < cutoff:
                _nonce_cache.popitem(last=False)
            else:
                break

        if key in _nonce_cache:
            _nonce_cache.move_to_end(key)
            return True
        return False


def _nonce_mark(nonce: str, ts: int) -> None:
    """Insere o nonce no cache (chamado SÓ após validação completa)."""
    if not nonce:
        return

    window = _CONFIG.get("window", 30)
    bucket = ts // max(1, window)
    key = f"{nonce}|{bucket}"

    now = time.time()
    cache_size = _CONFIG.get("cache_size", 10000)

    with _nonce_lock:
        _nonce_cache[key] = now
        _nonce_cache.move_to_end(key)
        while len(_nonce_cache) > cache_size:
            _nonce_cache.popitem(last=False)


# ============================================================
# BUILD AUTH
# ============================================================
def build_auth(msg=None) -> dict:
    """
    Cria o bloco `_auth` para um payload.

    Retorna:
        {
          "pub": "<hex>",
          "ts": <unix ts>,
          "nonce": "<hex 32>",
          "msg_hash": "<sha256 hex>",
          "sig": "<hex>"
        }

    Assinatura cobre:
        DOMAIN || pub || "|" || ts || "|" || nonce || "|" || msg_hash
    """
    priv = _node_id_priv
    if priv is None:
        return {}

    try:
        ts = int(time.time())
        nonce = os.urandom(16).hex()
        msg_hash = _msg_hash(msg)

        parts = [
            _DOMAIN,
            _node_id_pub_hex.encode("ascii"),
            b"|",
            str(ts).encode("ascii"),
            b"|",
            nonce.encode("ascii"),
            b"|",
            msg_hash.encode("ascii"),
        ]
        to_sign = b"".join(parts)
        sig_hex = _sign(to_sign)

        return {
            "pub": _node_id_pub_hex,
            "ts": ts,
            "nonce": nonce,
            "msg_hash": msg_hash,
            "sig": sig_hex,
        }
    except Exception as e:
        log.warning("build_auth falhou: %s", e)
        return {}


# ============================================================
# VERIFY AUTH
# ============================================================
def verify_auth(auth: dict, msg=None) -> tuple[bool, str]:
    """
    Verifica um bloco `_auth`.

    Ordem (importante):
      1. Estrutura + limites de tamanho
      2. Timestamp na janela
      3. Binding ao payload (msg_hash)
      4. Assinatura Ed25519  ← ANTES de consumir nonce
      5. Anti-replay (nonce) ← consome só se tudo acima passou

    Retorna (True, "") em sucesso; (False, "motivo") em falha.
    """
    # 1) Estrutura
    if not auth or not isinstance(auth, dict):
        return False, "auth vazio ou invalido"

    pub       = auth.get("pub", "")
    sig       = auth.get("sig", "")
    nonce     = auth.get("nonce", "")
    msg_hash  = auth.get("msg_hash", "")
    ts_raw    = auth.get("ts", 0)

    if not isinstance(pub, str) or not isinstance(sig, str):
        return False, "pub/sig devem ser strings"

    if len(pub) > MAX_PUB_LEN:
        return False, f"pub grande demais ({len(pub)} > {MAX_PUB_LEN})"
    if len(sig) > MAX_SIG_LEN:
        return False, f"sig grande demais ({len(sig)} > {MAX_SIG_LEN})"
    if len(nonce) > MAX_NONCE_LEN:
        return False, f"nonce grande demais ({len(nonce)} > {MAX_NONCE_LEN})"
    if len(msg_hash) > MAX_MSG_HASH_LEN:
        return False, "msg_hash grande demais"

    if not pub or not sig:
        return False, "auth incompleto"

    try:
        ts = int(ts_raw)
    except (ValueError, TypeError):
        return False, "ts invalido"

    # Anti-DoS: pubkey bloqueada?
    if _is_blocked(pub):
        return False, "pubkey temporariamente bloqueada"

    # 2) Janela de tempo
    now = int(time.time())
    delta = abs(now - ts)
    window = _CONFIG.get("window", 30)
    if delta > window:
        return False, f"ts fora da janela (delta={delta}s > {window}s)"

    # 3) Binding ao payload
    if msg is not None:
        try:
            expected_hash = _msg_hash(msg)
        except Exception as e:
            return False, f"msg invalida: {e}"
        if not msg_hash:
            _record_failure(pub)
            return False, "msg_hash ausente no auth"
        if not hmac.compare_digest(msg_hash, expected_hash):
            _record_failure(pub)
            return False, "msg_hash nao corresponde ao payload"

    # 4) Assinatura (ANTES de consumir nonce)
    parts = [
        _DOMAIN,
        pub.encode("ascii", "ignore"),
        b"|",
        str(ts).encode("ascii"),
        b"|",
        (nonce or "").encode("ascii", "ignore"),
        b"|",
        (msg_hash or "").encode("ascii", "ignore"),
    ]
    to_verify = b"".join(parts)

    if not _verify_sig(pub, sig, to_verify):
        _record_failure(pub)
        return False, "assinatura invalida"

    # Passou tudo — limpa tracker de falhas desta pubkey
    _clear_failures(pub)

    # 5) Anti-replay (consome nonce)
    if not nonce:
        return False, "nonce ausente"
    if _nonce_seen(nonce, ts):
        return False, "replay detectado (nonce repetido)"
    _nonce_mark(nonce, ts)

    return True, ""


# ============================================================
# DEBUG / STATUS
# ============================================================
def status() -> dict:
    """Info útil para diagnóstico."""
    with _nonce_lock:
        n_nonces = len(_nonce_cache)
    with _fail_lock:
        n_blocked = sum(
            1 for e in _fail_tracker.values()
            if e.get("blocked_until", 0) > time.time()
        )
        n_tracked = len(_fail_tracker)
    with _node_lock:
        has_priv = _node_id_priv is not None
        pub_short = _node_id_pub_hex[:16] + "..." if _node_id_pub_hex else ""

    return {
        "mode": _CONFIG.get("mode"),
        "enabled": auth_enabled(),
        "required": auth_required(),
        "window_s": _CONFIG.get("window"),
        "nonce_cache_size": n_nonces,
        "nonce_cache_max": _CONFIG.get("cache_size"),
        "fail_threshold": _CONFIG.get("fail_threshold"),
        "fail_block_s": _CONFIG.get("fail_block_s"),
        "fail_pubkeys_tracked": n_tracked,
        "fail_pubkeys_blocked": n_blocked,
        "has_priv": has_priv,
        "node_pub": pub_short,
        "crypto_source": _CRYPTO_SOURCE,
    }


def info() -> dict:
    """Config resolvida (sem estado mutável)."""
    return {
        "domain": _DOMAIN.decode("ascii"),
        "mode": _CONFIG.get("mode"),
        "window_s": _CONFIG.get("window"),
        "nonce_cache_max": _CONFIG.get("cache_size"),
        "fail_threshold": _CONFIG.get("fail_threshold"),
        "fail_block_s": _CONFIG.get("fail_block_s"),
        "max_pub_len": MAX_PUB_LEN,
        "max_sig_len": MAX_SIG_LEN,
        "max_nonce_len": MAX_NONCE_LEN,
        "max_msg_hash_len": MAX_MSG_HASH_LEN,
        "max_payload_bytes": MAX_PAYLOAD_BYTES,
        "crypto_source": _CRYPTO_SOURCE,
    }


def reset_nonce_cache() -> None:
    """Limpa o cache de nonces (útil para testes)."""
    with _nonce_lock:
        _nonce_cache.clear()


def reset_fail_tracker() -> None:
    """Limpa o tracker de falhas (útil para testes)."""
    with _fail_lock:
        _fail_tracker.clear()


# ============================================================
# CLI
# ============================================================
def _cmd_status() -> int:
    print(json.dumps(status(), indent=2, ensure_ascii=False))
    return 0


def _cmd_info() -> int:
    print(json.dumps(info(), indent=2, ensure_ascii=False))
    return 0


def _cmd_test() -> int:
    """Testa assinatura + verificação em loopback."""
    if _Ed25519PublicKey is None:
        print(f"Ed25519 indisponível (fonte: {_CRYPTO_SOURCE})", file=sys.stderr)
        return 1

    try:
        from crypto import Ed25519PrivateKey
    except Exception:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
        except Exception as e:
            print(f"Ed25519PrivateKey indisponível: {e}", file=sys.stderr)
            return 1

    priv = Ed25519PrivateKey.generate()
    set_node_id_priv(priv)
    reset_nonce_cache()

    payload = {"type": "test", "value": 42, "nested": {"a": 1, "b": 2}}

    a = build_auth(msg=payload)
    if not a:
        print("build_auth falhou", file=sys.stderr)
        return 1
    print("build_auth OK:")
    print(f"  pub   = {a['pub'][:16]}...")
    print(f"  sig   = {a['sig'][:16]}...")
    print(f"  nonce = {a['nonce'][:16]}...")
    print(f"  hash  = {a['msg_hash'][:16]}...")

    ok, err = verify_auth(a, msg=payload)
    print(f"\nverify_auth (payload correto): ok={ok} err={err!r}")
    if not ok:
        return 1

    ok2, err2 = verify_auth(a, msg=payload)
    print(f"verify_auth (replay):          ok={ok2} err={err2!r}")
    if ok2:
        print("ERRO: replay foi aceito!", file=sys.stderr)
        return 1

    a3 = build_auth(msg=payload)
    payload_mod = dict(payload)
    payload_mod["value"] = 99
    ok3, err3 = verify_auth(a3, msg=payload_mod)
    print(f"verify_auth (payload trocado):  ok={ok3} err={err3!r}")
    if ok3:
        print("ERRO: payload trocado foi aceito!", file=sys.stderr)
        return 1

    print("\nTudo OK.")
    return 0


def _cmd_self_sign() -> int:
    """Assina um payload lido de stdin (JSON) e imprime o _auth."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        print(f"stdin invalido: {e}", file=sys.stderr)
        return 1

    if _node_id_priv is None:
        print("Chave privada não registrada. Use via código (main.py).",
              file=sys.stderr)
        return 1

    a = build_auth(msg=payload)
    print(json.dumps(a, indent=2, ensure_ascii=False))
    return 0


def _cli() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    p = argparse.ArgumentParser(prog="p2p_auth.py")
    p.add_argument("--status", action="store_true",
                   help="estado em runtime")
    p.add_argument("--info", action="store_true",
                   help="config resolvida")
    p.add_argument("--test", action="store_true",
                   help="testa sign+verify em loopback")
    p.add_argument("--self-sign", action="store_true",
                   help="assina payload lido de stdin (JSON)")
    args = p.parse_args()

    if args.status:
        return _cmd_status()
    if args.info:
        return _cmd_info()
    if args.test:
        return _cmd_test()
    if args.self_sign:
        return _cmd_self_sign()

    p.print_help()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_cli())
    except KeyboardInterrupt:
        sys.exit(130)
