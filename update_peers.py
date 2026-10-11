"""
update_peers.py — Heartbeat do nó local em peers.json (v2)
============================================================
Atualiza o timestamp do nó em um arquivo JSON que pode ser:
  - Lido por um dashboard externo
  - Enviado ao GitHub periodicamente (opcional)
  - Publicado numa página web

v2:
  - Path absoluto (Path(__file__).parent / "peers.json")
  - Lock threading (evita corrupcao com multiplas chamadas)
  - Validacao de node_id antes de logar
  - Escrita atomica ja estava correta (mantida)
"""
import json
import time
import os
import threading
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================
PEERS_FILE = Path(__file__).parent / "peers.json"
SEU_ENDERECO = os.environ.get("BRN_SEU_ENDERECO", "177.82.132.98:6001")

if ":" not in SEU_ENDERECO:
    raise ValueError(
        "SEU_ENDERECO deve estar no formato IP:PORTA "
        "(ex: 177.82.132.98:6001)"
    )

_lock = threading.Lock()


# ============================================================
# FUNCAO PRINCIPAL
# ============================================================
def update_local_timestamp(height: int = 0, node_id: str = ""):
    """
    Atualiza ts/h/id do nó em peers.json.

    Args:
        height: altura atual da cadeia (0 = nao atualiza)
        node_id: id do nó (vazio = nao atualiza)

    Returns:
        int: timestamp atualizado
    """
    with _lock:
        agora = int(time.time())

        # 1) Carrega com tratamento de corrupcao
        peers = {}
        if PEERS_FILE.exists():
            try:
                texto = PEERS_FILE.read_text(encoding="utf-8").strip()
                if texto:
                    peers = json.loads(texto)
                    if not isinstance(peers, dict):
                        print(f"[update_peers] formato invalido — recriando")
                        peers = {}
            except json.JSONDecodeError as e:
                print(f"[update_peers] JSON corrompido — recriando: {e}")
                peers = {}
            except Exception as e:
                print(f"[update_peers] erro lendo: {e}")
                peers = {}

        # 2) Atualiza entrada deste nó
        dados_no = peers.get(SEU_ENDERECO, {})
        dados_no["ts"] = agora

        if height and height > 0:
            dados_no["h"] = height
        if node_id:
            dados_no["id"] = str(node_id)[:32]

        peers[SEU_ENDERECO] = dados_no

        # 3) Salva com escrita atomica
        tmp = PEERS_FILE.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(peers, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, PEERS_FILE)
        except Exception as e:
            print(f"[update_peers] erro salvando: {e}")
            try:
                tmp.unlink()
            except Exception:
                pass
            raise

        # 4) Log
        _id = dados_no.get("id", "")
        _id_short = _id[:8] + "..." if _id else "(sem id)"
        _h = dados_no.get("h", "?")
        print(
            f"[update_peers] {SEU_ENDERECO} | ts={agora} | "
            f"h={_h} | id={_id_short}"
        )
        return agora


# ============================================================
# CLI
# ============================================================
if __name__ == "__main__":
    update_local_timestamp()
