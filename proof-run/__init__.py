"""Proof Run — turn a successful Hermes run into a live execution contract.

/proof start <name>   begin capture for this session
/proof stop           draft the contract from recorded calls
/proof bind           activate the contract (operator gesture)
/proof status         show bound contracts
/proof rebind <name>  replace the contract after intentional change

The plugin never makes inference calls, never sends network traffic, and never
touches credentials. It only blocks or allows mutating tool calls against the
recorded contract.
"""
from __future__ import annotations

import json
from pathlib import Path

from .proof_run import core

try:
    from plugins.plugin_storage import plugin_data_dir as _host_data_dir
except ImportError:
    def _host_data_dir(name: str) -> Path:
        from hermes_cli.config import get_hermes_home
        return Path(get_hermes_home()) / "plugin-data" / name


def _contracts_root(ctx) -> Path:
    return _host_data_dir("proof-run")


def _active_recorders(ctx) -> dict:
    if not hasattr(ctx, "_proof_run_recorders"):
        ctx._proof_run_recorders = {}
    return ctx._proof_run_recorders


def _find_recorder(ctx, session_id):
    """Return the session's recorder, else the sole active one (single-session demo/CLI use)."""
    store = _active_recorders(ctx)
    rec = store.get(session_id)
    if rec is None and len(store) == 1:
        rec = next(iter(store.values()))
    return rec


def _runtime_cache(ctx) -> dict:
    """Latest results from read-only tools, used as runtime state for enforcement."""
    if not hasattr(ctx, "_proof_run_runtime"):
        ctx._proof_run_runtime = {}
    return ctx._proof_run_runtime


def _session_buffer(ctx) -> list:
    """Rolling buffer of this session's tool calls for retroactive learning."""
    if not hasattr(ctx, "_proof_run_session_buffer"):
        ctx._proof_run_session_buffer = []
    return ctx._proof_run_session_buffer


_SESSION_BUFFER_MAX = 100


def _on_post_tool_call(ctx, tool_name, args, result, **kwargs):
    session_id = kwargs.get("session_id") or "default"
    rec = _find_recorder(ctx, session_id)
    if rec is not None:
        rec.note_tool(tool_name, args, result)
    # Always buffer calls so /proof learn can draft retroactively.
    buf = _session_buffer(ctx)
    buf.append({"tool": tool_name, "args": dict(args or {}), "result": result, "session": session_id})
    if len(buf) > _SESSION_BUFFER_MAX:
        del buf[: len(buf) - _SESSION_BUFFER_MAX]
    # F2: cache the result, parsing JSON strings into dicts.
    parsed = core._parse_result(result)
    if isinstance(parsed, dict) and session_id:
        cache = _runtime_cache(ctx)
        cache.setdefault(session_id, {})
        cache[session_id][tool_name] = parsed
    return None


def _on_pre_tool_call(ctx, tool_name, args, **kwargs):
    session_id = kwargs.get("session_id") or "default"
    rec = _find_recorder(ctx, session_id)
    # Note: pre_tool_call has no result yet. The actual recording happens
    # in post_tool_call with the real result. Don't note here to avoid
    # polluting the recorder with None results that trigger error detection.
    if rec is not None:
        return None
    # Enforcement: if any bound contract covers this mutating tool, evaluate it.
    contracts_dir = _contracts_root(ctx) / "contracts"
    if not contracts_dir.exists():
        return None
    # B5: use the current session's runtime cache, not a global one.
    all_runtime = _runtime_cache(ctx)
    runtime = all_runtime.get(session_id, {})
    for path in sorted(contracts_dir.glob("*.json")):
        try:
            contract = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not contract.get("bound"):
            continue
        if tool_name not in contract.get("action_window", {}).get("mutating_tools", []):
            continue
        verdict = core.check_contract(contract, runtime=runtime, proposed_call={"tool": tool_name, "args": args})
        if verdict and verdict["action"] == "block":
            return verdict
    return None


