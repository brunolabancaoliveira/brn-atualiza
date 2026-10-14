"""contracts/vm.py — VM de contratos do BRN
================================================================
VM simples, determinística e limitada. Não é EVM — é uma máquina
de operações declarativas com sandbox de estado.

Formato do contrato (dict):
{
    "version": 1,
    "name": "Counter",
    "initial_state": {"count": 0},
    "methods": {
        "increment": [
            {"op": "add", "path": "count", "value": 1},
            {"op": "emit", "event": "Incremented"}
        ],
        "reset": [
            {"op": "set", "path": "count", "value": 0}
        ]
    }
}

Operações suportadas:
    set          → state[path] = value
    add          → state[path] += value (cria se não existir)
    sub          → state[path] -= value
    emit         → registra evento (contract_events)
    require_eq   → aborta se state[path] != value
    require_ge   → aborta se state[path] < value
    transfer     → db.contract_spend(from, to, amount)
    inc_nonce    → incrementa nonce do contrato

API pública:
    ContractVM.validate(code) -> (ok, msg)
    deploy(chain, owner, code, metadata=None) -> dict
    call(chain, contract_id, caller, args) -> dict

Requer:
    db.py v6.4+ (métodos contract_*)
================================================================
"""
from __future__ import annotations

import hashlib
import os
import re
import time

# ============================================================
# LIMITES (sandbox)
# ============================================================
MAX_OPS_PER_CALL   = 64
MAX_STATE_KEYS     = 1000
MAX_PATH_LEN       = 64
MAX_STRING_LEN     = 256
MAX_METHODS        = 32
MAX_CONTRACT_NAME  = 64
CONTRACT_ID_PREFIX = "ctr1"


# ============================================================
# HELPERS
# ============================================================
_PATH_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def _valid_path(p: str) -> bool:
    return bool(isinstance(p, str) and _PATH_RE.match(p))


def _now() -> int:
    return int(time.time())


def _new_contract_id(owner: str, code: dict) -> str:
    """Gera ID determinístico-suficiente (prefixo ctr1 + 32 hex)."""
    h = hashlib.sha256()
    h.update(owner.encode("utf-8", "ignore"))
    h.update(repr(code).encode("utf-8", "ignore"))
    h.update(os.urandom(8))
    h.update(str(_now()).encode())
    return CONTRACT_ID_PREFIX + h.hexdigest()[:32]


def _get_state(db, cid: str) -> dict:
    try:
        s = db.contract_get_state(cid)
        return dict(s) if isinstance(s, dict) else {}
    except Exception:
        return {}


def _set_state(db, cid: str, state: dict) -> None:
    # limita tamanho
    if len(state) > MAX_STATE_KEYS:
        raise ValueError(f"state muito grande ({len(state)} chaves)")
    db.contract_set_state(cid, state)


# ============================================================
# ContractVM.validate
# ============================================================
class ContractVM:
    """Validador estático do contrato (sem executar)."""

    @staticmethod
    def validate(code) -> tuple:
        if not isinstance(code, dict):
            return False, "code deve ser dict"

        # versão
        v = code.get("version", 1)
        if v != 1:
            return False, f"versao {v} nao suportada"

        # nome
        name = code.get("name", "")
        if not isinstance(name, str) or not name:
            return False, "campo 'name' obrigatorio (string)"
        if len(name) > MAX_CONTRACT_NAME:
            return False, f"name excede {MAX_CONTRACT_NAME} chars"

        # initial_state
        st = code.get("initial_state", {})
        if not isinstance(st, dict):
            return False, "initial_state deve ser dict"
        if len(st) > MAX_STATE_KEYS:
            return False, f"initial_state com muitas chaves (> {MAX_STATE_KEYS})"
        for k, v2 in st.items():
            if not _valid_path(k):
                return False, f"chave invalida em initial_state: {k!r}"
            if isinstance(v2, str) and len(v2) > MAX_STRING_LEN:
                return False, f"string muito longa em initial_state[{k}]"
            if not isinstance(v2, (int, float, str, bool, type(None))):
                return False, f"tipo invalido em initial_state[{k}]"

        # methods
        methods = code.get("methods", {})
        if not isinstance(methods, dict):
            return False, "methods deve ser dict"
        if len(methods) > MAX_METHODS:
            return False, f"metodos demais (> {MAX_METHODS})"
        for mname, ops in methods.items():
            if not isinstance(mname, str) or not _valid_path(mname):
                return False, f"nome de metodo invalido: {mname!r}"
            if not isinstance(ops, list):
                return False, f"metodo {mname} deve ser lista de ops"
            if len(ops) > MAX_OPS_PER_CALL:
                return False, f"metodo {mname} com ops demais"
            for i, op in enumerate(ops):
                ok, msg = ContractVM._validate_op(op, mname, i)
                if not ok:
                    return False, msg

        return True, "ok"

    @staticmethod
    def _validate_op(op, method: str, idx: int) -> tuple:
        if not isinstance(op, dict):
            return False, f"op #{idx} em {method}: nao e dict"
        kind = op.get("op")
        if not isinstance(kind, str):
            return False, f"op #{idx} em {method}: 'op' ausente"

        if kind in ("set", "add", "sub", "require_eq", "require_ge"):
            if not _valid_path(op.get("path", "")):
                return False, f"op #{idx} em {method}: path invalido"
            if kind in ("set", "add", "sub", "require_eq", "require_ge"):
                val = op.get("value")
                if not isinstance(val, (int, float, str, bool)):
                    return False, f"op #{idx} em {method}: value invalido"
                if isinstance(val, str) and len(val) > MAX_STRING_LEN:
                    return False, f"op #{idx}: string muito longa"

        elif kind == "emit":
            ev = op.get("event", "")
            if not isinstance(ev, str) or not ev:
                return False, f"op #{idx} em {method}: 'event' obrigatorio"
            if len(ev) > MAX_STRING_LEN:
                return False, f"op #{idx}: evento muito longo"

        elif kind == "transfer":
            for field in ("from", "to", "amount"):
                if field not in op:
                    return False, f"op #{idx}: transfer sem '{field}'"
            if not isinstance(op["amount"], int) or op["amount"] <= 0:
                return False, f"op #{idx}: amount deve ser int > 0"

        elif kind == "inc_nonce":
            pass

        else:
            return False, f"op #{idx} em {method}: tipo desconhecido {kind!r}"

        return True, "ok"


