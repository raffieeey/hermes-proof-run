"""Proof Run: operator-controlled capture and a fail-closed action gate.

The plugin does not replay a workflow, call a model, or infer safe parameter
ranges. It checks demonstrated actions and observations at Hermes' tool boundary.
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
import hashlib
import json
import math
import os
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
_GUIDED_MARKER_SCHEMA = 1
_GUIDED_MARKER_MAX_AGE = 24 * 60 * 60
_GUIDED_MARKER_MAX_BYTES = 64 * 1024
_GUIDED_MARKER_MAX_REASONS = 16
_GUIDED_MARKER_DIR = "guided-runs"
_GUIDED_MARKER_PREFIX = "run-"
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


def _marker_filename(session):
    digest = hashlib.sha256(str(session).encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"{_GUIDED_MARKER_PREFIX}{digest}.json"


def _open_guided_marker_dir(root, create):
    """Open the marker directory beneath the existing contracts root."""
    with core._contracts_fd(root, create) as contracts_fd:
        if contracts_fd is None:
            return None
        marker_fd = core._open_dir(contracts_fd, _GUIDED_MARKER_DIR, create)
        if marker_fd is not None and create:
            try:
                os.fchmod(marker_fd, 0o700)
            except OSError:
                os.close(marker_fd)
                raise
        return marker_fd


def _write_run_marker(root, session, run):
    steps = (run.get("plan") or {}).get("steps") or []
    action_tools = [step["tool"] for step in steps]
    marker = {
        "schema_version": _GUIDED_MARKER_SCHEMA,
        "procedure_name": run["name"],
        "contract_version": run["version"],
        "session_id": session,
        "step_index": run["index"],
        "action_tools": action_tools,
        "updated_at": time.time(),
    }
    payload = (json.dumps(marker, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    directory = _open_guided_marker_dir(root, True)
    if directory is None:
        raise OSError("guided run marker directory is unavailable")
    try:
        core._atomic_write(directory, _marker_filename(session), payload)
    finally:
        os.close(directory)


def _delete_run_marker(root, session):
    directory = _open_guided_marker_dir(root, False)
    if directory is None:
        return
    try:
        try:
            os.unlink(_marker_filename(session), dir_fd=directory)
        except FileNotFoundError:
            return
    finally:
        os.close(directory)


class _MarkerHold(ValueError):
    """A syntactically valid marker the loader cannot re-arm and must not discard.

    ``session`` is set only when the marker names its session readably; without it
    the failure holds every session.
    """

    def __init__(self, message, session=None):
        super().__init__(message)
        self.session = session


def _marker_session(marker):
    session = marker.get("session_id")
    if isinstance(session, str) and session and len(session) <= 1024:
        return session
    return None


def _shown(value):
    text = repr(value)
    return text if len(text) <= 24 else text[:21] + "..."


def _why(exc):
    return str(getattr(exc, "strerror", None) or exc or type(exc).__name__)[:100]


def _validate_run_marker(marker, filename):
    if not isinstance(marker, dict):
        raise _MarkerHold("marker is not an object")
    # A hold names the marker's own session whenever that session is readable, so a
    # bad field costs one session. Without a readable session, or when the filename
    # contradicts it, nothing says whose marker this is and every session is held.
    session = _marker_session(marker)
    version_field = marker.get("schema_version")
    if type(version_field) is not int or version_field != _GUIDED_MARKER_SCHEMA:
        # A newer plugin version may own this marker: hold its session, keep the file.
        raise _MarkerHold(
            f"unsupported marker schema version {_shown(version_field)}", session
        )
    if session is None:
        raise _MarkerHold("invalid session id")
    if filename != _marker_filename(session):
        raise _MarkerHold("marker filename does not match session")
    raw_name = marker.get("procedure_name")
    if not isinstance(raw_name, str):
        raise _MarkerHold("invalid procedure name", session)
    try:
        name = core._validate_contract_name(raw_name)
    except core.ContractError:
        raise _MarkerHold("invalid procedure name", session) from None
    version = marker.get("contract_version")
    if type(version) is not int or version < 1:
        raise _MarkerHold("invalid contract version", session)
    index = marker.get("step_index")
    if type(index) is not int or index < 0:
        raise _MarkerHold("invalid step index", session)
    tools = marker.get("action_tools")
    if not isinstance(tools, list) or not tools or len(tools) > 256:
        raise _MarkerHold("invalid action tool list", session)
    if any(
        not isinstance(tool, str)
        or not tool.strip()
        or len(tool) > 256
        or "\x00" in tool
        or tool in core.READ_ONLY_TOOLS
        for tool in tools
    ):
        raise _MarkerHold("invalid action tool name", session)
    if index >= len(tools):
        raise _MarkerHold("step index is outside the action sequence", session)
    updated_at = marker.get("updated_at")
    if isinstance(updated_at, bool) or not isinstance(updated_at, (int, float)):
        raise _MarkerHold("invalid marker timestamp", session)
    updated_at = float(updated_at)
    if not math.isfinite(updated_at):
        raise _MarkerHold("invalid marker timestamp", session)
    return {
        "name": name,
        "version": version,
        "session_id": session,
        "index": index,
        "tools": tools,
        "updated_at": updated_at,
    }


def _drop_run_marker(directory, filename):
    try:
        os.unlink(filename, dir_fd=directory)
    except OSError:
        pass


def _marker_step_index(run):
    """Clamp a run's step index to a readable plan; 0 when the plan is unreadable."""
    plan = run.get("plan") if isinstance(run, dict) else None
    steps = plan.get("steps") if isinstance(plan, dict) else None
    last = len(steps) - 1 if isinstance(steps, list) and steps else 0
    index = run.get("index") if isinstance(run, dict) else 0
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        index = 0
    return min(index, last)


