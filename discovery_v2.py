"""discovery_v2.py — Descoberta LAN via UDP multicast."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time

log = logging.getLogger("discovery")

MULTICAST_GROUP = "239.255.42.99"
MULTICAST_PORT = 50007
MSG_MAX_AGE = 60
TTL = int(os.environ.get("BRN_MC_TTL", "2"))

NETWORK_SECRET = os.environ.get("BRN_NETWORK_SECRET", "")
TOKEN = hmac.new(NETWORK_SECRET.encode(),
                 b"brn-disc-v1", hashlib.sha256).hexdigest()


def get_all_local_ips() -> list:
    ips = set()
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


class UDPDiscovery(threading.Thread):
    def __init__(self, tcp_port, node_uuid, on_peer, running_flag):
        super().__init__(daemon=True, name="UDP-Discovery")
        self.tcp_port = tcp_port
        self.node_uuid = node_uuid
        self.on_peer = on_peer
        self.running_flag = running_flag
        self.sock = None
        self.local_ips = get_all_local_ips()
        self.primary_ip = get_primary_ip()
        self._seen = {}
        self._stop = threading.Event()

    def _encode(self, kind):
        return json.dumps({
            "t": kind, "port": self.tcp_port,
            "uuid": self.node_uuid, "ts": int(time.time()), "tok": TOKEN,
        }).encode()

    def _decode(self, data):
        try:
            m = json.loads(data)
        except Exception:
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
        except Exception:
            pass
        s.bind(("", MULTICAST_PORT))
        for ip in self.local_ips:
            try:
                mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton(ip)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError:
                continue
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, TTL)
        except OSError:
            pass
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        s.settimeout(2.0)
        self.sock = s

    def _recv_loop(self):
        while self.running_flag() and not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            m = self._decode(data)
            if not m or m.get("uuid") == self.node_uuid:
                continue
            remote_ip = addr[0]
            try:
                port = int(m["port"])
            except Exception:
                continue
            key = f"{remote_ip}:{port}"
            now = time.time()
            if key in self._seen and now - self._seen[key] < 30:
                continue
            self._seen[key] = now
            try:
                self.on_peer(remote_ip, port)
            except Exception as e:
                log.debug(f"on_peer({key}): {e}")

    def _send_loop(self):
        while self.running_flag() and not self._stop.is_set():
            ping = self._encode("PING")
            for target in [(MULTICAST_GROUP, MULTICAST_PORT),
                           ("255.255.255.255", MULTICAST_PORT)]:
                try:
                    self.sock.sendto(ping, target)
                except Exception:
                    pass
            n = len(self._seen)
            wait = 2.0 if n < 2 else (10.0 if n < 5 else 30.0)
            if self._stop.wait(wait):
                break

    def run(self):
        try:
            self._open()
        except Exception as e:
            log.error(f"[UDP] falha ao abrir socket: {e}")
            return
        log.info(f"[UDP] ativo IPs={self.local_ips}")
        threading.Thread(target=self._send_loop, daemon=True,
                         name="UDP-Send").start()
        self._recv_loop()

    def stop(self):
        self._stop.set()
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass


class MDNSDiscovery(threading.Thread):
    """Placeholder — requer zeroconf. Não obrigatório."""

    def __init__(self, tcp_port, on_peer, running_flag, node_uuid=""):
        super().__init__(daemon=True, name="mDNS")
        self.port = tcp_port
        self.on_peer = on_peer
        self.running_flag = running_flag
        self.node_uuid = node_uuid
        self._stop = threading.Event()

    def run(self):
        log.info("[mDNS] desabilitado (zeroconf nao obrigatorio)")

    def stop(self):
        self._stop.set()