def _load_contract(ctx, name: str) -> dict | None:
    path = _contracts_root(ctx) / "contracts" / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _bind_contract(ctx, contract: dict) -> None:
    """Save the contract as bound."""
    contract["bound"] = True
    path = _contracts_root(ctx) / "contracts" / f"{contract['contract_id']}.json"
    path.write_text(json.dumps(contract, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _handle(ctx, raw_args: str, session_id: str = "default") -> str:
    args = (raw_args or "").strip().split()
    if not args:
        return "Usage: /proof start <name> | stop | learn <name> [last N] | status | unbind <name>"
    cmd = args[0]
    if cmd == "start" and len(args) > 1:
        rec = core.Recorder(root=_contracts_root(ctx))
        rec.start(args[1], intent=" ".join(args[2:]) or args[1])
        # F3: key the recorder by the current session
        _active_recorders(ctx)[session_id] = rec
        return f"Recording '{args[1]}' — do the task once, then /proof stop."
    if cmd == "stop":
        # F3: pop from any session's recorder
        recorders = _active_recorders(ctx)
        rec = recorders.pop(session_id, None)
        if rec is None and len(recorders) == 1:
            rec = recorders.pop(next(iter(recorders)))
        if rec is None:
            return "No active recording."
        try:
            contract = rec.stop(success=True)
        except core.ContractError as exc:
            return f"Contract not created: {exc}"
        _bind_contract(ctx, contract)
        return (f"Contract '{contract['contract_id']}' is live (v{contract['version']}).\n"
                f"Gating: {', '.join(contract['action_window']['mutating_tools'])}\n"
                f"Contract JSON: {_contracts_root(ctx)}/contracts/{contract['contract_id']}.json")
    if cmd == "learn" and len(args) > 1:
        name = args[1]
        last_n = int(args[2]) if len(args) > 2 and args[2].isdigit() else None
        # F3: only use calls from the current session
        buf = _session_buffer(ctx)
        session_calls = [c for c in buf if c.get("session", "default") == session_id]
        if not session_calls:
            return "No tool calls in this session. Run a task first, then /proof learn <name>."
        calls = session_calls[-last_n:] if last_n else session_calls
        rec = core.Recorder(root=_contracts_root(ctx))
        rec.start(name, intent=f"Learned from session ({len(calls)} calls)")
        for call in calls:
            rec.note_tool(call["tool"], call["args"], call["result"])
        try:
            contract = rec.stop(success=True)
        except core.ContractError as exc:
            return f"Contract not created: {exc}"
        _bind_contract(ctx, contract)
        return (f"Contract '{name}' is live (v{contract['version']}), learned from {len(calls)} calls.\n"
                f"Gating: {', '.join(contract['action_window']['mutating_tools'])}")
    if cmd == "unbind" and len(args) > 1:
        contract = _load_contract(ctx, args[1])
        if not contract:
            return f"No contract named {args[1]}."
        contract["bound"] = False
        path = _contracts_root(ctx) / "contracts" / f"{args[1]}.json"
        path.write_text(json.dumps(contract, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return f"Contract {args[1]} unbound (no longer gating)."
    if cmd == "status":
        root = _contracts_root(ctx) / "contracts"
        if not root.exists():
            return "No contracts."
        lines = []
        for path in sorted(root.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            lines.append(f"{data['contract_id']} v{data['version']} bound={data.get('bound', False)}")
        return "\n".join(lines) or "No contracts."
    if cmd == "rebind" and len(args) > 1:
        rec = core.Recorder(root=_contracts_root(ctx))
        rec.start(args[1], intent=args[1])
        _active_recorders(ctx)[session_id] = rec
        return f"Rebinding '{args[1]}' — do the corrected task once, then /proof stop."
    return "Usage: /proof start <name> | stop | learn <name> [last N] | status | unbind <name>"


def register(ctx):
    ctx.register_command(
        name="proof",
        handler=lambda raw, session_id="default": _handle(ctx, raw, session_id),
        description="Record a successful run into a live execution contract",
        args_hint="start <name> | stop | learn <name> [last N] | status | unbind <name>",
    )
    ctx.register_hook("pre_tool_call", lambda **kw: _on_pre_tool_call(ctx, **kw))
    ctx.register_hook("post_tool_call", lambda **kw: _on_post_tool_call(ctx, **kw))
