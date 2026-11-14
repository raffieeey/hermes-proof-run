"""Proof Run: operator-controlled capture and a fail-closed action gate.

The plugin does not replay a workflow, call a model, or infer safe parameter
ranges. It checks demonstrated actions and observations at Hermes' tool boundary.
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from pathlib import Path
import shlex
import threading
import time

from plugins.plugin_storage import plugin_data_dir

from .proof_run import commands, core, guided, hostport
from .proof_run.store import WorkflowStore

_BUFFER_MAX = 100
_PENDING_MAX = 256
_FACT_MAX_AGE = 60.0
_USAGE = (
    "Usage: /proof start <name> [intent] | stop | cancel | "
    "learn <name> [N] | rebind <name> | status | unbind <name> | "
    "capture start <name> [intent] | capture finish | capture cancel | "
    "prepare <name> [json] | inspect <name> | promote <name> <revision> | "
    "run <name> [json] | runs | show <run-id> | cancel-run <run-id> | resume <run-id> | "
    "save [name=value ...] | pin <name> <tool>.<field> | unpin <name> <tool>.<field>"
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
    state = _state(ctx)
    session = _sid(kwargs.get("session_id"))
    args = args or {}
    with state["lock"]:
        run = state["runs"].get(session)
        if run is not None:
            try:
                verdict = guided.evaluate_call(
                    run,
                    tool_name=tool_name,
                    args=args,
                    tool_call_id=kwargs.get("tool_call_id"),
                )
            except (core.ContractError, ValueError, TypeError, KeyError):
                return _block("guided run state is unreadable; use /proof cancel")
            if verdict is not None and verdict.get("action") == "block":
                return _block(
                    str(verdict.get("message") or "guided run blocked this call")
                )
        # Checked workflow authorizers take over legacy guard consumption in this
        # phase, but guided-run procedure checks must still apply.
        if hostport.transform_deferral_active(ctx, kwargs.get("execution_phase")):
            return None
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
        run = state["runs"].get(session)
        if run is not None:
            try:
                guided.note_result(
                    run,
                    tool_name=tool_name,
                    args=args,
                    result=result,
                    metadata=metadata,
                    tool_call_id=kwargs.get("tool_call_id"),
                )
            except (core.ContractError, ValueError, TypeError, KeyError):
                # An observer error must not leave a run free to continue unchecked.
                run["state"] = "stopped"
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
    for key in ("runtime", "buffers", "recorders", "runs"):
        state[key].pop(session, None)
    for key in [k for k in state["pending"] if k[0] == session]:
        del state["pending"][key]


def _on_pre_llm_call(ctx, **kwargs):
    state = _state(ctx)
    session = _sid(kwargs.get("session_id"))
    with state["lock"]:
        run = state["runs"].get(session)
        if run is None:
            return None
        try:
            text, keep = guided.notice(run)
        except (core.ContractError, ValueError, TypeError, KeyError):
            state["runs"].pop(session, None)
            return None
        if not keep:
            state["runs"].pop(session, None)
    return {"context": text} if text else None


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
    # A recording supersedes any guided run in this session.
    state["runs"].pop(session, None)
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


def _guided_contract(ctx, name):
    """A saved contract that carries a procedure block, or None."""
    if not name:
        return None
    try:
        contract = core.load_contract(_contracts_root(ctx), name)
    except (core.ContractError, OSError, ValueError, TypeError) as exc:
        raise core.ContractError(f"contract {name} is unreadable or damaged") from exc
    if not isinstance(contract, dict) or not isinstance(contract.get("procedure"), dict):
        return None
    return contract


def _guided_save(ctx, state, session, rest):
    recording = state["recorders"].get(session)
    if recording is None:
        return (
            "Procedure not saved: no active recording in this session. "
            "Use /proof start <name> first."
        )
    try:
        tokens = shlex.split(rest)
    except ValueError as exc:
        return f"Procedure not saved: the input list could not be parsed ({exc})."
    # Validate before anything is written: a refused save keeps the recording.
    try:
        pairs = guided.parse_inputs(tokens)
        snapshot = recording["recorder"].snapshot()
        procedure = guided.build_procedure(snapshot, pairs)
    except (guided.GuidedError, core.ContractError) as exc:
        return f"Procedure not saved: {exc}"
    try:
        contract = recording["recorder"].stop(
            success=True, bound=False, extra_fields={"procedure": procedure}
        )
    except (core.ContractError, OSError, ValueError, TypeError) as exc:
        # Recorder.stop retains its snapshot when the durable write fails, so the
        # operator can correct a transient problem and retry /proof save.
        return f"Procedure not saved: {exc}"
    state["recorders"].pop(session, None)
    state["runtime"].pop(session, None)
    state["buffers"].pop(session, None)
    inputs = [item["name"] for item in procedure["inputs"]]
    detail = f"Inputs: {', '.join(inputs)}." if inputs else "No inputs."
    hint = f"/proof run {contract['contract_id']}"
    if inputs:
        hint += " " + " ".join(f"{name}=<value>" for name in inputs)
    return (
        f"Procedure '{contract['contract_id']}' saved (v{contract['version']}, unbound). "
        f"{detail} Start it with {hint}. Nothing is gated outside a run."
    )


def _guided_run(ctx, state, session, rest, contract):
    name = contract["contract_id"]
    if session in state["recorders"]:
        return (
            "Run not started: this session is recording. "
            "Use /proof stop or /proof cancel first."
        )
    active = state["runs"].get(session)
    if active is not None and active.get("state") == "completed":
        state["runs"].pop(session, None)
        active = None
    if active is not None:
        return (
            f"Run not started: a run of '{active['name']}' is already active in this "
            "session. Use /proof cancel to end it."
        )
    if contract.get("bound"):
        return (
            f"Run not started: contract {name} is bound as a strict guard. "
            f"Use /proof unbind {name} first."
        )
    try:
        tokens = shlex.split(rest)
    except ValueError as exc:
        return f"Run not started: the inputs could not be parsed ({exc})."
    try:
        pairs = guided.parse_inputs(tokens[1:])
        run = guided.new_run(
            name, contract["version"], contract["procedure"], dict(pairs)
        )
    except (guided.GuidedError, core.ContractError) as exc:
        return f"Run not started: {exc}"
    state["runs"][session] = run
    bound = ", ".join(
        f"{key}={value}" for key, value in run["plan"]["inputs"].items()
    )
    return (
        f"Run started: '{name}' step 1 of {guided.step_count(run)}"
        + (f" with {bound}" if bound else "")
        + ". The next message carries the guide; every action call is checked."
    )


def _guided_pin(ctx, rest, pin):
    prefix = "Not pinned"
    try:
        tokens = shlex.split(rest) if rest else []
    except ValueError as exc:
        return f"{prefix}: the command arguments could not be parsed ({exc})."
    if len(tokens) != 2:
        verb = "pin" if pin else "unpin"
        return f"{prefix}: use /proof {verb} <name> <tool>.<field>."
    name, target = tokens
    try:
        contract = core.load_contract(_contracts_root(ctx), name)
    except (core.ContractError, OSError, ValueError, TypeError) as exc:
        return f"{prefix}: {exc}"
    if contract is None:
        return f"{prefix}: no contract named {name}."
    if not isinstance(contract.get("procedure"), dict):
        return f"{prefix}: {name} is not a guided procedure."
    try:
        guided.validate_procedure(contract["procedure"])
    except guided.GuidedError as exc:
        return f"{prefix}: {exc}"
    try:
        if pin:
            guided.pin_field(contract, target)
        else:
            guided.unpin_field(contract, target)
        core.save_contract(_contracts_root(ctx), contract, new_version=False)
    except (guided.GuidedError, core.ContractError, OSError, ValueError, TypeError) as exc:
        return f"{prefix}: {exc}"
    state = "pinned" if pin else "unpinned"
    return (
        f"Fact {target} is {state} for procedure '{name}'. It is required before "
        "each step that recorded it."
    )


def _guided_dispatch(ctx, raw_args, session):
    """Handle the guided verbs; None falls through to the existing handlers."""
    text = (raw_args or "").strip()
    if not text:
        return None
    verb, _, rest = text.partition(" ")
    verb = verb.strip()
    if verb not in ("save", "pin", "unpin", "run", "inspect"):
        return None
    rest = rest.strip()
    contract = None
    if verb in ("run", "inspect"):
        name = rest.partition(" ")[0].strip()
        try:
            contract = _guided_contract(ctx, name)
        except (core.ContractError, OSError, ValueError, TypeError) as exc:
            if verb == "run":
                return f"Run not started: {exc}"
            return f"Procedure unavailable: {exc}"
        if contract is None:
            return None
    state = _state(ctx)
    with state["lock"]:
        if verb == "save":
            return _guided_save(ctx, state, session, rest)
        if verb == "run":
            return _guided_run(ctx, state, session, rest, contract)
        if verb == "inspect":
            if contract is None:
                return None
            try:
                return guided.inspect_text(contract)
            except (guided.GuidedError, core.ContractError) as exc:
                return f"Procedure unavailable: {exc}"
        return _guided_pin(ctx, rest, pin=verb == "pin")


def _handle(ctx, raw_args: str, session_id: str = "default", session_key=None, platform=None, source=None) -> str:
    session = _sid(session_id)
    guided_reply = _guided_dispatch(ctx, raw_args, session)
    if guided_reply is not None:
        return guided_reply
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
                lines = []
                for c in contracts:
                    line = (
                        f"{c['contract_id']} v{c['version']} bound={c.get('bound', False)} "
                        f"evidence={c.get('evidence_status', 'legacy')}"
                    )
                    if isinstance(c.get("procedure"), dict):
                        line += " procedure"
                    lines.append(line)
                run = state["runs"].get(session)
                if run is not None:
                    lines.append(guided.status_text(run))
                return "\n".join(lines) or "No contracts."
            return _USAGE
        except (core.ContractError, OSError, ValueError, TypeError, KeyError) as exc:
            return f"Contract not created or changed: {exc}"


def register(ctx):
    ctx._proof_run_state = {
        "lock": threading.RLock(),
        "recorders": {},
        "runs": {},
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
        description="Capture a task, save a guided procedure or strict guard, and reuse it on standard Hermes",
        args_hint="start <name> | save [name=value ...] | run <name> [name=value ...] | inspect <name> | status",
    )
    ctx.register_hook("pre_tool_call", lambda **kw: _on_pre_tool_call(ctx, **kw))
    ctx.register_hook("post_tool_call", lambda **kw: _on_post_tool_call(ctx, **kw))
    ctx.register_hook("pre_llm_call", lambda **kw: _on_pre_llm_call(ctx, **kw))
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