def _hold_session(state, session, reason):
    state.setdefault("held_sessions", {})[session] = reason


def _hold_everything(state, reason):
    reasons = state.get("global_hold") or []
    if reason not in reasons and len(reasons) < _GUIDED_MARKER_MAX_REASONS:
        reasons.append(reason)
    state["global_hold"] = reasons


def _marker_hold_message(state, session):
    """Why effectful tools are held for this session by a marker failure, or None."""
    reasons = state.get("global_hold")
    if reasons:
        return (
            "guided run markers could not be read or validated "
            f"({'; '.join(reasons[:3])}), so no guided run can be trusted. "
            "Every effectful tool is blocked in every session. "
            "Use /proof cancel to release them."
        )
    reason = (state.get("held_sessions") or {}).get(session)
    if reason:
        return (
            f"a guided run marker for this session cannot be re-armed ({reason}) "
            "and is kept untouched. Every effectful tool is blocked in this session. "
            "Use /proof cancel to release them."
        )
    return None


def _scan_run_markers(root, state):
    try:
        directory = _open_guided_marker_dir(root, False)
    except Exception as exc:
        _hold_everything(state, f"the marker directory cannot be opened ({_why(exc)})")
        return
    if directory is None:
        return
    try:
        try:
            filenames = os.listdir(directory)
        except OSError as exc:
            _hold_everything(state, f"the marker directory cannot be listed ({_why(exc)})")
            return
        now = time.time()
        for filename in filenames:
            if not (
                filename.startswith(_GUIDED_MARKER_PREFIX)
                and filename.endswith(".json")
            ):
                continue
            label = f"marker {filename[:12]}..."
            try:
                info = os.stat(filename, dir_fd=directory, follow_symlinks=False)
                if info.st_size > _GUIDED_MARKER_MAX_BYTES:
                    _hold_everything(
                        state, f"{label} is larger than {_GUIDED_MARKER_MAX_BYTES} bytes"
                    )
                    continue
                raw = core._read_bytes(directory, filename)
            except FileNotFoundError:
                continue
            except Exception as exc:
                _hold_everything(state, f"{label} cannot be read ({_why(exc)})")
                continue
            if raw is None:
                continue
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError, RecursionError):
                # Not JSON at all: nothing says which tools a run covered, so the
                # file is kept as evidence and every session is held.
                _hold_everything(state, f"{label} is not valid JSON")
                continue
            try:
                marker = _validate_run_marker(data, filename)
            except _MarkerHold as exc:
                if exc.session is not None:
                    _hold_session(state, exc.session, str(exc))
                else:
                    _hold_everything(state, f"{label} cannot be validated ({exc})")
                continue
            except Exception as exc:
                _hold_everything(state, f"{label} cannot be validated ({_why(exc)})")
                continue
            if now - marker["updated_at"] > _GUIDED_MARKER_MAX_AGE:
                _drop_run_marker(directory, filename)
                continue
            action_tools = marker["tools"]
            state["runs"][marker["session_id"]] = {
                "name": marker["name"],
                "version": marker["version"],
                "plan": {
                    "inputs": {},
                    "steps": [{"tool": tool} for tool in action_tools],
                    "tools": action_tools,
                },
                "index": marker["index"],
                "state": "stopped",
                "blocks": 0,
                "pending": None,
                "reads": {},
                "interrupted": True,
            }
    finally:
        os.close(directory)