# ============================================================
# EXECUÇÃO DE OPS
# ============================================================
def _run_op(op: dict, state: dict, ctx: dict) -> None:
    """Executa uma op. Levanta ValueError em falha."""
    kind = op["op"]

    if kind == "set":
        state[op["path"]] = op["value"]

    elif kind == "add":
        cur = state.get(op["path"], 0)
        if not isinstance(cur, (int, float)):
            cur = 0
        state[op["path"]] = cur + op["value"]

    elif kind == "sub":
        cur = state.get(op["path"], 0)
        if not isinstance(cur, (int, float)):
            cur = 0
        state[op["path"]] = cur - op["value"]

    elif kind == "require_eq":
        cur = state.get(op["path"])
        if cur != op["value"]:
            raise ValueError(
                f"require_eq falhou: {op['path']}={cur!r} != {op['value']!r}"
            )

    elif kind == "require_ge":
        cur = state.get(op["path"], 0)
        try:
            if cur < op["value"]:
                raise ValueError(
                    f"require_ge falhou: {op['path']}={cur!r} < {op['value']!r}"
                )
        except TypeError:
            raise ValueError(f"require_ge: tipos incompatíveis em {op['path']}")

    elif kind == "emit":
        db = ctx["db"]
        cid = ctx["contract_id"]
        db.contract_log_event(cid, op["event"], {
            "caller": ctx.get("caller", ""),
            "args":   ctx.get("args", {}),
            "ts":     _now(),
        })

    elif kind == "transfer":
        db = ctx["db"]
        src = op.get("from") or ctx.get("caller", "")
        dst = op["to"]
        amt = int(op["amount"])
        if not src or not dst:
            raise ValueError("transfer: from/to obrigatorios")
        db.contract_spend(src, dst, amt)
        db.contract_log_event(ctx["contract_id"], "transfer", {
            "from": src, "to": dst, "amount": amt, "ts": _now(),
        })

    elif kind == "inc_nonce":
        cur = state.get("__nonce__", 0)
        state["__nonce__"] = int(cur) + 1

    else:
        raise ValueError(f"op desconhecida: {kind}")


# ============================================================
# deploy
# ============================================================
def deploy(chain, owner: str, code: dict, metadata=None) -> dict:
    """
    Registra um novo contrato na chain.
    Retorna {"ok": bool, "contract_id": str, "msg": str}.
    """
    ok, msg = ContractVM.validate(code)
    if not ok:
        return {"ok": False, "msg": f"contrato invalido: {msg}"}

    db = chain.db

    cid = _new_contract_id(owner, code)

    try:
        db.contract_insert(cid, owner, code, metadata or {})
    except Exception as e:
        return {"ok": False, "msg": f"falha ao salvar contrato: {e}"}

    # state inicial
    initial = code.get("initial_state", {}) or {}
    if not isinstance(initial, dict):
        initial = {}
    try:
        _set_state(db, cid, dict(initial))
    except Exception as e:
        # rollback: remove o contrato
        try:
            db.contract_delete(cid)
        except Exception:
            pass
        return {"ok": False, "msg": f"falha ao salvar state: {e}"}

    db.contract_log_event(cid, "deployed", {
        "owner":    owner,
        "name":     code.get("name", ""),
        "ts":       _now(),
    })

    return {
        "ok":          True,
        "contract_id": cid,
        "owner":       owner,
        "name":        code.get("name", ""),
        "msg":         f"contrato {cid} deployado",
    }


