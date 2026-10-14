"""rendezvous_discovery.py — Decide entre rendezvous_mongo e HTTP.
Compatível com main.py v12.5:
    RendezvousDiscovery(tcp_port, node_uuid, on_peer, running_flag, sign_fn)
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("rendezvous")


class RendezvousDiscovery:
    """Fachada: delega para RendezvousMongo se Mongo disponível, senão no-op."""

    def __init__(self, tcp_port: int, node_uuid: str,
                 on_peer, running_flag, sign_fn=None,
                 blockchain=None):
        self.tcp_port = tcp_port
        self.node_uuid = node_uuid
        self.on_peer = on_peer
        self.running_flag = running_flag
        self.sign_fn = sign_fn
        self.blockchain = blockchain
        self._impl = None
        self._running = False

    def start(self):
        # Preferência: MongoDB (se configurado)
        try:
            from mongo_client import mongo
            if hasattr(mongo, "is_configured") and mongo.is_configured():
                from rendezvous_mongo import RendezvousMongo
                self._impl = RendezvousMongo(
                    tcp_port=self.tcp_port,
                    node_uuid=self.node_uuid,
                    on_peer=self.on_peer,
                    running_flag=self.running_flag,
                    blockchain=self.blockchain,
                )
                self._impl.start()
                self._running = True
                log.info("rendezvous: MongoDB ativo")
                return
        except ImportError:
            pass
        except Exception as e:
            log.debug(f"rendezvous mongo falhou: {e}")

        log.info("rendezvous: nenhum backend disponível (no-op)")

    def stop(self):
        if self._impl is not None:
            try:
                self._impl.stop()
            except Exception:
                pass
        self._running = False

    def get_status(self) -> dict:
        if self._impl and hasattr(self._impl, "status"):
            try:
                return self._impl.status()
            except Exception:
                pass
        return {"backend": "none", "running": self._running}