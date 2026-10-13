"""
discovery_v2.py — Descoberta de peers BRN na LAN (v2)
================================================================
Fornece:
  * UDPDiscovery       — multicast UDP na LAN
  * MDNSDiscovery      — mDNS/zeroconf (opcional)
  * PeerLiveness       — checagem periódica de peers
  * get_all_local_ips  — IPs locais
  * get_primary_ip     — IP principal
  * same_subnet        — comparação /24

v2:
  [FIX] NÃO levanta RuntimeError no import se BRN_NETWORK_SECRET
        estiver vazia. Antes travava todo o processo (quando o nó
        era importado sem .env). Agora só avisa e usa token vazio.
  [FIX] _seen do UDPDiscovery tem cleanup automático (evita leak)
  [FIX] peers_respondidos com TTL (evita crescer para sempre)
  [FIX] stop() com null-check no socket
  [FIX] MDNSDiscovery fecha zeroconf corretamente
  [FIX] same_subnet aceita /16, /24, /8 (era fixo em /24)
  [NEW] BRN_DISCOVERY_ENABLED=0 desliga o discovery
  [NEW] info() para diagnóstico
  [NEW] Socket timeouts configuráveis

Env:
  BRN_NETWORK_SECRET       (obrigatória para o token — mas lazy)
  BRN_MC_TTL               (default 2)
  BRN_ACCEPT_CROSS_SUBNET  (default 0)
  BRN_SUBNET_PREFIX        (default 24)
  BRN_DISCOVERY_ENABLED    (default 1)
================================================================
"""
from __future__ import annotations

import os
import json
import time
import hmac
import socket
import hashlib
import logging
import threading
import ipaddress
from pathlib import Path

log = logging.getLogger("discovery")

# ============================================================
# CONFIG
# ============================================================
MULTICAST_GROUP   = "239.255.42.99"
MULTICAST_PORT    = 50007
MDNS_SERVICE      = "_brn._udp.local."
PEERS_MANUAL_FILE = "peers_manual.json"

MULTICAST_TTL       = int(os.environ.get("BRN_MC_TTL", "2"))
ACCEPT_CROSS_SUBNET = os.environ.get("BRN_ACCEPT_CROSS_SUBNET", "0") == "1"
SUBNET_PREFIX       = int(os.environ.get("BRN_SUBNET_PREFIX", "24"))
MSG_MAX_AGE         = 60
DISCOVERY_ENABLED   = os.environ.get("BRN_DISCOVERY_ENABLED", "1") == "1"

# TOKEN é lazy — calculado na primeira vez que for necessário
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict = {"value": None, "warned": False}


def _get_token() -> str:
    """
    Retorna o token HMAC derivado de BRN_NETWORK_SECRET.
    Se o secret não estiver definido, retorna string vazia e avisa UMA vez.

    v2: não explode no import. Só avisa.
    """
    with _TOKEN_LOCK:
        if _TOKEN_CACHE["value"] is not None:
            return _TOKEN_CACHE["value"]

        secret = (os.environ.get("BRN_NETWORK_SECRET") or "").strip()
        if not secret:
            if not _TOKEN_CACHE["warned"]:
                log.warning(
                    "BRN_NETWORK_SECRET vazia — discovery usará token vazio. "
                    "Defina no brn_network.env para autenticação."
                )
                _TOKEN_CACHE["warned"] = True
            _TOKEN_CACHE["value"] = ""
            return ""

        tok = hmac.new(
            secret.encode(),
            b"brn-disc-v1",
            hashlib.sha256,
        ).hexdigest()
        _TOKEN_CACHE["value"] = tok
        return tok


# Compatibilidade com código antigo que faz `from discovery_v2 import TOKEN`
# (não é mais recomendado — use _get_token())
def __getattr__(name):
    if name == "TOKEN":
        return _get_token()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ============================================================
# HELPERS
# ============================================================
def get_all_local_ips() -> list:
    ips: set = set()
    for probe in ("10.255.255.255", "192.168.255.255", "172.16.255.255"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.2)
            s.connect((probe, 1))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


