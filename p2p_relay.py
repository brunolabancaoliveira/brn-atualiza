"""
p2p_relay.py — Conexão indireta via nós intermediários (v2.0)
E2E: X25519 + ChaCha20-Poly1305. Relay só vê ciphertext.
"""
from __future__ import annotations

import os
import json
import time
import base64
import logging
import secrets
import threading
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional

from p2p_auth import build_auth, verify_auth, auth_enabled

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger("relay")

MAX_RELAY_HOPS             = int(os.environ.get("BRN_RELAY_HOPS", "2"))
MAX_ROUTES                 = int(os.environ.get("BRN_RELAY_MAX_ROUTES", "500"))
MAX_PENDING                = int(os.environ.get("BRN_RELAY_MAX_PENDING", "500"))
MAX_CAPABILITIES           = int(os.environ.get("BRN_RELAY_MAX_CAPS", "1000"))
RELAY_REQ_RATE_LIMIT       = int(os.environ.get("BRN_RELAY_RATE", "20"))
ROUTE_TTL_S                = int(os.environ.get("BRN_RELAY_ROUTE_TTL", "600"))
PENDING_TTL_S              = int(os.environ.get("BRN_RELAY_PENDING_TTL", "120"))
CLEANUP_INTERVAL_S         = 60
RELAY_MSG_MAX_BYTES        = int(os.environ.get("BRN_RELAY_MSG_MAX",
                                                  str(256 * 1024)))
MAX_ACTIVE_ROUTES_PER_PEER = int(os.environ.get("BRN_RELAY_MAX_PER_PEER", "5"))

RELAY_PROTOCOL_VERSION     = 1


def _ed_priv_to_x25519(ed_priv) -> X25519PrivateKey:
    ed_raw = ed_priv.private_bytes_raw()
    h = hashes.Hash(hashes.SHA512())
    h.update(ed_raw)
    digest = h.finalize()
    scalar = bytearray(digest[:32])
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    return X25519PrivateKey.from_private_bytes(bytes(scalar))


def _derive_shared_key(priv: X25519PrivateKey, peer_pub: X25519PublicKey,
                       info: bytes) -> bytes:
    shared = priv.exchange(peer_pub)
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32,
                salt=b"brn-relay-v1", info=info)
    return hkdf.derive(shared)


def _encrypt_e2e(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)
    return nonce + ct


def _decrypt_e2e(key: bytes, packed: bytes, aad: bytes) -> bytes:
    if len(packed) < 12 + 16:
        raise ValueError("ciphertext muito curto")
    nonce, ct = packed[:12], packed[12:]
    return ChaCha20Poly1305(key).decrypt(nonce, ct, aad)


@dataclass
class RelayRoute:
    route_id: str
    source_node: str
    target_node: str
    via_node: str
    shared_key: bytes
    created_at: float = field(default_factory=time.time)
    active: bool = True
    last_used: float = field(default_factory=time.time)
    bytes_in: int = 0
    bytes_out: int = 0


@dataclass
class PendingRequest:
    req_id: str
    requester_node: str
    target_node: str
    via_node: str
    eph_priv: object
    eph_pub_b64: str
    created_at: float = field(default_factory=time.time)
    status: str = "pending"


class PeerDirectory:
    def __init__(self):
        self._by_uuid: dict[str, tuple[str, int, float]] = {}
        self._lock = threading.Lock()

    def set(self, uuid_hex: str, ip: str, port: int) -> None:
        if not uuid_hex or not ip:
            return
        with self._lock:
            self._by_uuid[uuid_hex] = (ip, int(port), time.time())

    def get(self, uuid_hex: str) -> Optional[tuple[str, int]]:
        with self._lock:
            v = self._by_uuid.get(uuid_hex)
            if not v:
                return None
            if time.time() - v[2] > 600:
                return None
            return (v[0], v[1])

    def all(self) -> dict[str, tuple[str, int]]:
        with self._lock:
            now = time.time()
            return {u: (ip, port) for u, (ip, port, ts)
                    in self._by_uuid.items() if now - ts < 600}

    def prune(self, ttl: float = 600) -> int:
        now = time.time()
        with self._lock:
            stale = [u for u, (_, _, ts) in self._by_uuid.items()
                     if now - ts > ttl]
            for u in stale:
                del self._by_uuid[u]
            return len(stale)


