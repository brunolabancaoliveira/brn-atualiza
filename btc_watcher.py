# btc_watcher.py - v4 - Confirma BTC via RPC público e envia pra mempool BRN
# ============================================================
# v4:
#   [FIX] usa requests.Session (keep-alive) e fecha no stop()
#         → elimina ResourceWarning "' ' was never closed" (linha 147)
#   [FIX] stop() com threading.Event (antes só self.running bool)
#   [FIX] valida None em escrow['expires_at'] / ['btc_expected_sats']
#   [FIX] trata HTTP 429 / 5xx com backoff (evita ban do Blockstream)
#   [FIX] logger em vez de print
#   [FIX] reconstrói o final do arquivo (Mempool.space fallback)
#   [NEW] info() para diagnóstico
#   [NEW] add_l2_transaction envolvido em try/except com aviso claro
#         (esse método não existe no blockchain.py v9.1.4 — se não existir
#          no seu, o L2 nunca libera; adicione ou troque por submit_tx)
# ============================================================
import time
import logging
import threading

import requests

import btc_config

log = logging.getLogger("btc_watcher")

# Config RPC (Blockstream + Mempool.space)
BLOCKSTREAM_MAINNET = "https://blockstream.info/api"
BLOCKSTREAM_TESTNET = "https://blockstream.info/testnet/api"
MEMPOOL_MAINNET     = "https://mempool.space/api"
MEMPOOL_TESTNET     = "https://mempool.space/testnet/api"

POLL_INTERVAL_S     = 30
HTTP_TIMEOUT_S      = 15
HTTP_TIP_TIMEOUT_S  = 5
MAX_TXS_PER_ADDR    = 30
BACKOFF_ON_429_S    = 120