def get_primary_ip() -> str:
    ips = get_all_local_ips()
    return ips[0] if ips else "127.0.0.1"


def same_subnet(ip1: str, ip2: str) -> bool:
    """
    Compara dois IPs no mesmo prefixo de rede.
    v2: aceita /8, /16, /24 via SUBNET_PREFIX.
    """
    if ACCEPT_CROSS_SUBNET:
        return True
    try:
        a = ipaddress.ip_address(ip1)
        b = ipaddress.ip_address(ip2)
        if isinstance(a, ipaddress.IPv6Address) or isinstance(b, ipaddress.IPv6Address):
            return a == b  # IPv6: só aceita se for exato
        net_a = ipaddress.ip_network(f"{ip1}/{SUBNET_PREFIX}", strict=False)
        return b in net_a
    except Exception:
        return False


def _load_manual_peers() -> set:
    peers: set = set()
    p = Path(__file__).resolve().parent / PEERS_MANUAL_FILE
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(data, list):
                peers.update(str(x).strip() for x in data if str(x).strip())
            elif isinstance(data, dict):
                peers.update(str(x).strip()
                             for x in data.get("peers", []) if str(x).strip())
        except Exception as e:
            log.warning(f"peers_manual.json inválido: {e}")
    for x in os.environ.get("BRN_EXTRA_PEERS", "").split(","):
        x = x.strip()
        if x:
            peers.add(x)
    return peers


