"""
discovery_v2.py — Descoberta de peers BRN na LAN
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
from pathlib import Path

log = logging.getLogger("discovery")

MULTICAST_GROUP = "239.255.42.99"
MULTICAST_PORT  = 50007
MDNS_SERVICE    = "_brn._udp.local."
PEERS_MANUAL_FILE = "peers_manual.json"

NETWORK_SECRET = os.environ.get("BRN_NETWORK_SECRET")
if not NETWORK_SECRET:
    raise RuntimeError("BRN_NETWORK_SECRET é obrigatório")

TOKEN = hmac.new(NETWORK_SECRET.encode(), b"brn-disc-v1", hashlib.sha256).hexdigest()

MULTICAST_TTL       = int(os.environ.get("BRN_MC_TTL", "2"))
ACCEPT_CROSS_SUBNET = os.environ.get("BRN_ACCEPT_CROSS_SUBNET", "0") == "1"
SUBNET_PREFIX       = int(os.environ.get("BRN_SUBNET_PREFIX", "24"))
MSG_MAX_AGE         = 60


def get_all_local_ips() -> list[str]:
    ips: set[str] = set()
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
    if ACCEPT_CROSS_SUBNET:
        return True
    try:
        p = SUBNET_PREFIX // 8
        r = SUBNET_PREFIX % 8
        a, b = ip1.split("."), ip2.split(".")
        if a[:p] != b[:p]:
            return False
        if r == 0:
            return True
        mask = (0xFF << (8 - r)) & 0xFF
        return (int(a[p]) & mask) == (int(b[p]) & mask)
    except Exception:
        return False


def _load_manual_peers() -> set[str]:
    peers: set[str] = set()
    p = Path(__file__).resolve().parent / PEERS_MANUAL_FILE
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(data, list):
                peers.update(str(x).strip() for x in data if str(x).strip())
            elif isinstance(data, dict):
                peers.update(str(x).strip() for x in data.get("peers", []) if str(x).strip())
        except Exception as e:
            log.warning(f"peers_manual.json inválido: {e}")
    for x in os.environ.get("BRN_EXTRA_PEERS", "").split(","):
        x = x.strip()
        if x:
            peers.add(x)
    return peers


class UDPDiscovery(threading.Thread):
    def __init__(self, tcp_port, node_uuid, on_peer, running_flag):
        super().__init__(daemon=True, name="UDP-Discovery")
        self.tcp_port = tcp_port
        self.node_uuid = node_uuid
        self.on_peer = on_peer
        self.running_flag = running_flag
        self.sock: socket.socket | None = None
        self.local_ips = get_all_local_ips()
        self.primary_ip = get_primary_ip()
        self._seen: dict[str, float] = {}
        self._stop_evt = threading.Event()

    def _encode(self, kind: str) -> bytes:
        return json.dumps({
            "t": kind, "port": self.tcp_port,
            "uuid": self.node_uuid, "ts": int(time.time()), "tok": TOKEN,
        }).encode()

    def _decode(self, data: bytes) -> dict | None:
        try:
            m = json.loads(data)
        except (ValueError, TypeError):
            return None
        if not isinstance(m, dict) or m.get("tok") != TOKEN:
            return None
        if abs(time.time() - m.get("ts", 0)) > MSG_MAX_AGE:
            return None
        return m

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

    def _recv_loop(self):
        while self.running_flag() and not self._stop_evt.is_set():
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
                try: self.sock.sendto(self._encode("PONG"), addr)
                except Exception: pass

    def _send_loop(self):
        while self.running_flag() and not self._stop_evt.is_set():
            ping = self._encode("PING")
            for target in [(MULTICAST_GROUP, MULTICAST_PORT),
                           ("255.255.255.255", MULTICAST_PORT)]:
                try: self.sock.sendto(ping, target)
                except Exception: pass
            for ip in self.local_ips:
                try:
                    self.sock.sendto(ping, (ip.rsplit(".", 1)[0] + ".255", MULTICAST_PORT))
                except Exception: pass

            n = len(self._seen)
            interval = 2.0 if n < 2 else (10.0 if n < 5 else 30.0)
            if self._stop_evt.wait(interval):
                break

    def _register(self, peer: str, ip: str, port: int):
        now = time.time()
        fresh = peer not in self._seen or (now - self._seen[peer]) > 30
        self._seen[peer] = now
        if fresh:
            try: self.on_peer(ip, port)
            except Exception as e:
                log.debug(f"on_peer({peer}) falhou: {e}")

    def run(self):
        try:
            self._open()
        except Exception as e:
            log.error(f"[UDP] falha ao abrir socket: {e}")
            return
        log.info(f"[UDP] ativo | IPs={self.local_ips} | ttl={MULTICAST_TTL}")
        threading.Thread(target=self._send_loop, daemon=True, name="UDP-Send").start()
        self._recv_loop()

    def stop(self):
        self._stop_evt.set()
        s, self.sock = self.sock, None
        if s:
            try: s.close()
            except OSError: pass


class MDNSDiscovery(threading.Thread):
    def __init__(self, tcp_port, on_peer, running_flag, node_uuid=""):
        super().__init__(daemon=True, name="mDNS-Discovery")
        self.tcp_port = tcp_port
        self.on_peer = on_peer
        self.running_flag = running_flag
        self.node_uuid = node_uuid
        self.zeroconf = None
        self.info = None
        self._stop_evt = threading.Event()

    def run(self):
        try:
            from zeroconf import ServiceInfo, Zeroconf, ServiceBrowser, ServiceListener
        except ImportError:
            log.warning("[mDNS] zeroconf não instalado — pulando")
            return

        from socket import inet_aton
        ip = get_primary_ip()

        class _Listener(ServiceListener):
            def __init__(self, on_peer, primary_ip):
                self.on_peer = on_peer
                self.primary_ip = primary_ip
            def _handle(self, zc, type_, name):
                info = zc.get_service_info(type_, name)
                if not info: return
                for a in info.parsed_addresses():
                    if a.startswith("127."): continue
                    if not same_subnet(a, self.primary_ip): continue
                    try: self.on_peer(a, info.port)
                    except Exception: pass
            def add_service(self, zc, t, n):    self._handle(zc, t, n)
            def update_service(self, zc, t, n): self._handle(zc, t, n)
            def remove_service(self, zc, t, n): pass

        try:
            self.zeroconf = Zeroconf()
            self.info = ServiceInfo(
                MDNS_SERVICE,
                f"BRN-{socket.gethostname()}.{MDNS_SERVICE}",
                addresses=[inet_aton(ip)],
                port=self.tcp_port,
                properties={"uuid": self.node_uuid.encode()},
                server=f"{socket.gethostname()}.local.",
            )
            self.zeroconf.register_service(self.info)
            ServiceBrowser(self.zeroconf, MDNS_SERVICE, _Listener(self.on_peer, ip))
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

    def stop(self):
        self._stop_evt.set()


class PeerLiveness(threading.Thread):
    def __init__(self, get_peers, ping_peer, remove_peer, running_flag):
        super().__init__(daemon=True, name="PeerLiveness")
        self.get_peers = get_peers
        self.ping_peer = ping_peer
        self.remove_peer = remove_peer
        self.running_flag = running_flag
        self._misses: dict[str, int] = {}
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
                        try: self.remove_peer(p)
                        except Exception: pass
                        self._misses.pop(p, None)

    def stop(self):
        self._stop_evt.set()