class RelayManager:
    def __init__(self, p2p_manager, node_priv=None):
        self.p2p = p2p_manager
        self.node_priv = node_priv
        self.directory = PeerDirectory()

        self.routes: dict[str, RelayRoute] = {}
        self.pending: dict[str, PendingRequest] = {}
        self.peer_capabilities: dict[str, dict] = {}
        self._routes_by_peer: dict[str, set[str]] = defaultdict(set)

        self.lock = threading.Lock()
        self._caps_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._rate: dict[str, list[float]] = defaultdict(list)

        self._stop_evt = threading.Event()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="Relay-Cleanup")
        self._cleanup_thread.start()

        self.metrics = {
            "routes_created": 0, "routes_rejected": 0, "routes_expired": 0,
            "relay_requests_in": 0, "relay_requests_out": 0,
            "bytes_relayed": 0, "rate_limited": 0,
            "caps_announced": 0, "cap_rejects": 0,
        }
        self._metrics_lock = threading.Lock()
        self._on_relay_data_cb = None

    def _inc(self, key: str, n: int = 1) -> None:
        with self._metrics_lock:
            if key in self.metrics:
                self.metrics[key] += n

    def _check_rate(self, peer_uuid: str) -> bool:
        now = time.time()
        with self._rate_lock:
            w = self._rate[peer_uuid]
            w[:] = [t for t in w if now - t < 60]
            if len(w) >= RELAY_REQ_RATE_LIMIT:
                self._inc("rate_limited")
                return False
            w.append(now)
            return True

    @property
    def node_uuid(self) -> str:
        return getattr(self.p2p, "node_uuid", "")

    def _send_to(self, ip: str, port: int, message: dict,
                 timeout: float = 10.0):
        from p2p_unified import P2PClient
        return P2PClient.send_message(ip, port, message, timeout=timeout)

    def _send_to_uuid(self, uuid_hex: str, message: dict,
                      timeout: float = 10.0):
        addr = self.directory.get(uuid_hex)
        if not addr:
            # tenta no discovery (fallback)
            try:
                addr = self.p2p.discovery.get_addr_by_uuid(uuid_hex)
            except Exception:
                addr = None
        if not addr:
            log.debug(f"relay: uuid {uuid_hex[:12]} não mapeado")
            return None, 0.0
        return self._send_to(addr[0], addr[1], message, timeout)

    def announce_capabilities(self, can_relay: bool = True,
                              bandwidth_tier: str = "medium") -> None:
        msg = {
            "type": "relay_capabilities",
            "version": RELAY_PROTOCOL_VERSION,
            "node_uuid": self.node_uuid,
            "can_relay": can_relay,
            "bandwidth_tier": bandwidth_tier,
            "ts": int(time.time()),
        }
        if auth_enabled() and self.node_priv is not None:
            payload_sem = dict(msg)
            msg["_auth"] = build_auth(msg=payload_sem)

        for addr in self.p2p.discovery.listar_peers():
            try:
                ip, port = addr.rsplit(":", 1)
                threading.Thread(
                    target=self._send_to,
                    args=(ip, int(port), msg),
                    daemon=True,
                ).start()
            except Exception:
                continue

    def register_capability(self, node_uuid: str, caps: dict,
                            verified: bool = False) -> None:
        if not node_uuid:
            return
        with self._caps_lock:
            if (len(self.peer_capabilities) >= MAX_CAPABILITIES
                    and node_uuid not in self.peer_capabilities):
                try:
                    oldest = min(self.peer_capabilities.items(),
                                 key=lambda kv: kv[1].get("last_seen", 0))
                    del self.peer_capabilities[oldest[0]]
                except ValueError:
                    pass
                self._inc("cap_rejects")
            self.peer_capabilities[node_uuid] = {
                "can_relay": bool(caps.get("can_relay", False)),
                "bandwidth_tier": str(caps.get("bandwidth_tier", "unknown"))[:16],
                "last_seen": time.time(),
                "verified": verified,
            }
            self._inc("caps_announced")

    def find_relay_node(self, target_node: str,
                        exclude: Optional[set] = None) -> Optional[str]:
        exclude = set(exclude or ())
        exclude.add(target_node)
        exclude.add(self.node_uuid)

        with self._caps_lock:
            candidates = [
                (u, c) for u, c in self.peer_capabilities.items()
                if u not in exclude and c.get("can_relay")
                and c.get("verified", False)
            ]
        for u, _ in candidates:
            if self.directory.get(u):
                return u
        return None

    def create_relay_request(self, target_node: str,
                             via_node: str) -> Optional[str]:
        if not self.node_priv:
            log.warning("relay: node_priv ausente — não posso iniciar E2E")
            return None

        with self._caps_lock:
            caps = self.peer_capabilities.get(via_node)
            if not caps or not caps.get("can_relay") or not caps.get("verified"):
                log.info(f"relay: via {via_node[:12]} não é relayer confiável")
                return None

        eph_priv = X25519PrivateKey.generate()
        eph_pub_b64 = base64.b64encode(
            eph_priv.public_key().public_bytes_raw()
        ).decode()

        req_id = secrets.token_hex(16)

        pending = PendingRequest(
            req_id=req_id,
            requester_node=self.node_uuid,
            target_node=target_node,
            via_node=via_node,
            eph_priv=eph_priv,
            eph_pub_b64=eph_pub_b64,
        )
        with self.lock:
            if len(self.pending) >= MAX_PENDING:
                try:
                    oldest = min(self.pending.items(),
                                 key=lambda kv: kv[1].created_at)
                    del self.pending[oldest[0]]
                except ValueError:
                    pass
            self.pending[req_id] = pending

        payload = {
            "type": "relay_connect_request",
            "req_id": req_id,
            "target_uuid": target_node,
            "requester_uuid": self.node_uuid,
            "eph_pub_b64": eph_pub_b64,
            "ts": int(time.time()),
        }
        if auth_enabled():
            payload_sem = dict(payload)
            payload["_auth"] = build_auth(msg=payload_sem)

        resp, _ = self._send_to_uuid(via_node, payload)
        self._inc("relay_requests_out")

        if not resp or not resp.get("ok"):
            with self.lock:
                self.pending.pop(req_id, None)
            log.info(f"relay: via {via_node[:12]} recusou req {req_id[:12]}")
            return None
        return req_id

    def handle_relay_request(self, from_ip: str, from_uuid: Optional[str],
                             data: dict) -> dict:
        self._inc("relay_requests_in")
        if not from_uuid:
            return {"ok": False, "error": "no_uuid"}
        if not self._check_rate(from_uuid):
            return {"ok": False, "error": "rate_limited"}

        req_id = str(data.get("req_id", ""))
        if not req_id or len(req_id) > 64:
            return {"ok": False, "error": "bad_req_id"}

        target = str(data.get("target_uuid", ""))
        if not target:
            return {"ok": False, "error": "bad_target"}

        if auth_enabled():
            a = data.get("_auth")
            if not a:
                return {"ok": False, "error": "auth_required"}
            payload_sem = {k: v for k, v in data.items() if k != "_auth"}
            ok, err = verify_auth(a, msg=payload_sem)
            if not ok:
                return {"ok": False, "error": "bad_auth"}

        self.directory.set(from_uuid, from_ip, int(data.get("_port", 6001)))

        forward = {
            "type": "relay_connect_incoming",
            "req_id": req_id,
            "requester_uuid": from_uuid,
            "via_uuid": self.node_uuid,
            "eph_pub_b64": data.get("eph_pub_b64", ""),
            "ts": int(time.time()),
        }
        if auth_enabled():
            payload_sem = dict(forward)
            forward["_auth"] = build_auth(msg=payload_sem)

        resp, _ = self._send_to_uuid(target, forward)
        if not resp or not resp.get("ok"):
            return {"ok": False, "error": "target_unreachable"}
        return {"ok": True, "req_id": req_id}

    def handle_incoming_relay(self, from_ip: str, from_uuid: Optional[str],
                              data: dict) -> dict:
        if not from_uuid:
            return {"ok": False, "error": "no_uuid"}
        if not self._check_rate(from_uuid):
            return {"ok": False, "error": "rate_limited"}

        req_id = str(data.get("req_id", ""))
        requester = str(data.get("requester_uuid", ""))
        via_uuid = str(data.get("via_uuid", ""))
        eph_pub_b64 = str(data.get("eph_pub_b64", ""))
        if not (req_id and requester and via_uuid and eph_pub_b64):
            return {"ok": False, "error": "missing_fields"}

        if auth_enabled():
            a = data.get("_auth")
            if not a:
                return {"ok": False, "error": "auth_required"}
            payload_sem = {k: v for k, v in data.items() if k != "_auth"}
            ok, err = verify_auth(a, msg=payload_sem)
            if not ok:
                return {"ok": False, "error": "bad_auth"}

        with self._caps_lock:
            via_caps = self.peer_capabilities.get(via_uuid)
        if not via_caps or not via_caps.get("verified"):
            self._inc("routes_rejected")
            return {"ok": False, "error": "via_not_trusted"}

        with self.lock:
            if len(self._routes_by_peer[requester]) >= MAX_ACTIVE_ROUTES_PER_PEER:
                self._inc("routes_rejected")
                return {"ok": False, "error": "peer_quota_exceeded"}

        if not self.node_priv:
            return {"ok": False, "error": "no_node_priv"}

        try:
            eph_pub = X25519PublicKey.from_public_bytes(
                base64.b64decode(eph_pub_b64))
            my_x = _ed_priv_to_x25519(self.node_priv)
            shared = _derive_shared_key(my_x, eph_pub,
                                        info=b"brn-relay-target")
        except Exception as e:
            log.warning(f"relay: ECDH falhou — {e}")
            return {"ok": False, "error": "ecdh_failed"}

        route_id = secrets.token_hex(8)

        route = RelayRoute(
            route_id=route_id,
            source_node=requester,
            target_node=self.node_uuid,
            via_node=via_uuid,
            shared_key=shared,
        )
        with self.lock:
            if len(self.routes) >= MAX_ROUTES:
                try:
                    oldest = min(self.routes.items(),
                                 key=lambda kv: kv[1].last_used)
                    del self.routes[oldest[0]]
                except ValueError:
                    pass
            self.routes[route_id] = route
            self._routes_by_peer[requester].add(route_id)
        self._inc("routes_created")

        response = {
            "type": "relay_connect_accepted",
            "req_id": req_id,
            "route_id": route_id,
            "target_uuid": self.node_uuid,
            "eph_pub_b64": base64.b64encode(
                my_x.public_key().public_bytes_raw()).decode(),
            "ts": int(time.time()),
        }
        if auth_enabled():
            payload_sem = dict(response)
            response["_auth"] = build_auth(msg=payload_sem)

        self._send_to_uuid(via_uuid, response)
        return {"ok": True, "route_id": route_id, "via_uuid": via_uuid}

    def handle_route_accepted(self, data: dict) -> bool:
        req_id = str(data.get("req_id", ""))
        route_id = str(data.get("route_id", ""))
        target_uuid = str(data.get("target_uuid", ""))
        eph_pub_b64 = str(data.get("eph_pub_b64", ""))

        with self.lock:
            pending = self.pending.get(req_id)
            if not pending:
                return False
            if pending.status != "pending":
                return False
            if target_uuid != pending.target_node:
                return False
            try:
                peer_eph_pub = X25519PublicKey.from_public_bytes(
                    base64.b64decode(eph_pub_b64))
                shared = _derive_shared_key(pending.eph_priv, peer_eph_pub,
                                            info=b"brn-relay-target")
            except Exception as e:
                log.warning(f"relay: ECDH no origen falhou — {e}")
                return False

            route = RelayRoute(
                route_id=route_id,
                source_node=self.node_uuid,
                target_node=pending.target_node,
                via_node=pending.via_node,
                shared_key=shared,
            )
            self.routes[route_id] = route
            self._routes_by_peer[pending.target_node].add(route_id)
            del self.pending[req_id]
            self._inc("routes_created")

        log.info(f"relay: rota {route_id[:12]} com {target_uuid[:12]} "
                 f"via {pending.via_node[:12]}")
        return True

    def relay_message(self, route_id: str, message: bytes) -> bool:
        if len(message) > RELAY_MSG_MAX_BYTES:
            log.warning(f"relay: mensagem grande demais ({len(message)}B)")
            return False

        with self.lock:
            route = self.routes.get(route_id)
            if not route or not route.active:
                return False
            route.last_used = time.time()
            if self.node_uuid == route.source_node:
                dest = route.target_node
            else:
                dest = route.source_node
            via = route.via_node
            shared_key = route.shared_key

        aad = f"{route_id}|{self.node_uuid}|{dest}".encode()
        try:
            ciphertext = _encrypt_e2e(shared_key, message, aad)
        except Exception as e:
            log.warning(f"relay: encrypt falhou — {e}")
            return False

        payload = {
            "type": "relay_data",
            "route_id": route_id,
            "from_uuid": self.node_uuid,
            "to_uuid": dest,
            "ciphertext_b64": base64.b64encode(ciphertext).decode(),
            "ts": int(time.time()),
        }
        if auth_enabled():
            payload_sem = dict(payload)
            payload["_auth"] = build_auth(msg=payload_sem)

        resp, _ = self._send_to_uuid(via, payload)
        with self.lock:
            route = self.routes.get(route_id)
            if route:
                route.bytes_out += len(ciphertext)
        self._inc("bytes_relayed", len(ciphertext))
        return bool(resp and resp.get("ok"))

    def handle_relay_data(self, from_ip: str, from_uuid: Optional[str],
                          data: dict) -> dict:
        req_id = str(data.get("route_id", ""))
        from_node = str(data.get("from_uuid", ""))
        to_node = str(data.get("to_uuid", ""))
        ct_b64 = str(data.get("ciphertext_b64", ""))

        if not (req_id and from_node and to_node and ct_b64):
            return {"ok": False, "error": "bad_fields"}

        if to_node == self.node_uuid:
            with self.lock:
                route = self.routes.get(req_id)
                if not route:
                    return {"ok": False, "error": "unknown_route"}
                shared_key = route.shared_key
                route.last_used = time.time()
                route.bytes_in += len(ct_b64)
            aad = f"{req_id}|{from_node}|{self.node_uuid}".encode()
            try:
                plaintext = _decrypt_e2e(
                    shared_key, base64.b64decode(ct_b64), aad)
            except Exception as e:
                log.warning(f"relay: decrypt falhou — {e}")
                return {"ok": False, "error": "decrypt_failed"}
            self._on_relay_data(from_node, plaintext)
            return {"ok": True, "delivered": True}

        with self.lock:
            route = self.routes.get(req_id)
            if not route:
                return {"ok": False, "error": "unknown_route"}
            if route.via_node == self.node_uuid:
                next_hop = (route.target_node if from_node == route.source_node
                            else route.source_node)
            else:
                next_hop = route.via_node
            route.last_used = time.time()

        resp, _ = self._send_to_uuid(next_hop, data)
        self._inc("bytes_relayed", len(ct_b64))
        return {"ok": bool(resp and resp.get("ok"))}

    def set_on_relay_data(self, cb) -> None:
        self._on_relay_data_cb = cb

    def _on_relay_data(self, from_uuid: str, plaintext: bytes) -> None:
        if self._on_relay_data_cb:
            try:
                self._on_relay_data_cb(from_uuid, plaintext)
            except Exception as e:
                log.warning(f"on_relay_data callback falhou: {e}")

    def get_connection_path(self, target_node: str,
                            max_hops: Optional[int] = None) -> list[str]:
        max_hops = max_hops or MAX_RELAY_HOPS
        with self.lock:
            if any(r.target_node == target_node for r in self.routes.values()):
                return [target_node]
        if max_hops < 1:
            return []
        via = self.find_relay_node(target_node)
        if via:
            return [via, target_node]
        return []

    def get_status(self) -> dict:
        with self.lock:
            active_routes = len(self.routes)
            pending_reqs = len(self.pending)
        with self._caps_lock:
            known_relays = sum(1 for c in self.peer_capabilities.values()
                               if c.get("can_relay"))
            verified_relays = sum(1 for c in self.peer_capabilities.values()
                                   if c.get("can_relay") and c.get("verified"))
        return {
            "node_uuid": self.node_uuid[:16],
            "active_routes": active_routes,
            "pending_requests": pending_reqs,
            "known_relays": known_relays,
            "verified_relays": verified_relays,
            "metrics": dict(self.metrics),
        }

    def _cleanup_loop(self) -> None:
        while not self._stop_evt.wait(CLEANUP_INTERVAL_S):
            now = time.time()
            with self.lock:
                expired = [rid for rid, r in self.routes.items()
                           if now - r.last_used > ROUTE_TTL_S]
                for rid in expired:
                    r = self.routes.pop(rid, None)
                    if r:
                        self._routes_by_peer[r.source_node].discard(rid)
                        self._routes_by_peer[r.target_node].discard(rid)
                    self._inc("routes_expired")
                old_reqs = [rid for rid, r in self.pending.items()
                            if now - r.created_at > PENDING_TTL_S]
                for rid in old_reqs:
                    self.pending.pop(rid, None)

            with self._caps_lock:
                stale_caps = [u for u, c in self.peer_capabilities.items()
                              if now - c.get("last_seen", 0) > 1800]
                for u in stale_caps:
                    del self.peer_capabilities[u]

            self.directory.prune()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=2)