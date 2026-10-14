"""p2p_relay.py — Relay E2E (roteia mensagens via peers intermediários)
================================================================
Permite que dois nós que não conseguem conexão direta (NAT simétrico,
firewall) se comuniquem através de um nó intermediário que oferece
capacidade de relay.

Fluxo:
    1. Cada nó anuncia suas capabilities via "relay_capabilities"
       {"can_relay": true, "bandwidth_tier": "high"}

    2. Nó A quer falar com nó C, mas não consegue
       A → R: "relay_connect_request" {"target": "<C-uuid>"}
       R aceita e cria uma rota, responde {"ok": true, "route_id": "..."}

    3. R encaminha para C:
       R → C: "relay_connect_incoming" {"route_id", "from_uuid"}

    4. C aceita:
       C → R: "relay_connect_accepted" {"route_id"}

    5. A e C trocam dados via "relay_data":
       A → R: {"relay_data", "route_id", "payload"}
       R → C: {"relay_data", "route_id", "payload"}

Hooks do P2PServer (p2p_unified v6.6.3):
    on_relay_caps(ip, peer_id, msg)         → None
    on_relay_request(ip, peer_id, msg)      → dict
    on_relay_incoming(ip, peer_id, msg)     → dict
    on_relay_accepted(ip, peer_id, msg)     → None
    on_relay_data(ip, peer_id, msg)         → dict

API usada por main.py v12.5:
    RelayManager(p2p, node_id_priv)
    .routes                          (dict — usado no status loop)
    .register_capability(peer_id, caps, verified=False)
    .handle_relay_request(ip, peer_id, msg)   → dict
    .handle_incoming_relay(ip, peer_id, msg)  → dict
    .handle_route_accepted(msg)
    .handle_relay_data(ip, peer_id, msg)      → dict
    .status()                        → dict
    .stop()

Env:
    BRN_RELAY_MAX_ROUTES         (default 100)
    BRN_RELAY_ROUTE_TTL          (default 300s)
    BRN_RELAY_MAX_PAYLOAD_BYTES  (default 1 MiB)
    BRN_RELAY_MIN_TIER           (default "low" | "medium" | "high")
================================================================
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from typing import Optional


log = logging.getLogger("relay")


# ============================================================
# CONFIG
# ============================================================
MAX_ROUTES           = int(os.environ.get("BRN_RELAY_MAX_ROUTES", "100"))
ROUTE_TTL_S          = int(os.environ.get("BRN_RELAY_ROUTE_TTL", "300"))
MAX_PAYLOAD_BYTES    = int(os.environ.get("BRN_RELAY_MAX_PAYLOAD_BYTES",
                                          str(1024 * 1024)))
MIN_TIER             = os.environ.get("BRN_RELAY_MIN_TIER", "low").lower()

# Hierarquia de tiers (bandwidth)
TIER_RANK = {
    "unknown": 0,
    "low":     1,
    "medium":  2,
    "high":    3,
}

# Estados de rota
ROUTE_PENDING   = "pending"
ROUTE_ACCEPTED  = "accepted"
ROUTE_EXPIRED   = "expired"
ROUTE_CLOSED    = "closed"


# ============================================================
# HELPERS
# ============================================================
def _gen_route_id() -> str:
    return "rt_" + secrets.token_hex(8)


def _now() -> float:
    return time.time()


def _payload_size(obj) -> int:
    try:
        import json
        return len(json.dumps(obj, separators=(",", ":")).encode("utf-8"))
    except Exception:
        return 0


# ============================================================
# RelayManager
# ============================================================
class RelayManager:
    """
    Gerencia capacidades de relay e rotas entre peers.
    Thread-safe. Persiste em memória.
    """

    def __init__(self, p2p, node_id_priv):
        self.p2p = p2p
        self.priv = node_id_priv

        # node_id -> {"can_relay": bool, "tier": str, "verified": bool,
        #             "last_seen": float, "ip": str, "port": int}
        self._caps: dict = {}

        # route_id -> {"route_id", "origin_peer", "target_peer",
        #              "origin_ip", "origin_port",
        #              "target_ip", "target_port",
        #              "state", "created_at", "last_activity",
        #              "bytes_relayed", "packets_relayed"}
        self._routes: dict = {}

        self._lock = threading.RLock()
        self._stop = threading.Event()

        # Métricas
        self._stats = {
            "capabilities_registered": 0,
            "routes_created":          0,
            "routes_accepted":         0,
            "routes_rejected":         0,
            "routes_expired":          0,
            "packets_relayed":         0,
            "bytes_relayed":           0,
            "packets_dropped":         0,
            "auth_rejected":           0,
        }

        # Thread de limpeza
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="Relay-Cleanup")
        self._cleanup_thread.start()

        log.info("RelayManager iniciado (max_routes=%d ttl=%ds)",
                 MAX_ROUTES, ROUTE_TTL_S)

    # ============================================================
    # PROPRIEDADES públicas (main.py lê .routes no status loop)
    # ============================================================
    @property
    def routes(self) -> dict:
        with self._lock:
            return dict(self._routes)

    @property
    def capabilities(self) -> dict:
        with self._lock:
            return dict(self._caps)

    # ============================================================
    # CAPABILITIES
    # ============================================================
    def register_capability(self, peer_id: str, caps: dict,
                            verified: bool = False) -> None:
        """
        Registra/atualiza capabilities de um peer.
        Chamado quando um peer envia "relay_capabilities".
        """
        if not peer_id:
            return
        if not isinstance(caps, dict):
            return

        can_relay = bool(caps.get("can_relay", False))
        tier = str(caps.get("bandwidth_tier", "unknown")).lower()
        if tier not in TIER_RANK:
            tier = "unknown"

        with self._lock:
            self._caps[peer_id] = {
                "can_relay":      can_relay,
                "tier":           tier,
                "tier_rank":      TIER_RANK[tier],
                "verified":       bool(verified),
                "last_seen":      _now(),
            }
            self._stats["capabilities_registered"] += 1

        log.debug("capability: %s can_relay=%s tier=%s verified=%s",
                  peer_id[:12], can_relay, tier, verified)

    def _peer_can_relay(self, peer_id: str) -> bool:
        with self._lock:
            entry = self._caps.get(peer_id)
        if not entry:
            return False
        if not entry.get("can_relay"):
            return False
        # se MIN_TIER for definido, exige que o tier seja >= esse
        min_rank = TIER_RANK.get(MIN_TIER, 0)
        if entry.get("tier_rank", 0) < min_rank:
            return False
        return True

    # ============================================================
    # ROUTES
    # ============================================================
    def _make_route(self, origin_peer: str, target_peer: str,
                    origin_ip: str = "", origin_port: int = 0) -> dict:
        """Cria entrada de rota em PENDING."""
        route_id = _gen_route_id()
        now = _now()
        route = {
            "route_id":       route_id,
            "origin_peer":    origin_peer,
            "target_peer":    target_peer,
            "origin_ip":      origin_ip,
            "origin_port":    int(origin_port or 0),
            "target_ip":      "",
            "target_port":    0,
            "state":          ROUTE_PENDING,
            "created_at":     now,
            "last_activity":  now,
            "bytes_relayed":  0,
            "packets_relayed": 0,
        }
        return route

    def _prune_routes(self):
        """Remove rotas expiradas ou acima do limite."""
        now = _now()
        with self._lock:
            # expira rotas antigas
            expired = []
            for rid, r in list(self._routes.items()):
                if now - r["last_activity"] > ROUTE_TTL_S:
                    r["state"] = ROUTE_EXPIRED
                    expired.append(rid)
            for rid in expired:
                self._routes.pop(rid, None)
                self._stats["routes_expired"] += 1

            # se ainda acima do limite, remove as mais antigas
            if len(self._routes) > MAX_ROUTES:
                ordered = sorted(
                    self._routes.items(),
                    key=lambda kv: kv[1]["last_activity"]
                )
                excess = len(self._routes) - MAX_ROUTES
                for rid, _ in ordered[:excess]:
                    self._routes.pop(rid, None)

    def _cleanup_loop(self):
        """Thread que limpa rotas periodicamente."""
        while not self._stop.wait(60):
            try:
                self._prune_routes()
            except Exception as e:
                log.debug("cleanup: %s", e)

    # ============================================================
    # HANDLER: relay_connect_request
    # ============================================================
    def handle_relay_request(self, ip: str, peer_id: Optional[str],
                             msg: dict) -> dict:
        """
        Um peer pediu para criarmos uma rota até `target`.
        Aqui decidimos se aceitamos e, se sim, encaminhamos para o alvo.

        Espera msg:
            {"type": "relay_connect_request",
             "target": "<uuid do alvo>",
             "origin_port": <int opcional>}
        """
        if not peer_id:
            self._stats["routes_rejected"] += 1
            return {"ok": False, "error": "sem peer_id"}

        # Verifica se nós temos capacidade de relayar
        # (o próprio RelayManager implica que sim, mas checamos também)
        target = str(msg.get("target", "")).strip()
        if not target:
            self._stats["routes_rejected"] += 1
            return {"ok": False, "error": "target vazio"}

        if target == peer_id:
            self._stats["routes_rejected"] += 1
            return {"ok": False, "error": "target == origin"}

        # Não relayamos para nós mesmos
        my_uuid = getattr(self.p2p, "node_uuid", "") if self.p2p else ""
        if target == my_uuid:
            self._stats["routes_rejected"] += 1
            return {"ok": False, "error": "target == self"}

        self._prune_routes()

        origin_port = int(msg.get("origin_port", 0) or 0)

        with self._lock:
            if len(self._routes) >= MAX_ROUTES:
                self._stats["routes_rejected"] += 1
                return {"ok": False, "error": "relay lotado"}

            route = self._make_route(
                origin_peer=peer_id,
                target_peer=target,
                origin_ip=ip,
                origin_port=origin_port,
            )
            self._routes[route["route_id"]] = route
            self._stats["routes_created"] += 1

        log.info("relay: rota %s — %s → %s (via nós)",
                 route["route_id"][:12], peer_id[:12], target[:12])

        # ---- encaminha para o alvo (incoming) ----
        forwarded = self._forward_to_target(
            route=route,
            message={
                "type":       "relay_connect_incoming",
                "route_id":   route["route_id"],
                "from_uuid":  peer_id,
            },
        )
        if not forwarded:
            # remove rota; alvo não está conectado
            with self._lock:
                self._routes.pop(route["route_id"], None)
            self._stats["routes_rejected"] += 1
            return {"ok": False, "error": "target nao alcancavel"}

        return {"ok": True, "route_id": route["route_id"]}

    # ============================================================
    # HANDLER: relay_connect_incoming
    # ============================================================
    def handle_incoming_relay(self, ip: str, peer_id: Optional[str],
                              msg: dict) -> dict:
        """
        Um relay intermediário nos avisa que alguém quer falar conosco.
        Aqui só aceitamos (a política real pode evoluir).

        Espera msg:
            {"type": "relay_connect_incoming",
             "route_id": "...",
             "from_uuid": "..."}
        """
        route_id = str(msg.get("route_id", "")).strip()
        from_uuid = str(msg.get("from_uuid", "")).strip()
        if not route_id or not from_uuid:
            return {"ok": False, "error": "campos faltando"}

        # Aqui poderíamos checar blocklist, tarifas, etc.
        # Por enquanto, aceitamos sempre.

        log.info("relay: incoming %s de %s",
                 route_id[:12], from_uuid[:12])
        return {"ok": True, "accepted": True}

    # ============================================================
    # HANDLER: relay_connect_accepted
    # ============================================================
    def handle_route_accepted(self, msg: dict) -> bool:
        """
        O target confirmou que aceitou a rota.
        Espera msg: {"route_id": "..."}.
        """
        route_id = str(msg.get("route_id", "")).strip()
        if not route_id:
            return False

        with self._lock:
            route = self._routes.get(route_id)
            if not route:
                return False
            route["state"] = ROUTE_ACCEPTED
            route["last_activity"] = _now()
            self._stats["routes_accepted"] += 1

        log.info("relay: rota %s ACEITA (%s → %s)",
                 route_id[:12],
                 route.get("origin_peer", "")[:12],
                 route.get("target_peer", "")[:12])
        return True

    # ============================================================
    # HANDLER: relay_data
    # ============================================================
    def handle_relay_data(self, ip: str, peer_id: Optional[str],
                          msg: dict) -> dict:
        """
        Recebe dados que devem ser repassados pelo túnel.
        Espera msg:
            {"type": "relay_data",
             "route_id": "...",
             "payload": <qualquer>,
             "origin": "origin" | "target"}   # quem enviou
        """
        route_id = str(msg.get("route_id", "")).strip()
        if not route_id:
            self._stats["packets_dropped"] += 1
            return {"ok": False, "error": "sem route_id"}

        payload = msg.get("payload")
        origin = str(msg.get("origin", "")).strip().lower()

        size = _payload_size(payload)
        if size > MAX_PAYLOAD_BYTES:
            self._stats["packets_dropped"] += 1
            return {"ok": False, "error": f"payload > {MAX_PAYLOAD_BYTES}"}

        with self._lock:
            route = self._routes.get(route_id)
            if not route:
                self._stats["packets_dropped"] += 1
                return {"ok": False, "error": "rota inexistente"}

            if route["state"] != ROUTE_ACCEPTED:
                self._stats["packets_dropped"] += 1
                return {"ok": False, "error": f"rota em {route['state']}"}

            # descobre destino
            if origin == "target":
                dest_uuid = route["origin_peer"]
            else:
                dest_uuid = route["target_peer"]

            route["last_activity"] = _now()
            route["bytes_relayed"] += size
            route["packets_relayed"] += 1

        # encaminha para o peer destino
        forwarded = self._send_to_peer(dest_uuid, {
            "type":       "relay_data",
            "route_id":   route_id,
            "payload":    payload,
            "origin":     "relay",
        })

        if forwarded:
            self._stats["packets_relayed"] += 1
            self._stats["bytes_relayed"] += size
            return {"ok": True}
        else:
            self._stats["packets_dropped"] += 1
            return {"ok": False, "error": "peer destino offline"}

    # ============================================================
    # FORWARDING
    # ============================================================
    def _forward_to_target(self, route: dict, message: dict) -> bool:
        """Envia mensagem para o peer de destino da rota."""
        target_uuid = route.get("target_peer", "")
        if not target_uuid:
            return False
        return self._send_to_peer(target_uuid, message)

    def _send_to_peer(self, peer_uuid: str, message: dict) -> bool:
        """
        Envia mensagem a um peer (por uuid). Usa o P2PManager.
        Retorna True se conseguiu despachar.
        """
        if not self.p2p or not peer_uuid:
            return False

        # Estratégia 1: se o P2PManager tem send_to_peer, usa
        if hasattr(self.p2p, "send_to_peer"):
            try:
                return bool(self.p2p.send_to_peer(peer_uuid, message))
            except Exception as e:
                log.debug("send_to_peer: %s", e)

        # Estratégia 2: procura a conexão pelo peer_hex
        try:
            peers = getattr(self.p2p, "_peers", {}) or {}
            with getattr(self.p2p, "_peers_lock", threading.RLock()):
                for conn in peers.values():
                    try:
                        if getattr(conn, "peer_hex", "").startswith(peer_uuid[:32]):
                            conn.send_obj(message)
                            return True
                    except Exception:
                        continue
        except Exception as e:
            log.debug("send_to_peer fallback: %s", e)

        return False

    # ============================================================
    # STATUS / STOP
    # ============================================================
    def status(self) -> dict:
        with self._lock:
            n_routes = len(self._routes)
            n_pending = sum(1 for r in self._routes.values()
                            if r["state"] == ROUTE_PENDING)
            n_accepted = sum(1 for r in self._routes.values()
                             if r["state"] == ROUTE_ACCEPTED)
            n_caps = len(self._caps)
            n_relayers = sum(1 for c in self._caps.values()
                             if c.get("can_relay"))

        return {
            "routes":            n_routes,
            "routes_pending":    n_pending,
            "routes_accepted":   n_accepted,
            "capabilities":      n_caps,
            "peers_can_relay":   n_relayers,
            "max_routes":        MAX_ROUTES,
            "route_ttl_s":       ROUTE_TTL_S,
            "min_tier":          MIN_TIER,
            **self._stats,
        }

    def list_routes(self) -> list:
        with self._lock:
            return [
                {
                    "route_id":      r["route_id"],
                    "origin_peer":   r["origin_peer"],
                    "target_peer":   r["target_peer"],
                    "state":         r["state"],
                    "created_at":    r["created_at"],
                    "last_activity": r["last_activity"],
                    "packets":       r["packets_relayed"],
                    "bytes":         r["bytes_relayed"],
                }
                for r in self._routes.values()
            ]

    def stop(self):
        self._stop.set()
        if self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=2)
        with self._lock:
            self._routes.clear()
            self._caps.clear()
        log.info("RelayManager encerrado")


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("Testando RelayManager...")

    # ---- mock P2PManager ----
    class FakeP2P:
        def __init__(self):
            self.node_uuid = "aaaa"
            self._peers = {}
            self._peers_lock = threading.RLock()
            self.sent = []

        def send_to_peer(self, uuid, msg):
            self.sent.append((uuid, msg))
            return True

    class FakePriv:
        def sign(self, data):
            return b"fakesig"

    p2p = FakeP2P()
    relay = RelayManager(p2p, FakePriv())

    # ---- 1. Capability ----
    relay.register_capability("peer_alpha", {
        "can_relay": True, "bandwidth_tier": "high"
    }, verified=True)
    caps = relay.capabilities
    assert "peer_alpha" in caps
    assert caps["peer_alpha"]["can_relay"]
    assert caps["peer_alpha"]["verified"] is True
    print(f"  [ok] register_capability")

    # ---- 2. Peer sem can_relay ----
    relay.register_capability("peer_beta", {"can_relay": False})
    assert not relay._peer_can_relay("peer_beta")
    assert relay._peer_can_relay("peer_alpha")
    print("  [ok] _peer_can_relay distingue")

    # ---- 3. Relação request → accept → data ----
    r = relay.handle_relay_request(
        "10.0.0.1", "peer_origin",
        {"target": "peer_target", "origin_port": 6001},
    )
    assert r["ok"], r
    route_id = r["route_id"]
    print(f"  [ok] handle_relay_request → {route_id[:12]}")

    # aceita a rota (simula resposta do target)
    ok = relay.handle_route_accepted({"route_id": route_id})
    assert ok
    route = relay.routes[route_id]
    assert route["state"] == "accepted"
    print("  [ok] handle_route_accepted")

    # incoming (do lado do target) — aceita
    r2 = relay.handle_incoming_relay(
        "10.0.0.2", "peer_target",
        {"route_id": route_id, "from_uuid": "peer_origin"},
    )
    assert r2["ok"]
    print("  [ok] handle_incoming_relay")

    # ---- 4. Envia dados nos dois sentidos ----
    r3 = relay.handle_relay_data(
        "10.0.0.1", "peer_origin",
        {"route_id": route_id, "payload": {"hello": "target"}, "origin": "origin"},
    )
    assert r3["ok"], r3
    r4 = relay.handle_relay_data(
        "10.0.0.2", "peer_target",
        {"route_id": route_id, "payload": {"hello": "origin"}, "origin": "target"},
    )
    assert r4["ok"], r4
    print("  [ok] handle_relay_data (idempotente ambos sentidos)")

    # ---- 5. Status ----
    st = relay.status()
    print(f"  [info] status: {json.dumps(st, ensure_ascii=False)}")
    assert st["routes"] == 1
    assert st["routes_accepted"] == 1
    assert st["packets_relayed"] == 2

    # ---- 6. Payload grande ----
    r5 = relay.handle_relay_data(
        "10.0.0.1", "peer_origin",
        {"route_id": route_id, "payload": "x" * (MAX_PAYLOAD_BYTES + 1),
         "origin": "origin"},
    )
    assert not r5["ok"]
    print(f"  [ok] payload grande rejeitado ({r5['error']})")

    # ---- 7. Rota invalida ----
    r6 = relay.handle_relay_data(
        "10.0.0.1", "peer_origin",
        {"route_id": "rt_inexistente", "payload": "x", "origin": "origin"},
    )
    assert not r6["ok"]
    print("  [ok] rota inexistente rejeitada")

    # ---- 8. Auto-relay rejeitado ----
    r7 = relay.handle_relay_request(
        "10.0.0.1", "peer_x",
        {"target": "peer_x"},
    )
    assert not r7["ok"]
    print("  [ok] auto-relay rejeitado")

    # ---- 9. Limpeza ----
    relay.stop()
    print("  [ok] stop()")

    print("RelayManager OK.")