"""p2p_relay.py — Relay E2E (roteia mensagens via peers intermediários)."""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("relay")


class RelayManager:
    """
    Gerencia capacidades de relay e rotas.
    Compatível com hooks chamados pelo main.py v12.5.
    """

    def __init__(self, p2p, node_id_priv):
        self.p2p = p2p
        self.priv = node_id_priv
        self.routes = {}          # route_id -> dict
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._stats = {"relayed": 0, "rejected": 0, "accepted": 0}

    # ============================================================
    # Capacidades — quem pode relayar
    # ============================================================
    def register_capability(self, peer_id: str, caps: dict,
                            verified: bool = False):
        with self._lock:
            entry = self.routes.setdefault(peer_id, {})
            entry.update({
                "can_relay": bool(caps.get("can_relay")),
                "bandwidth_tier": caps.get("bandwidth_tier", "unknown"),
                "verified": verified,
                "last_seen": time.time(),
            })

    # ============================================================
    # Rotas
    # ============================================================
    def request_route(self, target_peer: str, via_peer: str) -> dict:
        """Solicita rota até target_peer via via_peer."""
        with self._lock:
            info = self.routes.get(via_peer) or {}
        if not info.get("can_relay"):
            return {"ok": False, "msg": "peer não pode relayar"}
        route_id = f"r_{int(time.time()*1000)}"
        with self._lock:
            self.routes[route_id] = {
                "target": target_peer, "via": via_peer,
                "created_at": time.time(), "state": "pending",
            }
        return {"ok": True, "route_id": route_id}

    def handle_relay_request(self, ip, peer_id, msg):
        """Hook chamado quando um peer pede relay."""
        try:
            target = msg.get("target")
            if not target:
                return
            self._stats["accepted"] += 1
            # resposta simples: aceita
            return {"ok": True, "target": target}
        except Exception as e:
            log.debug(f"relay request falhou: {e}")
            return {"ok": False, "msg": str(e)}

    def handle_incoming_relay(self, ip, peer_id, msg):
        """Hook: mensagem chegou endereçada a outro peer."""
        route_id = msg.get("route_id", "")
        with self._lock:
            route = self.routes.get(route_id)
        if route:
            self._stats["relayed"] += 1
            return {"ok": True}
        self._stats["rejected"] += 1
        return {"ok": False, "msg": "rota desconhecida"}

    def handle_route_accepted(self, msg):
        route_id = msg.get("route_id", "")
        with self._lock:
            if route_id in self.routes:
                self.routes[route_id]["state"] = "accepted"
                return True
        return False

    def handle_relay_data(self, ip, peer_id, data):
        with self._lock:
            self._stats["relayed"] += 1
        return True

    # ============================================================
    # Status
    # ============================================================
    def status(self) -> dict:
        with self._lock:
            n_routes = sum(1 for k, v in self.routes.items()
                           if isinstance(v, dict) and "target" in v)
            n_caps = sum(1 for k, v in self.routes.items()
                         if isinstance(v, dict) and "can_relay" in v)
        return {
            "routes": n_routes,
            "capabilities": n_caps,
            **self._stats,
        }

    def stop(self):
        self._stop.set()