def _load_run_markers(root, state):
    """Re-arm fresh markers as stopped runs, and fail closed on what cannot be.

    Discarded: valid markers older than 24 hours. Kept, never deleted: a marker
    that is valid JSON with an invalid field holds its own session when it names
    one readably (and its filename agrees); text that is not JSON, a marker that
    names no usable session, and an unreadable directory or marker hold every
    session. /proof cancel releases a hold.
    """
    try:
        _scan_run_markers(root, state)
    except Exception as exc:
        _hold_everything(state, f"loading markers failed ({_why(exc)})")


def _on_pre_tool_call(ctx, tool_name, args, **kwargs):
    state = _state(ctx)
    session = _sid(kwargs.get("session_id"))
    args = args or {}
    with state["lock"]:
        hold = _marker_hold_message(state, session)
        if hold is not None and tool_name not in core.READ_ONLY_TOOLS:
            return _block(hold)
        if session in state["runs"]:
            run = state["runs"][session]
            prior_state = run.get("state") if isinstance(run, dict) else None
            try:
                verdict = guided.evaluate_call(
                    run,
                    tool_name=tool_name,
                    args=args,
                    tool_call_id=kwargs.get("tool_call_id"),
                )
            except Exception:
                # Fail closed. A run that is missing, not a mapping, or fails its
                # structural check cannot tell us which tools the procedure covers,
                # so every effectful tool stays blocked in this session until the
                # operator cancels. Read-only tools stay usable.
                if tool_name in core.READ_ONLY_TOOLS:
                    return None
                plan = run.get("plan") if isinstance(run, dict) else None
                tools = plan.get("tools") if isinstance(plan, dict) else None
                if isinstance(run, dict):
                    run["state"] = "stopped"
                    if tools:
                        try:
                            _write_run_marker(_contracts_root(ctx), session, run)
                        except Exception:
                            pass
                return _block(
                    "guided run state is unreadable; every effectful tool is blocked "
                    "in this session. Use /proof cancel to release them."
                )
            if (
                isinstance(run, dict)
                and prior_state != "stopped"
                and run.get("state") == "stopped"
            ):
                try:
                    _write_run_marker(_contracts_root(ctx), session, run)
                except Exception:
                    pass
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
        if isinstance(run, dict):
            outcome = None
            try:
                outcome = guided.note_result(
                    run,
                    tool_name=tool_name,
                    args=args,
                    result=result,
                    metadata=metadata,
                    tool_call_id=kwargs.get("tool_call_id"),
                )
            except Exception:
                # An observer error must not leave a run free to continue unchecked.
                run["state"] = "stopped"
                run["pending"] = None
                outcome = "stopped"
            if run.get("state") == "completed":
                try:
                    _delete_run_marker(_contracts_root(ctx), session)
                except Exception:
                    # Failed cleanup cannot release the tools; retain a held marker.
                    run["state"] = "stopped"
                    run["index"] = _marker_step_index(run)
                    try:
                        _write_run_marker(_contracts_root(ctx), session, run)
                    except Exception:
                        pass
            elif outcome in ("advanced", "failed", "stopped") or run.get("state") == "stopped":
                try:
                    _write_run_marker(_contracts_root(ctx), session, run)
                except Exception:
                    run["state"] = "stopped"
                    run["pending"] = None
                    run["index"] = _marker_step_index(run)
                    try:
                        _write_run_marker(_contracts_root(ctx), session, run)
                    except Exception:
                        pass
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
    (state.get("held_sessions") or {}).pop(session, None)
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
        except Exception:
            if isinstance(run, dict):
                run["state"] = "stopped"
                run["pending"] = None
                try:
                    _write_run_marker(_contracts_root(ctx), session, run)
                except Exception:
                    pass
            return {
                "context": "Proof Run: guided run state is unreadable and stopped. "
                "Report this to the operator; do not improvise around its tools. "
                "/proof cancel releases them."
            }
        if not keep:
            state["runs"].pop(session, None)
    return {"context": text} if text else None


