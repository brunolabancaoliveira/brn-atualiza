"""
rendezvous_discovery.py — Descoberta via hub central (internet, HTTP)
"""
from __future__ import annotations

import os
import time
import json
import logging
import threading
import urllib.request
import urllib.error
from typing import Callable, Optional

log = logging.getLogger("rendezvous")

DEFAULT_TIMEOUT  = 10
DEFAULT_INTERVAL = 120
INITIAL_DELAY    = 5
MAX_PEERS_PER_ROUND = 100


class RendezvousDiscovery(threading.Thread):
    def __init__(self, tcp_port: int, node_uuid: str,
                 on_peer: Callable[[str, int], None],
                 running_flag: Callable[[], bool],
                 sign_fn: Optional[Callable[[bytes], bytes]] = None) -> None:
        super().__init__(daemon=True, name="Rendezvous")
        self.tcp_port = tcp_port
        self.node_uuid = node_uuid
        self.on_peer = on_peer
        self.running_flag = running_flag
        self.sign_fn = sign_fn

        self.base_url = os.environ.get("BRN_RENDEZVOUS_URL", "").strip().rstrip("/")
        self.interval = int(os.environ.get("BRN_RENDEZVOUS_INTERVAL",
                                            str(DEFAULT_INTERVAL)))
        self.timeout  = int(os.environ.get("BRN_RENDEZVOUS_TIMEOUT",
                                            str(DEFAULT_TIMEOUT)))
        self.ip_hint  = os.environ.get("BRN_PUBLIC_IP", "").strip()

        self._seen: dict[str, float] = {}
        self._seen_lock = threading.Lock()
        self._stop_evt = threading.Event()

        if not self.base_url:
            log.warning("BRN_RENDEZVOUS_URL vazio — rendezvous desabilitado")

    def _post(self, path: str, body: dict) -> Optional[dict]:
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "brn-node-rendezvous/1"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            log.warning(f"rendezvous POST {path}: HTTP {e.code}")
        except urllib.error.URLError as e:
            log.debug(f"rendezvous POST {path}: rede — {e.reason}")
        except Exception as e:
            log.debug(f"rendezvous POST {path}: {e}")
        return None

    def _announce(self) -> Optional[dict]:
        ts = int(time.time())
        payload = {"uuid": self.node_uuid, "port": self.tcp_port,
                   "ts": ts, "ip_hint": self.ip_hint, "ua": "brn-node/1"}
        if self.sign_fn is not None:
            try:
                sig = self.sign_fn(f"{self.node_uuid}|{self.tcp_port}|{ts}".encode())
                if isinstance(sig, (bytes, bytearray)): sig = sig.hex()
                payload["sig"] = sig
            except Exception as e:
                log.debug(f"sign_fn falhou: {e}")
        resp = self._post("/api/rendezvous/announce", payload)
        return resp if resp and resp.get("ok") else None

    def _dispatch(self, peers: list) -> int:
        added = 0
        now = time.time()
        for p in peers[:MAX_PEERS_PER_ROUND]:
            ip = str(p.get("ip", "")).strip()
            port = p.get("port")
            uuid = p.get("uuid", "")
            if not ip or not isinstance(port, int) or uuid == self.node_uuid:
                continue
            key = f"{ip}:{port}"
            with self._seen_lock:
                if now - self._seen.get(key, 0) < 300:
                    continue
                self._seen[key] = now
            try:
                self.on_peer(ip, port); added += 1
            except Exception as e:
                log.debug(f"on_peer({ip}:{port}) falhou: {e}")
        return added

    def run(self):
        if not self.base_url:
            return
        log.info(f"Rendezvous ativo → {self.base_url} "
                 f"(intervalo {self.interval}s)")
        if self._stop_evt.wait(INITIAL_DELAY):
            return
        consecutive = 0
        while self.running_flag() and not self._stop_evt.is_set():
            try:
                resp = self._announce()
                if resp:
                    consecutive = 0
                    peers = resp.get("peers", [])
                    n = self._dispatch(peers)
                    if n:
                        log.info(f"rendezvous: +{n} peer(s) (total={len(peers)})")
                else:
                    consecutive += 1
                    if consecutive in (1, 5, 20):
                        log.warning(f"rendezvous sem resposta ({consecutive}x)")
            except Exception as e:
                log.warning(f"rendezvous loop: {e}")
            wait = self.interval if consecutive <= 5 else min(self.interval * 2, 600)
            if self._stop_evt.wait(wait):
                break

    def stop(self):
        self._stop_evt.set()