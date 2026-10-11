"""
network_guard.py — Bloqueia mineracao se nao houver outro no na rede (v2)
=========================================================================
Garante que nao exista cadeia paralela: 1 no sozinho nunca minera.

v2:
  [FIX] peers via cadeia de fallback:
          p2p.listar_peers()
          p2p.discovery.listar_peers()
          p2p.get_peers()
          p2p.peers (dict)
          p2p.get_status()['peer_count']
        — antes só tentava p2p.discovery.listar_peers() e silenciava erros
  [FIX] _first_peer_ts protegido por lock (race condition)
  [FIX] reset() atômico
  [FIX] p2p=None tratado (não explode)
  [FIX] se nenhuma API de listagem existir → loga ERROR (não silencia)
  [NEW] MIN_PEER_HEIGHT: peer precisa ter altura >= X (default 0 = não exige)
  [NEW] status() público para diagnóstico / UI
  [NEW] force_allow() / force_deny() / clear_force() para testes e emergência
  [NEW] info() com config resolvida
  [NEW] CLI: --info / --status

Env:
  BRN_ALLOW_SOLO_MINING  (0/1)  — modo dev (default 0)
  BRN_MIN_PEER_STABLE    int    — segundos que peer precisa estar visível
                                  (default 10)
  BRN_MIN_PEER_HEIGHT    int    — altura mínima do peer (default 0)

Retorno de pode_minerar():
    (bool, str)  — (pode, motivo)
=========================================================================
"""
from __future__ import annotations

import os
import time
import threading
from typing import Any, Optional, Tuple

# ============================================================
# CONFIG
# ============================================================
ALLOW_SOLO       = os.environ.get("BRN_ALLOW_SOLO_MINING", "0") == "1"
MIN_PEER_STABLE  = int(os.environ.get("BRN_MIN_PEER_STABLE", "10"))
MIN_PEER_HEIGHT  = int(os.environ.get("BRN_MIN_PEER_HEIGHT", "0"))

# ============================================================
# ESTADO (protegido por lock)
# ============================================================
_lock = threading.Lock()
_first_peer_ts: Optional[float] = None
_force: Optional[bool] = None
_api_warned = False


# ============================================================
# LOG
# ============================================================
def _log(msg: str, level: str = "info") -> None:
    try:
        from brn_logger import get_logger
        lg = get_logger("netguard")
        getattr(lg, level, lg.info)("[NetGuard] " + msg)
    except Exception:
        print(f"[NetGuard] {msg}", flush=True)


# ============================================================
# DESCOBERTA DE PEERS (fallback chain)
# ============================================================
def _extract_peer_count(raw: Any) -> int:
    """Normaliza o retorno de qualquer API para int (contagem de peers)."""
    if raw is None:
        return 0
    if isinstance(raw, dict):
        return len(raw)
    if isinstance(raw, (list, tuple, set)):
        return len(raw)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _count_peers_via_api(p2p) -> Tuple[int, str]:
    """
    Tenta listar peers via várias APIs conhecidas.
    Retorna (count, api_used) — api_used="" se nenhuma funcionou.
    """
    if p2p is None:
        return 0, "p2p=None"

    # 1) p2p.listar_peers()  (PT-BR)
    fn = getattr(p2p, "listar_peers", None)
    if callable(fn):
        try:
            return _extract_peer_count(fn()), "p2p.listar_peers"
        except Exception as e:
            _log(f"p2p.listar_peers() falhou: {e}", "debug")

    # 2) p2p.discovery.listar_peers()  (formato antigo)
    disc = getattr(p2p, "discovery", None)
    if disc is not None:
        fn = getattr(disc, "listar_peers", None)
        if callable(fn):
            try:
                return _extract_peer_count(fn()), "p2p.discovery.listar_peers"
            except Exception as e:
                _log(f"p2p.discovery.listar_peers() falhou: {e}", "debug")

    # 3) p2p.get_peers()  (EN)
    fn = getattr(p2p, "get_peers", None)
    if callable(fn):
        try:
            return _extract_peer_count(fn()), "p2p.get_peers"
        except Exception as e:
            _log(f"p2p.get_peers() falhou: {e}", "debug")

    # 4) p2p.peers (atributo direto — dict ou list)
    peers_attr = getattr(p2p, "peers", None)
    if peers_attr is not None:
        return _extract_peer_count(peers_attr), "p2p.peers"

    # 5) get_status() com 'peer_count'
    fn = getattr(p2p, "get_status", None)
    if callable(fn):
        try:
            st = fn()
            if isinstance(st, dict):
                n = st.get("peer_count") or st.get("peers")
                return _extract_peer_count(n), "p2p.get_status"
        except Exception:
            pass

    return 0, ""


def _peer_height_max(p2p) -> int:
    """
    Tenta descobrir a altura máxima entre os peers.
    Retorna 0 se não conseguir.
    """
    if p2p is None:
        return 0

    for attr in ("peers", "peer_status"):
        peers_attr = getattr(p2p, attr, None)
        if isinstance(peers_attr, dict):
            heights = []
            for v in peers_attr.values():
                if isinstance(v, dict):
                    h = v.get("height")
                    if isinstance(h, int):
                        heights.append(h)
                elif hasattr(v, "height"):
                    try:
                        heights.append(int(v.height))
                    except Exception:
                        pass
            if heights:
                return max(heights)

    fn = getattr(p2p, "get_status", None)
    if callable(fn):
        try:
            st = fn()
            if isinstance(st, dict):
                h = st.get("max_peer_height") or st.get("peer_height_max")
                if isinstance(h, int):
                    return h
        except Exception:
            pass

    return 0