def _on_session_finalize(ctx, session_id=None, **kwargs):
    state = _state(ctx)
    session = _sid(session_id)
    with state["lock"]:
        try:
            _delete_run_marker(_contracts_root(ctx), session)
        except Exception:
            # If cleanup is unavailable, the marker re-arms as held on the next load.
            pass
        _clear_session(state, session)


def _begin(ctx, state, args, session, rebind):
    name = core._validate_contract_name(args[1])
    if session in state["recorders"]:
        return "Already recording in this session. Use /proof stop or /proof cancel."
    hold = _marker_hold_message(state, session)
    if hold is not None:
        return f"Recording not started: {hold}"
    held = state["runs"].get(session)
    if held is not None and held.get("interrupted"):
        return "Recording not started: an interrupted guided run is stopped and held. Use /proof cancel first."
    prior = core.load_contract(_contracts_root(ctx), name)
    if prior and not rebind:
        return f"Contract {name} already exists. Use /proof rebind {name}."
    if not prior and rebind:
        return f"No contract named {name}. Use /proof start {name}."
    recorder = core.Recorder(root=_contracts_root(ctx))
    recorder.start(name, intent=" ".join(args[2:]) or name)
    if session in state["runs"]:
        try:
            _delete_run_marker(_contracts_root(ctx), session)
        except Exception as exc:
            return f"Recording not started: could not clear the guided run marker ({exc})."
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
    hold = _marker_hold_message(state, session)
    if hold is not None:
        return f"Run not started: {hold}"
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
    try:
        _write_run_marker(_contracts_root(ctx), session, run)
    except (core.ContractError, OSError, ValueError, TypeError, KeyError) as exc:
        return f"Run not started: could not persist the run marker ({exc})."
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
                note = ""
                try:
                    _delete_run_marker(_contracts_root(ctx), session)
                except Exception as exc:
                    if not state.get("global_hold"):
                        return f"Cancellation failed: could not remove the guided run marker ({exc})."
                    # Nothing reachable to delete: the store itself is what is held.
                    note = (
                        f" The guided run marker could not be removed ({exc}); "
                        "it holds again after the next plugin reload."
                    )
                if (state.get("held_sessions") or {}).get(session):
                    note = " The marker hold on this session is cleared." + note
                if state.get("global_hold"):
                    state["global_hold"] = []
                    note = " The marker hold on every session is cleared." + note
                _clear_session(state, session)
                return (
                    "Recording or guided run cancelled in this session. "
                    "Existing contracts are unchanged." + note
                )
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
                lines = []
                hold = _marker_hold_message(state, session)
                if hold is not None:
                    lines.append(f"hold: {hold}")
                try:
                    contracts = core.list_contracts(_contracts_root(ctx))
                except (core.ContractError, OSError, ValueError, TypeError, KeyError) as exc:
                    if hold is None:
                        raise
                    contracts = []
                    lines.append(f"Contracts unavailable: {exc}")
                for c in contracts:
                    line = (
                        f"{c['contract_id']} v{c['version']} bound={c.get('bound', False)} "
                        f"evidence={c.get('evidence_status', 'legacy')}"
                    )
                    if isinstance(c.get("procedure"), dict):
                        line += " procedure"
                    lines.append(line)
                if session in state["runs"]:
                    run = state["runs"][session]
                    try:
                        lines.append(guided.status_text(run))
                    except Exception:
                        lines.append(
                            "guided run state is unreadable; every effectful tool is "
                            "blocked in this session. Use /proof cancel to release them."
                        )
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
        "held_sessions": {},
        "global_hold": [],
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
        _load_run_markers(root, ctx._proof_run_state)
        store = WorkflowStore(root)
        if hasattr(ctx, "register_tool_authorizer"):
            ctx.register_tool_authorizer(hostport.make_legacy_authorizer(ctx, store))
            ctx._proof_run_state["authorizer_guard"] = "installed"
        if hasattr(ctx, "register_execution_receipt_sink"):
            ctx.register_execution_receipt_sink(hostport.make_receipt_sink(ctx, store))
    except Exception:
        if ctx._proof_run_state.get("authorizer_guard") != "installed":
            ctx._proof_run_state["authorizer_guard"] = "failed"