# ============================================================
# call
# ============================================================
def call(chain, contract_id: str, caller: str, args=None) -> dict:
    """
    Executa um método do contrato.
    args = {"method": "increment", ...} ou {"method": "..."}
    Retorna {"ok": bool, "result": dict, "msg": str}.
    """
    args = args or {}
    if not isinstance(args, dict):
        return {"ok": False, "msg": "args deve ser dict"}

    method = args.get("method", "")
    if not isinstance(method, str) or not method:
        return {"ok": False, "msg": "args.method obrigatorio"}

    db = chain.db
    c = db.contract_get(contract_id)
    if not c:
        return {"ok": False, "msg": f"contrato {contract_id} nao existe"}

    code = c.get("code") or {}
    methods = code.get("methods") or {}
    if method not in methods:
        return {"ok": False, "msg": f"metodo {method!r} nao existe"}

    ops = methods[method]
    if not isinstance(ops, list):
        return {"ok": False, "msg": "ops do metodo invalidas"}
    if len(ops) > MAX_OPS_PER_CALL:
        return {"ok": False, "msg": "ops demais"}

    state = _get_state(db, contract_id)
    snapshot = dict(state)  # para rollback em caso de falha

    ctx = {
        "db":          db,
        "contract_id": contract_id,
        "caller":      caller,
        "args":        args,
        "chain":       chain,
    }

    try:
        for i, op in enumerate(ops):
            _run_op(op, state, ctx)
    except Exception as e:
        # rollback (estado + remove eventos da chamada)
        try:
            _set_state(db, contract_id, snapshot)
        except Exception:
            pass
        return {"ok": False, "msg": f"execucao falhou em op #{i}: {e}"}

    # persiste novo estado
    try:
        _set_state(db, contract_id, state)
    except Exception as e:
        return {"ok": False, "msg": f"falha ao salvar state: {e}"}

    db.contract_log_event(contract_id, "called", {
        "caller": caller,
        "method": method,
        "args":   {k: v for k, v in args.items() if k != "method"},
        "ts":     _now(),
    })

    return {
        "ok":          True,
        "contract_id": contract_id,
        "method":      method,
        "state":       state,
        "msg":         f"metodo {method} executado",
    }


# ============================================================
# AUTO-TESTE
# ============================================================
if __name__ == "__main__":
    import sys

    # Testa validação sem precisar de chain
    print("Testando ContractVM.validate()...")

    # contrato válido
    good = {
        "version": 1,
        "name": "Counter",
        "initial_state": {"count": 0},
        "methods": {
            "increment": [
                {"op": "add", "path": "count", "value": 1},
                {"op": "emit", "event": "Incremented"},
            ],
            "reset": [
                {"op": "set", "path": "count", "value": 0},
            ],
        },
    }
    ok, msg = ContractVM.validate(good)
    assert ok, f"contrato valido rejeitado: {msg}"
    print("  [ok] contrato valido aceito")

    # versão errada
    bad1 = dict(good); bad1["version"] = 99
    ok, msg = ContractVM.validate(bad1)
    assert not ok
    print(f"  [ok] versao invalida rejeitada: {msg}")

    # nome faltando
    bad2 = dict(good); del bad2["name"]
    ok, msg = ContractVM.validate(bad2)
    assert not ok
    print(f"  [ok] nome faltando: {msg}")

    # op desconhecida
    bad3 = {
        "version": 1, "name": "X",
        "initial_state": {},
        "methods": {"foo": [{"op": "explode"}]},
    }
    ok, msg = ContractVM.validate(bad3)
    assert not ok
    print(f"  [ok] op desconhecida: {msg}")

    # transfer sem campos
    bad4 = {
        "version": 1, "name": "X",
        "initial_state": {},
        "methods": {"pay": [{"op": "transfer", "to": "brn1qq"}]},
    }
    ok, msg = ContractVM.validate(bad4)
    assert not ok
    print(f"  [ok] transfer incompleto: {msg}")

    print("ContractVM OK.")