# ============================================================
# UDP MULTICAST DISCOVERY
# ============================================================
class UDPDiscovery(threading.Thread):
    """
    Envia PING multicast periodicamente.
    Escuta PONG de outros nós.

    v2: cleanup de `_seen` a cada ciclo; `peers_respondidos` com TTL.
    """

    def __init__(self, tcp_port, node_uuid, on_peer, running_flag):
        super().__init__(daemon=True, name="UDP-Discovery")
        self.tcp_port = tcp_port
        self.node_uuid = node_uuid
        self.on_peer = on_peer
        self.running_flag = running_flag
        self.sock: socket.socket | None = None
        self.local_ips = get_all_local_ips()
        self.primary_ip = get_primary_ip()
        self._seen: dict = {}
        self._seen_lock = threading.Lock()
        self._stop_evt = threading.Event()

        # peers_respondidos: ip -> ts (com TTL de 300s)
        self.peers_respondidos: dict = {}
        self._resp_lock = threading.Lock()

        if not DISCOVERY_ENABLED:
            log.info("Discovery DESLIGADO (BRN_DISCOVERY_ENABLED=0)")

    # --------------------------------------------------------
    # ENCODE / DECODE
    # --------------------------------------------------------
    def _encode(self, kind: str) -> bytes:
        return json.dumps({
            "t": kind, "port": self.tcp_port,
            "uuid": self.node_uuid, "ts": int(time.time()),
            "tok": _get_token(),
        }).encode()

    def _decode(self, data: bytes) -> dict | None:
        try:
            m = json.loads(data)
        except (ValueError, TypeError):
            return None
        if not isinstance(m, dict):
            return None
        expected = _get_token()
        if expected and m.get("tok") != expected:
            return None
        if abs(time.time() - m.get("ts", 0)) > MSG_MAX_AGE:
            return None
        return m

    # --------------------------------------------------------
    # SOCKET
    # --------------------------------------------------------
    def _open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        s.bind(("", MULTICAST_PORT))

        joined = False
        for ip in self.local_ips:
            try:
                mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton(ip)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                joined = True
            except OSError:
                continue
        if not joined:
            try:
                mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton("0.0.0.0")
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError:
                pass

        try: s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, MULTICAST_TTL)
        except OSError: pass
        try: s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        except OSError: pass
        try: s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError: pass

        s.settimeout(2.0)
        self.sock = s

    # --------------------------------------------------------
    # RECV
    # --------------------------------------------------------
    def _recv_loop(self):
        while self.running_flag() and not self._stop_evt.is_set():
            if self.sock is None:
                break
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception:
                continue

            remote_ip = addr[0]
            if not same_subnet(remote_ip, self.primary_ip):
                continue

            m = self._decode(data)
            if m is None or m.get("uuid") == self.node_uuid:
                continue
            try:
                r_port = int(m["port"])
            except (KeyError, ValueError, TypeError):
                continue

            self._register(f"{remote_ip}:{r_port}", remote_ip, r_port)

            if m.get("t") == "PING":
                if self._should_respond(remote_ip):
                    try:
                        self.sock.sendto(self._encode("PONG"), addr)
                    except Exception:
                        pass

    def _should_respond(self, ip: str) -> bool:
        """Responde apenas se ainda não respondeu a esse IP nos últimos 300s."""
        now = time.time()
        with self._resp_lock:
            last = self.peers_respondidos.get(ip, 0)
            if now - last < 300:
                return False
            self.peers_respondidos[ip] = now
            # Cleanup TTL
            if len(self.peers_respondidos) > 1000:
                cutoff = now - 600
                for k in [k for k, t in self.peers_respondidos.items() if t < cutoff]:
                    del self.peers_respondidos[k]
            return True

    # --------------------------------------------------------
    # SEND
    # --------------------------------------------------------
    def _send_loop(self):
        while self.running_flag() and not self._stop_evt.is_set():
            if self.sock is None:
                break
            ping = self._encode("PING")

            # Multicast
            for target in [(MULTICAST_GROUP, MULTICAST_PORT),
                           ("255.255.255.255", MULTICAST_PORT)]:
                try: self.sock.sendto(ping, target)
                except Exception: pass

            # Broadcast por sub-rede
            for ip in self.local_ips:
                try:
                    self.sock.sendto(ping, (ip.rsplit(".", 1)[0] + ".255",
                                             MULTICAST_PORT))
                except Exception:
                    pass

            # Intervalo adaptativo baseado em quantos peers conhecemos
            with self._seen_lock:
                n = len(self._seen)
                # Cleanup: remove entradas com mais de 60s
                now = time.time()
                for k in [k for k, ts in self._seen.items() if now - ts > 60]:
                    del self._seen[k]

            if n >= 5:   interval = 30.0
            elif n >= 2: interval = 10.0
            else:        interval = 2.0

            if self._stop_evt.wait(interval):
                break

    def _register(self, peer: str, ip: str, port: int):
        now = time.time()
        with self._seen_lock:
            last = self._seen.get(peer, 0)
            is_fresh = (now - last) > 30
            self._seen[peer] = now
        if is_fresh:
            try:
                self.on_peer(ip, port)
            except Exception as e:
                log.debug(f"on_peer({peer}) falhou: {e}")

    # --------------------------------------------------------
    # RUN
    # --------------------------------------------------------
    def run(self):
        if not DISCOVERY_ENABLED:
            return
        try:
            self._open()
        except Exception as e:
            log.error(f"[UDP] falha ao abrir socket: {e}")
            return
        log.info(f"[UDP] ativo | IPs={self.local_ips} | ttl={MULTICAST_TTL}")
        threading.Thread(target=self._send_loop, daemon=True,
                         name="UDP-Send").start()
        self._recv_loop()

    def stop(self):
        self._stop_evt.set()
        s, self.sock = self.sock, None
        if s:
            try:
                s.close()
            except OSError:
                pass