# ============================================================
# API PÚBLICA
# ============================================================
def reset() -> None:
    """Chamar quando p2p inicia/para. Idempotente e thread-safe."""
    global _first_peer_ts, _api_warned
    with _lock:
        _first_peer_ts = None


def force_allow() -> None:
    """Emergência/teste: libera mineração incondicionalmente."""
    global _force
    with _lock:
        _force = True
    _log("force_allow() — mineração liberada por override", "warning")


def force_deny() -> None:
    """Emergência/teste: bloqueia mineração."""
    global _force
    with _lock:
        _force = False
    _log("force_deny() — mineração bloqueada por override", "warning")


def clear_force() -> None:
    """Remove override (volta ao comportamento normal)."""
    global _force
    with _lock:
        _force = None


def pode_minerar(p2p) -> Tuple[bool, str]:
    """
    Retorna (pode, motivo).

    Regras:
      - override (_force) tem prioridade máxima
      - ALLOW_SOLO=1 → sempre pode (modo dev)
      - Sem peers → não pode
      - Peer precisa estar visível por MIN_PEER_STABLE segundos
      - Se MIN_PEER_HEIGHT > 0, peer precisa ter essa altura
    """
    global _first_peer_ts, _api_warned

    # 1) Override
    with _lock:
        override = _force
    if override is True:
        return True, "override (force_allow)"
    if override is False:
        return False, "override (force_deny)"

    # 2) Modo dev
    if ALLOW_SOLO:
        return True, "BRN_ALLOW_SOLO_MINING=1 (dev mode)"

    # 3) Conta peers
    n, api_used = _count_peers_via_api(p2p)

    if not api_used:
        with _lock:
            warned = _api_warned
            _api_warned = True
        if not warned:
            _log(
                "Nenhuma API de listagem de peers encontrada em p2p. "
                "Mineração ficará BLOQUEADA até corrigir. "
                "Esperado: p2p.listar_peers() / p2p.discovery.listar_peers() "
                "/ p2p.get_peers() / p2p.peers / p2p.get_status()",
                "error",
            )
        return False, "sem API de listagem de peers em p2p"

    # 4) Sem peers → bloqueia
    if n == 0:
        with _lock:
            _first_peer_ts = None
        return False, "sem peers — aguardando outro nó entrar na rede"

    # 5) Checa altura mínima
    if MIN_PEER_HEIGHT > 0:
        h = _peer_height_max(p2p)
        if h < MIN_PEER_HEIGHT:
            return False, (
                f"peer com altura baixa ({h} < {MIN_PEER_HEIGHT})"
            )

    # 6) Estabilização
    with _lock:
        if _first_peer_ts is None:
            _first_peer_ts = time.time()
            return False, (
                f"peer detectado, aguardando estabilizar "
                f"({MIN_PEER_STABLE}s)"
            )
        elapsed = time.time() - _first_peer_ts

    if elapsed < MIN_PEER_STABLE:
        falta = int(MIN_PEER_STABLE - elapsed) + 1
        return False, (
            f"estabilizando peer ({int(elapsed)}/{MIN_PEER_STABLE}s, "
            f"faltam {falta}s)"
        )

    return True, f"{n} peer(s) estavel(is) via {api_used}"


# ============================================================
# DIAGNÓSTICO
# ============================================================
def status(p2p=None) -> dict:
    """
    Snapshot do estado atual. Útil para UI / health check.
    Se p2p for passado, conta peers na hora.
    """
    with _lock:
        first_ts = _first_peer_ts
        force = _force
        warned = _api_warned

    n, api_used = _count_peers_via_api(p2p) if p2p is not None else (0, "")

    elapsed = int(time.time() - first_ts) if first_ts else 0
    wait_remaining = 0
    if first_ts and elapsed < MIN_PEER_STABLE:
        wait_remaining = MIN_PEER_STABLE - elapsed

    return {
        "allow_solo": ALLOW_SOLO,
        "min_peer_stable": MIN_PEER_STABLE,
        "min_peer_height": MIN_PEER_HEIGHT,
        "force_override": force,
        "api_warned": warned,
        "peers_count": n,
        "api_used": api_used or "(nenhuma)",
        "first_peer_ts": first_ts,
        "stable_for_s": elapsed,
        "wait_remaining_s": wait_remaining,
    }


def info() -> dict:
    """Config resolvida (sem consultar p2p)."""
    return {
        "allow_solo": ALLOW_SOLO,
        "min_peer_stable": MIN_PEER_STABLE,
        "min_peer_height": MIN_PEER_HEIGHT,
        "force": _force,
    }


# ============================================================
# CLI (diagnóstico)
# ============================================================
if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--info":
        print(json.dumps(info(), indent=2, ensure_ascii=False))
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        print(json.dumps(status(), indent=2, ensure_ascii=False))
        sys.exit(0)

    print("Uso:")
    print("  python network_guard.py --info     # config")
    print("  python network_guard.py --status   # estado atual")
    sys.exit(0)
