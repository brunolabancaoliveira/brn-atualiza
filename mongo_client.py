"""
mongo_client.py — Cliente MongoDB compartilhado (explorer + nó) (v2)
=====================================================================
Cliente lazy, thread-safe, tolerante a falhas. Falha do Mongo NÃO
derruba o processo — apenas desabilita funcionalidades que dependem dele.

v2:
  [FIX] client() com double-checked locking (antes criava 2 clients)
  [FIX] close() reseta _last_error e _last_ping
  [FIX] _build_uri distingue mongodb+srv:// (Atlas) de mongodb:// (self-host)
  [FIX] exceções específicas: ServerSelectionTimeoutError, OperationFailure,
        ConfigurationError (antes só PyMongoError genérico)
  [FIX] URI com senha não vaza em logs (usa _mask_uri)
  [FIX] MONGO_PASS placeholder detectado e desabilita
  [NEW] is_configured() público
  [NEW] ensure_indexes() — cria índices para blocks/txs/peers/escrows
  [NEW] heartbeat(interval) — thread daemon com ping periódico
  [NEW] status() com URI mascarada + tempo desde último ping
  [NEW] collection(name) atalho
  [NEW] CLI: --info / --check / --indexes

Env:
  MONGO_URI    (preferido — sobrescreve tudo)
  MONGO_USER   (opcional, se URI não setada)
  MONGO_PASS   (opcional)
  MONGO_HOST   (obrigatório se URI não setada)
  MONGO_DB     (default "brn_analytics")
=====================================================================
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from typing import Optional
from urllib.parse import quote_plus

log = logging.getLogger("mongo")

# ============================================================
# IMPORT PYMONGO (opcional)
# ============================================================
try:
    from pymongo import MongoClient
    from pymongo.errors import (
        PyMongoError,
        ServerSelectionTimeoutError,
        OperationFailure,
        ConfigurationError,
    )
    _HAS_PYMONGO = True
except ImportError:
    _HAS_PYMONGO = False
    MongoClient = None
    PyMongoError = Exception
    ServerSelectionTimeoutError = Exception
    OperationFailure = Exception
    ConfigurationError = Exception


# ============================================================
# ÍNDICES PADRÃO
# ------------------------------------------------------------
# (coleção, chave, unique, nome)
# ============================================================
DEFAULT_INDEXES = [
    ("blocks",   [("height", 1)],          True,  "idx_height_unique"),
    ("blocks",   [("hash", 1)],            True,  "idx_hash_unique"),
    ("txs",      [("txid", 1)],            True,  "idx_txid_unique"),
    ("txs",      [("block_height", 1)],    False, "idx_block_height"),
    ("txs",      [("outputs.address", 1)], False, "idx_outputs_address"),
    ("peers",    [("addr", 1)],            True,  "idx_peer_addr_unique"),
    ("peers",    [("last_seen", 1)],       False, "idx_peer_last_seen"),
    ("escrows",  [("escrow_id", 1)],       True,  "idx_escrow_id_unique"),
    ("escrows",  [("buyer", 1)],           False, "idx_escrow_buyer"),
    ("escrows",  [("status", 1)],          False, "idx_escrow_status"),
]


# ============================================================
# HELPERS
# ============================================================
def _mask_uri(uri: str) -> str:
    """Remove a senha de uma URI mongo para log. 'user:senha@host' → 'user:***@host'."""
    if not uri:
        return ""
    try:
        if "://" not in uri:
            return uri
        scheme, rest = uri.split("://", 1)
        if "@" not in rest:
            return uri
        creds, host = rest.split("@", 1)
        if ":" in creds:
            user, _, _ = creds.partition(":")
            return f"{scheme}://{user}:***@{host}"
        return f"{scheme}://{creds}@{host}"
    except Exception:
        return "***"


def _is_atlas_host(host: str) -> bool:
    """Detecta se é um host Atlas (precisa mongodb+srv://)."""
    h = (host or "").lower()
    return ".mongodb.net" in h or h.startswith("cluster")


# ============================================================
# CLASSE MONGO
# ============================================================
class Mongo:
    """
    Cliente lazy do MongoDB.

    - Não conecta no __init__ — só na primeira operação.
    - Falha de conexão NÃO derruba o processo (retorna None / False).
    - Todos os métodos públicos são thread-safe.
    """

    def __init__(self) -> None:
        self._client = None
        self._lock = threading.RLock()
        self._last_error: Optional[str] = None
        self._last_ping: float = 0.0
        self._last_ping_ok: bool = False

        self.uri: Optional[str] = None
        self.db_name: str = os.environ.get("MONGO_DB", "brn_analytics")

        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_interval = 0

        if not _HAS_PYMONGO:
            log.warning("pymongo não instalado — Mongo desabilitado")
            return

        self.uri = self._build_uri()
        if self.uri:
            log.info("Mongo configurado: %s (db=%s)",
                     _mask_uri(self.uri), self.db_name)
        else:
            log.info("Mongo não configurado (env MONGO_* ausentes)")

    # --------------------------------------------------------
    # CONFIG
    # --------------------------------------------------------
    @staticmethod
    def _build_uri() -> Optional[str]:
        """
        Prioridade:
          1) MONGO_URI (completa)
          2) MONGO_USER + MONGO_PASS + MONGO_HOST
        Detecta Atlas vs self-hosted.
        """
        uri = (os.environ.get("MONGO_URI", "") or "").strip()
        if uri:
            return uri

        user = (os.environ.get("MONGO_USER", "") or "").strip()
        pwd  = (os.environ.get("MONGO_PASS", "") or "").strip()
        host = (os.environ.get("MONGO_HOST", "") or "").strip()

        if not (user and pwd and host):
            return None

        # Se o usuário esqueceu de trocar o placeholder, avisa e ignora
        if pwd.upper() in ("SUA_SENHA_DO_ATLAS_AQUI",
                           "SUA_SENHA_AQUI",
                           "CHANGEME",
                           "PASSWORD"):
            log.warning("MONGO_PASS parece ser um placeholder (%s) — "
                        "Mongo será desabilitado até você trocar", pwd)
            return None

        u = quote_plus(user)
        p = quote_plus(pwd)

        if _is_atlas_host(host):
            # Atlas: mongodb+srv://
            if host.endswith("/"):
                host = host[:-1]
            return (f"mongodb+srv://{u}:{p}@{host}/"
                    f"?retryWrites=true&w=majority")

        # Self-hosted: mongodb://
        # Aceita host:porta. Se não tem porta, adiciona :27017
        if ":" not in host.split("/")[0]:
            host = host + ":27017"
        # Preserva path se houver
        if "/" in host:
            netloc, _, path = host.partition("/")
            return f"mongodb://{u}:{p}@{netloc}/{path}"
        return f"mongodb://{u}:{p}@{host}/"

    def is_configured(self) -> bool:
        """True se há URI e pymongo instalado."""
        return _HAS_PYMONGO and bool(self.uri)

    # --------------------------------------------------------
    # CLIENT
    # --------------------------------------------------------
    def client(self):
        """
        Retorna o MongoClient (cria na primeira chamada).
        Double-checked locking: rápido no caminho quente, seguro na criação.
        """
        if not _HAS_PYMONGO or not self.uri:
            return None

        # Caminho rápido (fora do lock)
        c = self._client
        if c is not None:
            return c

        # Caminho lento — cria
        with self._lock:
            if self._client is not None:
                return self._client
            try:
                self._client = MongoClient(
                    self.uri,
                    serverSelectionTimeoutMS=3000,
                    connectTimeoutMS=3000,
                    socketTimeoutMS=5000,
                    maxPoolSize=20,
                    minPoolSize=0,
                    appname="brn-node",
                    retryWrites=True,
                )
                log.info("Mongo client criado (db=%s)", self.db_name)
                self._last_error = None
            except ConfigurationError as e:
                self._last_error = f"config: {e}"
                log.error("Mongo config inválida: %s", e)
                self._client = None
            except Exception as e:
                self._last_error = str(e)
                log.error("Falha criando client Mongo: %s", e)
                self._client = None
        return self._client

    def db(self):
        """Retorna o database (ou None se não configurado)."""
        c = self.client()
        if c is None:
            return None
        try:
            return c[self.db_name]
        except Exception as e:
            log.warning("mongo.db() falhou: %s", e)
            return None

    def collection(self, name: str):
        """Atalho: retorna db[name] ou None."""
        d = self.db()
        if d is None:
            return None
        return d[name]

    # --------------------------------------------------------
    # PING
    # --------------------------------------------------------
    def ping(self) -> tuple[bool, Optional[str]]:
        """
        Ping rápido. Retorna (ok, erro_str_ou_None).
        Atualiza _last_ping e _last_ping_ok.
        """
        if not _HAS_PYMONGO:
            return False, "pymongo não instalado"
        if not self.uri:
            return False, "não configurado"

        c = self.client()
        if c is None:
            return False, self._last_error or "client indisponível"

        try:
            c.admin.command("ping")
            self._last_ping = time.time()
            self._last_ping_ok = True
            self._last_error = None
            return True, None
        except ServerSelectionTimeoutError as e:
            self._last_error = f"timeout: {e}"
            self._last_ping_ok = False
            return False, self._last_error
        except OperationFailure as e:
            self._last_error = f"auth: {e}"
            self._last_ping_ok = False
            return False, self._last_error
        except PyMongoError as e:
            self._last_error = str(e)
            self._last_ping_ok = False
            return False, self._last_error
        except Exception as e:
            self._last_error = str(e)
            self._last_ping_ok = False
            return False, self._last_error

    # --------------------------------------------------------
    # ÍNDICES
    # --------------------------------------------------------
    def ensure_indexes(self,
                       specs: Optional[list] = None) -> dict:
        """
        Cria índices idempotentemente.
        specs: lista de (colecao, chave, unique, nome).
        Default: DEFAULT_INDEXES.

        Retorna dict com contadores: {"ok": n, "falhas": n, "erros": [...]}
        """
        d = self.db()
        if d is None:
            return {"ok": 0, "falhas": 0, "erros": ["db indisponível"]}

        specs = specs or DEFAULT_INDEXES
        ok = 0
        falhas = 0
        erros = []

        for col, key, unique, name in specs:
            try:
                d[col].create_index(key, unique=unique, name=name)
                ok += 1
            except Exception as e:
                falhas += 1
                erros.append(f"{col}.{name}: {e}")
                log.debug("ensure_indexes %s.%s: %s", col, name, e)

        if ok:
            log.info("Mongo índices: %d ok, %d falhas", ok, falhas)
        return {"ok": ok, "falhas": falhas, "erros": erros}

    # --------------------------------------------------------
    # HEARTBEAT
    # --------------------------------------------------------
    def heartbeat(self, interval: int = 60) -> None:
        """
        Inicia thread daemon que pinga a cada `interval` segundos.
        Idempotente — chamar 2x é no-op.
        """
        with self._lock:
            if self._heartbeat_thread is not None and \
               self._heartbeat_thread.is_alive():
                return
            if not self.is_configured():
                return

            self._heartbeat_interval = interval
            self._heartbeat_stop.clear()
            t = threading.Thread(
                target=self._heartbeat_loop,
                args=(interval,),
                daemon=True,
                name="MongoHeartbeat",
            )
            self._heartbeat_thread = t
            t.start()
            log.info("Mongo heartbeat a cada %ds", interval)

    def _heartbeat_loop(self, interval: int) -> None:
        while not self._heartbeat_stop.wait(interval):
            try:
                ok, err = self.ping()
                if not ok:
                    log.debug("Mongo heartbeat falhou: %s", err)
            except Exception as e:
                log.debug("Mongo heartbeat exception: %s", e)

    def stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------
    def status(self) -> dict:
        now = time.time()
        return {
            "configured":   self.is_configured(),
            "pymongo":      _HAS_PYMONGO,
            "uri":          _mask_uri(self.uri or ""),
            "db_name":      self.db_name,
            "client_ready": self._client is not None,
            "last_ping_ok": self._last_ping_ok,
            "last_ping_ago_s": (
                int(now - self._last_ping) if self._last_ping else None
            ),
            "last_error":   self._last_error or "",
            "heartbeat_alive": bool(
                self._heartbeat_thread is not None
                and self._heartbeat_thread.is_alive()
            ),
            "heartbeat_interval_s": self._heartbeat_interval,
        }

    # --------------------------------------------------------
    # CLOSE
    # --------------------------------------------------------
    def close(self) -> None:
        """Fecha client e reseta estado. Idempotente."""
        self.stop_heartbeat()
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
            self._last_error = None
            self._last_ping = 0.0
            self._last_ping_ok = False
        log.info("Mongo client fechado")

    def __repr__(self) -> str:
        return (f"<Mongo configured={self.is_configured()} "
                f"db={self.db_name} uri={_mask_uri(self.uri or '')}>")


# ============================================================
# SINGLETON GLOBAL
# ============================================================
mongo = Mongo()


# ============================================================
# CLI
# ============================================================
def _cmd_info() -> int:
    print(json.dumps(mongo.status(), indent=2, ensure_ascii=False))
    return 0


def _cmd_check() -> int:
    print(f"URI   : {_mask_uri(mongo.uri or '(não configurado)')}")
    print(f"DB    : {mongo.db_name}")
    print(f"PyMongo: {'ok' if _HAS_PYMONGO else 'NÃO INSTALADO'}")
    print()

    if not mongo.is_configured():
        print("Mongo não configurado. Defina MONGO_URI ou "
              "MONGO_USER/MONGO_PASS/MONGO_HOST.", file=sys.stderr)
        return 1

    print("Pingando...")
    ok, err = mongo.ping()
    if ok:
        print("OK ✓")
        try:
            d = mongo.db()
            nomes = d.list_collection_names()
            print(f"Coleções ({len(nomes)}): {', '.join(nomes) or '(nenhuma)'}")
        except Exception as e:
            print(f"AVISO: list_collection_names falhou: {e}")
        return 0
    else:
        print(f"FALHOU: {err}", file=sys.stderr)
        return 1


def _cmd_indexes() -> int:
    if not mongo.is_configured():
        print("Mongo não configurado.", file=sys.stderr)
        return 1
    print("Criando índices...")
    r = mongo.ensure_indexes()
    print(json.dumps(r, indent=2, ensure_ascii=False))
    return 0 if r["falhas"] == 0 else 1


def _cli() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    p = argparse.ArgumentParser(prog="mongo_client.py")
    p.add_argument("--info", action="store_true",
                   help="mostra config/status (URI mascarada)")
    p.add_argument("--check", action="store_true",
                   help="ping + lista coleções")
    p.add_argument("--indexes", action="store_true",
                   help="cria índices padrão")
    args = p.parse_args()

    if args.info:
        return _cmd_info()
    if args.check:
        return _cmd_check()
    if args.indexes:
        return _cmd_indexes()

    p.print_help()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_cli())
    except KeyboardInterrupt:
        sys.exit(130)
