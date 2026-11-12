"""Proof Run: operator-controlled capture and a fail-closed action gate.

The plugin does not replay a workflow, call a model, or infer safe parameter
ranges. It checks demonstrated actions and observations at Hermes' tool boundary.
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from pathlib import Path
import threading
import time

from plugins.plugin_storage import plugin_data_dir

from .proof_run import commands, core, hostport
from .proof_run.store import WorkflowStore

_BUFFER_MAX = 100
_PENDING_MAX = 256
_FACT_MAX_AGE = 60.0
_USAGE = (
    "Usage: /proof start <name> [intent] | stop | cancel | "
    "learn <name> [N] | rebind <name> | status | unbind <name> | "
    "capture start <name> [intent] | capture finish | capture cancel | "
    "prepare <name> [json] | inspect <name> | promote <name> <revision> | "
    "run <name> [json] | runs | show <run-id> | cancel-run <run-id> | resume <run-id>"
)


def _contracts_root(ctx) -> Path:
    return plugin_data_dir("proof-run")


def _state(ctx):
    return ctx._proof_run_state


def _sid(session_id):
    return session_id or "default"


def _metadata(kwargs):
    return {k: kwargs.get(k) for k in ("status", "error_type", "error_message")}


def _block(message):
    return {"action": "block", "message": f"Proof Run: {message}"}


def _on_pre_tool_call(ctx, tool_name, args, **kwargs):
    if hostport.transform_deferral_active(ctx, kwargs.get("execution_phase")):
        return None
    state = _state(ctx)
    session = _sid(kwargs.get("session_id"))
    args = args or {}
    with state["lock"]:
        cache = state["runtime"].setdefault(session, {})
        if tool_name in core.READ_ONLY_TOOLS:
            # A failed/in-flight refresh must not leave an older healthy value.
            cache.pop(core.fact_key(tool_name, args), None)
            return None
        try:
            contracts = core.list_contracts(_contracts_root(ctx))
            now = time.monotonic()
            runtime = {
                key: value["result"]
                for key, value in cache.items()
                if now - value["observed_at"] <= _FACT_MAX_AGE
            }
            recording = state["recorders"].get(session)
            checks = []
            for contract in contracts:
                if not contract.get("bound"):
                    continue
                # Re-demonstration is an explicit operator exemption for THIS
                # contract in THIS session, never for every contract/session.
                if recording and contract.get("contract_id") == recording["name"]:
                    continue
                if contract.get("contract_id") in state["unproven"]:
                    return _block(
                        "replay evidence could not be persisted; rebind is required"
                    )
                if contract.get("schema_version") != 2:
                    return _block("legacy contract requires /proof rebind before reuse")
                verdict = core.check_contract(
                    contract,
                    runtime=runtime,
                    proposed_call={"tool": tool_name, "args": args},
                )
                if verdict is None:
                    continue
                if verdict["action"] == "block":
                    return verdict
                checks.append(
                    {
                        "name": contract["contract_id"],
                        "version": contract["version"],
                        "step": deepcopy(contract["steps"][verdict["step_index"]]),
                    }
                )
            call_id = kwargs.get("tool_call_id")
            if checks:
                if not call_id:
                    return _block("a guarded action requires a host tool_call_id")
                if len(state["pending"]) >= _PENDING_MAX:
                    return _block(
                        "too many unfinished guarded actions; restart the session"
                    )
                state["pending"][(session, call_id)] = checks
            # Consume before dispatch, not after: simultaneous mutations cannot
            # both spend one observation. Any mutation can invalidate read facts.
            cache.clear()
            return None
        except (core.ContractError, OSError, ValueError, TypeError, KeyError):
            return _block("contract is unreadable or invalid; repair or re-record it")


def _on_post_tool_call(ctx, tool_name, args, result, **kwargs):
    state = _state(ctx)
    session = _sid(kwargs.get("session_id"))
    args = deepcopy(args or {})
    result = deepcopy(result)
    metadata = _metadata(kwargs)
    with state["lock"]:
        recording = state["recorders"].get(session)
        if recording:
            try:
                recording["recorder"].note_tool(tool_name, args, result, **metadata)
            except core.ContractError:
                # Preserve the failed capture without letting one observer error
                # bypass pending replay-outcome handling for other contracts.
                recording["rejected"] = True
        buffer = state["buffers"].setdefault(session, deque(maxlen=_BUFFER_MAX))
        buffer.append(
            {"tool": tool_name, "args": args, "result": result, "metadata": metadata}
        )
        if tool_name in core.READ_ONLY_TOOLS:
            cache = state["runtime"].setdefault(session, {})
            key = core.fact_key(tool_name, args)
            cache.pop(key, None)
            parsed = core._parse_result(result)
            if isinstance(parsed, dict) and not core._is_error_result(
                result, **metadata
            ):
                cache[key] = {"result": parsed, "observed_at": time.monotonic()}
        checks = state["pending"].pop((session, kwargs.get("tool_call_id")), [])
        for check in checks:
            passed = core.check_evidence(check["step"], result, **metadata)
            updates: dict[str, object] = {"last_check": {"verified": passed}}
            if not passed:
                updates["evidence_status"] = "unproven"
            try:
                core.update_contract_state(
                    _contracts_root(ctx),
                    check["name"],
                    expected_version=check["version"],
                    **updates,
                )
            except (core.ContractError, OSError):
                # Observer hooks cannot veto the completed call. Retain a local
                # stop as well; a malformed durable file also blocks the gate.
                state["unproven"].add(check["name"])
    return None


def _clear_session(state, session):
    for key in ("runtime", "buffers", "recorders"):
        state[key].pop(session, None)
    for key in [k for k in state["pending"] if k[0] == session]:
        del state["pending"][key]


def _on_session_finalize(ctx, session_id=None, **kwargs):
    state = _state(ctx)
    with state["lock"]:
        _clear_session(state, _sid(session_id))


def _begin(ctx, state, args, session, rebind):
    name = core._validate_contract_name(args[1])
    if session in state["recorders"]:
        return "Already recording in this session. Use /proof stop or /proof cancel."
    prior = core.load_contract(_contracts_root(ctx), name)
    if prior and not rebind:
        return f"Contract {name} already exists. Use /proof rebind {name}."
    if not prior and rebind:
        return f"No contract named {name}. Use /proof start {name}."
    recorder = core.Recorder(root=_contracts_root(ctx))
    recorder.start(name, intent=" ".join(args[2:]) or name)
    state["recorders"][session] = {"name": name, "recorder": recorder}
    state["runtime"].pop(session, None)
    state["buffers"].pop(session, None)
    verb = "Rebinding" if rebind else "Recording"
    return f"{verb} '{name}'. Do the task once, then /proof stop."


def _finish(ctx, state, session):
    recording = state["recorders"].pop(session, None)
    if recording is None:
        return "No active recording in this session."
    try:
        if recording.get("rejected"):
            raise core.ContractError("recording contained unsafe or invalid data")
        contract = recording["recorder"].stop(success=True, bound=True)
    finally:
        state["runtime"].pop(session, None)
        state["buffers"].pop(session, None)
    state["unproven"].discard(contract["contract_id"])
    return (
        f"Contract '{contract['contract_id']}' is live (v{contract['version']}).\n"
        f"Gating: {', '.join(contract['action_window']['mutating_tools'])}\n"
        "Exact demonstrated arguments only. Fresh matching reads are required again."
    )


def _learn(ctx, state, args, session):
    name = core._validate_contract_name(args[1])
    if session in state["recorders"]:
        return "Already recording. Use /proof stop or /proof cancel first."
    if core.load_contract(_contracts_root(ctx), name):
        return f"Contract {name} already exists. Use /proof rebind {name}."
    if len(args) == 3:
        if not args[2].isdigit() or not 1 <= int(args[2]) <= _BUFFER_MAX:
            return f"N must be between 1 and {_BUFFER_MAX}."
        count = int(args[2])
    else:
        count = _BUFFER_MAX
    calls = list(state["buffers"].get(session, []))[-count:]
    if not calls:
        return "No tool calls in this session. Run a task first."
    recorder = core.Recorder(root=_contracts_root(ctx))
    recorder.start(name, intent=f"Operator capture of {len(calls)} recent calls")
    for call in calls:
        recorder.note_tool(
            call["tool"], call["args"], call["result"], **call["metadata"]
        )
    contract = recorder.stop(success=True, bound=True)
    state["runtime"].pop(session, None)
    state["buffers"].pop(session, None)
    return f"Contract '{name}' is live (v{contract['version']}), learned from {len(calls)} calls."


def _handle(ctx, raw_args: str, session_id: str = "default", session_key=None, platform=None, source=None) -> str:
    new = commands.handle(
        ctx,
        raw_args,
        session_id=session_id,
        session_key=session_key,
        platform=platform,
        source=source,
    )
    if new is not None:
        return new
    args = (raw_args or "").strip().split()
    if not args:
        return _USAGE
    session = _sid(session_id)
    state = _state(ctx)
    with state["lock"]:
        try:
            cmd = args[0]
            if cmd in ("start", "rebind") and len(args) >= 2:
                return _begin(ctx, state, args, session, cmd == "rebind")
            if cmd == "stop" and len(args) == 1:
                return _finish(ctx, state, session)
            if cmd == "cancel" and len(args) == 1:
                _clear_session(state, session)
                return "Recording cancelled in this session. Existing contracts are unchanged."
            if cmd == "learn" and 2 <= len(args) <= 3:
                return _learn(ctx, state, args, session)
            if cmd == "unbind" and len(args) == 2:
                name = core._validate_contract_name(args[1])
                contract = core.update_contract_state(
                    _contracts_root(ctx), name, bound=False
                )
                if contract is None:
                    return f"No contract named {name}."
                return f"Contract {name} unbound (no longer gating)."
            if cmd == "status" and len(args) == 1:
                contracts = core.list_contracts(_contracts_root(ctx))
                return (
                    "\n".join(
                        f"{c['contract_id']} v{c['version']} bound={c.get('bound', False)} "
                        f"evidence={c.get('evidence_status', 'legacy')}"
                        for c in contracts
                    )
                    or "No contracts."
                )
            return _USAGE
        except (core.ContractError, OSError, ValueError, TypeError, KeyError) as exc:
            return f"Contract not created or changed: {exc}"


def register(ctx):
    ctx._proof_run_state = {
        "lock": threading.RLock(),
        "recorders": {},
        "runtime": {},
        "buffers": {},
        "pending": {},
        "unproven": set(),
        "checked_frames": {},
        "authorizer_guard": "unattempted",
    }
    ctx.register_command(
        name="proof",
        handler=lambda raw, session_id="default", session_key=None, platform=None, source=None: _handle(
            ctx,
            raw,
            session_id=session_id,
            session_key=session_key,
            platform=platform,
            source=source,
        ),
        description="Capture demonstrated actions, review a workflow, and run it with checked execution",
        args_hint="start <name> | stop | capture start <name> | prepare <name> | run <name> [json] | status | unbind <name>",
    )
    ctx.register_hook("pre_tool_call", lambda **kw: _on_pre_tool_call(ctx, **kw))
    ctx.register_hook("post_tool_call", lambda **kw: _on_post_tool_call(ctx, **kw))
    ctx.register_hook(
        "on_session_finalize", lambda **kw: _on_session_finalize(ctx, **kw)
    )
    try:
        root = _contracts_root(ctx)
        store = WorkflowStore(root)
        if hasattr(ctx, "register_tool_authorizer"):
            ctx.register_tool_authorizer(hostport.make_legacy_authorizer(ctx, store))
            ctx._proof_run_state["authorizer_guard"] = "installed"
        if hasattr(ctx, "register_execution_receipt_sink"):
            ctx.register_execution_receipt_sink(hostport.make_receipt_sink(ctx, store))
    except Exception:
        if ctx._proof_run_state.get("authorizer_guard") != "installed":
            ctx._proof_run_state["authorizer_guard"] = "failed"