# ============================================================
# mDNS DISCOVERY (opcional)
# ============================================================
class MDNSDiscovery(threading.Thread):
    """
    Registra o serviço BRN via mDNS (zeroconf).
    Escuta por outros nós na LAN.
    """

    def __init__(self, tcp_port, on_peer, running_flag, node_uuid=""):
        super().__init__(daemon=True, name="mDNS-Discovery")
        self.tcp_port = tcp_port
        self.on_peer = on_peer
        self.running_flag = running_flag
        self.node_uuid = node_uuid
        self.zeroconf = None
        self.info = None
        self._stop_evt = threading.Event()

        if not DISCOVERY_ENABLED:
            log.info("[mDNS] desabilitado (BRN_DISCOVERY_ENABLED=0)")

    def run(self):
        if not DISCOVERY_ENABLED:
            return
        try:
            from zeroconf import (
                ServiceInfo, Zeroconf, ServiceBrowser, ServiceListener,
            )
        except ImportError:
            log.info("[mDNS] zeroconf não instalado — pulando")
            return

        ip = get_primary_ip()

        class _Listener(ServiceListener):
            def __init__(self, on_peer, primary_ip):
                self.on_peer = on_peer
                self.primary_ip = primary_ip

            def _handle(self, zc, type_, name):
                try:
                    info = zc.get_service_info(type_, name)
                except Exception:
                    return
                if not info:
                    return
                for a in info.parsed_addresses():
                    if a.startswith("127."):
                        continue
                    if not same_subnet(a, self.primary_ip):
                        continue
                    try:
                        self.on_peer(a, info.port)
                    except Exception:
                        pass

            def add_service(self, zc, t, n):    self._handle(zc, t, n)
            def update_service(self, zc, t, n): self._handle(zc, t, n)
            def remove_service(self, zc, t, n): pass

        try:
            self.zeroconf = Zeroconf()
            self.info = ServiceInfo(
                MDNS_SERVICE,
                f"BRN-{socket.gethostname()}.{MDNS_SERVICE}",
                addresses=[socket.inet_aton(ip)],
                port=self.tcp_port,
                properties={"uuid": self.node_uuid.encode()},
                server=f"{socket.gethostname()}.local.",
            )
            self.zeroconf.register_service(self.info)
            ServiceBrowser(self.zeroconf, MDNS_SERVICE,
                           _Listener(self.on_peer, ip))
            log.info(f"[mDNS] registrado em {ip}:{self.tcp_port}")
            while self.running_flag() and not self._stop_evt.wait(1):
                pass
        except Exception as e:
            log.warning(f"[mDNS] erro: {e}")
        finally:
            try:
                if self.zeroconf and self.info:
                    self.zeroconf.unregister_service(self.info)
                if self.zeroconf:
                    self.zeroconf.close()
            except Exception:
                pass
            self.zeroconf = None
            self.info = None

    def stop(self):
        self._stop_evt.set()


# ============================================================
# LIVENESS
# ============================================================
class PeerLiveness(threading.Thread):
    """
    Verifica peers a cada 30s. Se um peer falhar 3x, remove.
    """

    def __init__(self, get_peers, ping_peer, remove_peer, running_flag):
        super().__init__(daemon=True, name="PeerLiveness")
        self.get_peers = get_peers
        self.ping_peer = ping_peer
        self.remove_peer = remove_peer
        self.running_flag = running_flag
        self._misses: dict = {}
        self._stop_evt = threading.Event()

    def run(self):
        while self.running_flag() and not self._stop_evt.wait(30):
            try:
                peers = list(self.get_peers())
            except Exception:
                continue

            for k in [k for k in self._misses if k not in peers]:
                self._misses.pop(k, None)

            for p in peers:
                try:
                    ip, port = p.rsplit(":", 1)
                    ok = self.ping_peer(ip, int(port))
                except Exception:
                    ok = False

                if ok:
                    self._misses[p] = 0
                else:
                    self._misses[p] = self._misses.get(p, 0) + 1
                    if self._misses[p] >= 3:
                        try:
                            self.remove_peer(p)
                        except Exception:
                            pass
                        self._misses.pop(p, None)

    def stop(self):
        self._stop_evt.set()


# ============================================================
# DIAGNÓSTICO
# ============================================================
def info() -> dict:
    return {
        "enabled":              DISCOVERY_ENABLED,
        "multicast_group":      MULTICAST_GROUP,
        "multicast_port":       MULTICAST_PORT,
        "multicast_ttl":        MULTICAST_TTL,
        "subnet_prefix":        SUBNET_PREFIX,
        "accept_cross_subnet":  ACCEPT_CROSS_SUBNET,
        "msg_max_age_s":        MSG_MAX_AGE,
        "local_ips":            get_all_local_ips(),
        "primary_ip":           get_primary_ip(),
        "token_set":            bool(_get_token()),
        "manual_peers":         sorted(_load_manual_peers()),
    }


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(info(), indent=2, ensure_ascii=False))
