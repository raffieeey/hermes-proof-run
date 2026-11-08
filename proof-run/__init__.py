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


def _contracts_root(ctx) -> Path:
    return Path(ctx.plugin_data_dir("proof-run"))


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


def _on_pre_tool_call(ctx, tool_name, args, **kwargs):
    session_id = kwargs.get("session_id") or "default"
    rec = _find_recorder(ctx, session_id)
    if rec is not None:
        rec.note_tool(tool_name, args, None)
        return None
    # Enforcement: if any bound contract covers this mutating tool, evaluate it.
    contracts_dir = _contracts_root(ctx) / "contracts"
    if not contracts_dir.exists():
        return None
    for path in sorted(contracts_dir.glob("*.json")):
        try:
            contract = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not contract.get("bound"):
            continue
        if tool_name not in contract.get("action_window", {}).get("mutating_tools", []):
            continue
        runtime = kwargs.get("runtime") or {}
        verdict = core.check_contract(contract, runtime=runtime, proposed_call={"tool": tool_name, "args": args})
        if verdict and verdict["action"] == "block":
            return verdict
    return None


def _on_post_tool_call(ctx, tool_name, args, result, **kwargs):
    session_id = kwargs.get("session_id") or "default"
    rec = _find_recorder(ctx, session_id)
    if rec is None:
        return None
    rec.note_tool(tool_name, args, result)
    return None


def _load_contract(ctx, name: str) -> dict | None:
    path = _contracts_root(ctx) / "contracts" / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _handle(ctx, raw_args: str) -> str:
    args = (raw_args or "").strip().split()
    if not args:
        return "Usage: /proof start <name> | stop | bind <name> | status | rebind <name>"
    cmd = args[0]
    if cmd == "start" and len(args) > 1:
        rec = core.Recorder(root=_contracts_root(ctx))
        rec.start(args[1], intent=" ".join(args[2:]) or args[1])
        _active_recorders(ctx)["default"] = rec
        return f"Recording '{args[1]}' — do the task once, then /proof stop."
    if cmd == "stop":
        rec = _active_recorders(ctx).pop("default", None)
        if rec is None:
            return "No active recording."
        try:
            contract = rec.stop(success=True)
        except core.ContractError as exc:
            return f"Contract not created: {exc}"
        return (f"Contract drafted for '{contract['contract_id']}'.\n"
                f"Mutating tools: {', '.join(contract['action_window']['mutating_tools'])}\n"
                f"Review the JSON at {_contracts_root(ctx)}/contracts/{contract['contract_id']}.json, then /proof bind {contract['contract_id']}.")
    if cmd == "bind" and len(args) > 1:
        contract = _load_contract(ctx, args[1])
        if not contract:
            return f"No contract named {args[1]}."
        contract["bound"] = True
        path = _contracts_root(ctx) / "contracts" / f"{args[1]}.json"
        path.write_text(json.dumps(contract, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return f"Contract {args[1]} bound."
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
        _active_recorders(ctx)["default"] = rec
        return f"Rebinding '{args[1]}' — do the corrected task once, then /proof stop."
    return "Usage: /proof start <name> | stop | bind <name> | status | rebind <name>"


def register(ctx):
    ctx.register_command(
        name="proof",
        handler=lambda raw: _handle(ctx, raw),
        description="Record a successful run into a live execution contract",
        args_hint="start <name> | stop | bind <name> | status | rebind <name>",
    )
    ctx.register_hook("pre_tool_call", lambda **kw: _on_pre_tool_call(ctx, **kw))
    ctx.register_hook("post_tool_call", lambda **kw: _on_post_tool_call(ctx, **kw))