class BTCWatcher(threading.Thread):
    def __init__(self, db, blockchain):
        super().__init__(daemon=True, name="BTCWatcher")
        self.db = db
        self.blockchain = blockchain
        self._stop_evt = threading.Event()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "brn-btc-watcher/4"})

        self._backoff_until = 0.0

        # Endereço placeholder → não roda
        addr = getattr(btc_config, "BTC_RECEIVE_ADDRESS", "") or ""
        self._enabled = bool(addr) and "SEU_ENDERECO" not in addr

        log.info("[BTC Watcher] Iniciado")
        log.info("[BTC Watcher] Endereço: %s", addr or "(vazio)")
        log.info("[BTC Watcher] Rede: %s", getattr(btc_config, "BTC_NETWORK", "?"))
        log.info("[BTC Watcher] Confirmações: %s",
                 getattr(btc_config, "BTC_MIN_CONFIRMATIONS", "?"))
        if not self._enabled:
            log.warning("[BTC Watcher] Endereço placeholder — watcher NÃO vai rodar")

    # --------------------------------------------------------
    # STOP / CONTROLE
    # --------------------------------------------------------
    @property
    def running(self) -> bool:
        return not self._stop_evt.is_set()

    def stop(self):
        self._stop_evt.set()
        try:
            self._session.close()
        except Exception:
            pass

    def info(self) -> dict:
        return {
            "running": self.running,
            "enabled": self._enabled,
            "address": getattr(btc_config, "BTC_RECEIVE_ADDRESS", ""),
            "network": getattr(btc_config, "BTC_NETWORK", ""),
            "min_confirmations": getattr(btc_config, "BTC_MIN_CONFIRMATIONS", 0),
            "backoff_until": self._backoff_until,
        }

    # --------------------------------------------------------
    # LOOP PRINCIPAL
    # --------------------------------------------------------
    def run(self):
        if not self._enabled:
            return

        while not self._stop_evt.is_set():
            try:
                if time.time() < self._backoff_until:
                    wait = max(1, int(self._backoff_until - time.time()))
                    log.debug("[BTC Watcher] backoff ativo — aguardando %ds", wait)
                    if self._stop_evt.wait(timeout=min(wait, 10)):
                        break
                    continue

                self.check_rpc()
            except Exception as e:
                log.warning("[BTC Watcher] Erro: %s", e)

            if self._stop_evt.wait(timeout=POLL_INTERVAL_S):
                break

        log.info("[BTC Watcher] Encerrado")

    # --------------------------------------------------------
    # CHECK
    # --------------------------------------------------------
    def check_rpc(self):
        try:
            escrows = self.db.get_l2_escrows(status="OPEN")
        except Exception as e:
            log.warning("[BTC Watcher] get_l2_escrows falhou: %s", e)
            return

        if not escrows:
            return

        btc_txs = self.fetch_via_rpc(btc_config.BTC_RECEIVE_ADDRESS)
        if not btc_txs:
            return

        agora = time.time()

        for escrow in escrows:
            # Campos podem vir None do DB
            escrow_id = escrow.get("escrow_id", "")
            expires_at = escrow.get("expires_at")
            expected_sats = escrow.get("btc_expected_sats")

            # Expirado?
            try:
                if expires_at and float(expires_at) < agora:
                    self.db.update_l2_escrow_status(escrow_id, "EXPIRED")
                    log.info("[BTC Watcher] Escrow expirado: %s", escrow_id)
                    continue
            except (TypeError, ValueError):
                pass

            if not expected_sats:
                log.debug("[BTC Watcher] escrow %s sem btc_expected_sats",
                          escrow_id[:12])
                continue

            min_sats = int(float(expected_sats) * 0.99)

            for btc_tx in btc_txs:
                txid_btc = btc_tx.get("txid", "")
                if not txid_btc:
                    continue

                # Anti-replay
                try:
                    if self.db.is_btc_txid_used(txid_btc):
                        continue
                except Exception:
                    pass

                # Valor mínimo
                if btc_tx.get("value_sats", 0) < min_sats:
                    continue

                # Confirmações
                needed = int(getattr(btc_config, "BTC_MIN_CONFIRMATIONS", 3))
                confs = int(btc_tx.get("confirmations", 0))
                if confs < needed:
                    log.info("[BTC Watcher] BTC %s... só %d/%d confirmações",
                             txid_btc[:16], confs, needed)
                    continue

                # ====== BTC CONFIRMADO ======
                log.info("[BTC Watcher] ============ BTC CONFIRMADO ============")
                log.info("[BTC Watcher] Escrow: %s", escrow_id)
                log.info("[BTC Watcher] BTC TXID: %s", txid_btc)
                log.info("[BTC Watcher] Valor: %.8f BTC",
                         btc_tx.get("value_sats", 0) / 1e8)
                log.info("[BTC Watcher] Confirmações: %d", confs)
                log.info("[BTC Watcher] Comprador BRN: %s",
                         escrow.get("buyer", ""))
                log.info("[BTC Watcher] BRN a liberar: %.8f BRN",
                         (escrow.get("brn_amount") or 0) / 1e8)
                log.info("[BTC Watcher] Enviando pra mempool BRN...")

                l2_tx = {
                    "type":         "l2_settlement",
                    "escrow_id":    escrow_id,
                    "buyer":        escrow.get("buyer", ""),
                    "brn_amount":   escrow.get("brn_amount", 0),
                    "btc_txid":     txid_btc,
                    "btc_address":  escrow.get("btc_address", ""),
                    "l2_hash":      escrow.get("l2_hash", ""),
                    "timestamp":    int(agora),
                }

                # ⚠️ ATENÇÃO: `add_l2_transaction` NÃO existe no blockchain.py v9.1.4.
                # Se você não tem esse método, o L2 nunca libera.
                # Opções:
                #   a) implementar add_l2_transaction no blockchain.py
                #   b) usar `submit_tx` com a tx formatada corretamente
                #   c) fazer o crédito direto via db.apply_tx
                try:
                    fn = getattr(self.blockchain, "add_l2_transaction", None)
                    if fn is None:
                        log.error("[BTC Watcher] blockchain.add_l2_transaction "
                                  "não existe — L2 não será liberado. "
                                  "Implemente esse método ou use submit_tx.")
                        continue
                    txid = fn(l2_tx)
                    log.info("[BTC Watcher] L2 TX %s enviada pra mempool. "
                             "Aguardando minerador.", txid)
                except Exception as e:
                    log.error("[BTC Watcher] Erro enviando pra mempool: %s", e)

    # --------------------------------------------------------
    # RPC PÚBLICO
    # --------------------------------------------------------
    def _http_get_json(self, url: str, timeout: int = HTTP_TIMEOUT_S):
        """
        GET que devolve JSON, fechando o response corretamente.
        Trata 429 e 5xx com backoff.
        Retorna (data, None) ou (None, erro_str).
        """
        try:
            with self._session.get(url, timeout=timeout) as r:
                if r.status_code == 429:
                    self._backoff_until = time.time() + BACKOFF_ON_429_S
                    log.warning("[RPC] HTTP 429 em %s — backoff %ds",
                                url, BACKOFF_ON_429_S)
                    return None, "429"
                if r.status_code >= 500:
                    return None, f"http {r.status_code}"
                if r.status_code != 200:
                    return None, f"http {r.status_code}"
                # r.json() lê o corpo completo → libera o socket
                return r.json(), None
        except requests.exceptions.Timeout:
            return None, "timeout"
        except requests.exceptions.ConnectionError as e:
            return None, f"conn: {e}"
        except Exception as e:
            return None, str(e)

    def _http_get_text(self, url: str, timeout: int = HTTP_TIP_TIMEOUT_S):
        try:
            with self._session.get(url, timeout=timeout) as r:
                if r.status_code != 200:
                    return None
                return r.text.strip()
        except Exception:
            return None

    def fetch_via_rpc(self, address):
        """
        Busca TXs da carteira BTC via RPC público.
        Tenta Blockstream primeiro, depois Mempool.space.
        """
        if not address:
            return []

        # ---- Blockstream ----
        txs = self._fetch_from(
            base=(BLOCKSTREAM_MAINNET
                  if getattr(btc_config, "BTC_NETWORK", "mainnet") == "mainnet"
                  else BLOCKSTREAM_TESTNET),
            address=address,
            nome="Blockstream",
        )
        if txs:
            return txs

        # ---- Mempool.space ----
        txs = self._fetch_from(
            base=(MEMPOOL_MAINNET
                  if getattr(btc_config, "BTC_NETWORK", "mainnet") == "mainnet"
                  else MEMPOOL_TESTNET),
            address=address,
            nome="Mempool.space",
        )
        return txs or []

    def _fetch_from(self, base: str, address: str, nome: str):
        data, err = self._http_get_json(f"{base}/address/{address}/txs")
        if err or data is None:
            log.debug("[RPC] %s falhou: %s", nome, err)
            return []
        if not isinstance(data, list):
            return []

        # Altura atual da chain BTC (para calcular confirmações)
        tip_str = self._http_get_text(f"{base}/blocks/tip/height")
        try:
            tip_height = int(tip_str) if tip_str else 0
        except (ValueError, TypeError):
            tip_height = 0

        txs = []
        for tx in data[:MAX_TXS_PER_ADDR]:
            if not isinstance(tx, dict):
                continue

            # Soma outputs que vão para o nosso endereço
            val = 0
            for vout in tx.get("vout", []):
                if vout.get("scriptpubkey_address") == address:
                    val += int(vout.get("value", 0) or 0)
            if val == 0:
                continue

            # Confirmações
            status = tx.get("status", {}) or {}
            confirmations = 0
            if status.get("confirmed"):
                bh = status.get("block_height")
                if bh and tip_height:
                    confirmations = max(0, tip_height - int(bh) + 1)
                else:
                    confirmations = int(getattr(
                        btc_config, "BTC_MIN_CONFIRMATIONS", 3))

            txs.append({
                "txid":          tx.get("txid", ""),
                "value_sats":    val,
                "confirmations": confirmations,
            })

        return txs
