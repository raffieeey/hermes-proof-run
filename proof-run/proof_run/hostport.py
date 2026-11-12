"""Checked HostPort adapter owned by the operator lane.

Never falls back to raw dispatch_tool. Final authorizer phases: check is pure;
commit consumes legacy evidence once. execution_phase=transform defers legacy
pre-hook consumption only when this authorizer actually registered.
"""

from __future__ import annotations

import copy
import secrets
import time
from typing import Any

from . import capture, core
from .compiler import DEFAULT_CATALOG
from .workflow import WorkflowError

_FACT_MAX_AGE = 60.0
_PENDING_MAX = 256
_REQUIRED_CAPS = ("final_authorization", "mandatory_receipts", "bound_context")
_FRAME_KEY = "checked_frames"


def should_defer_legacy_consume(execution_phase: Any) -> bool:
    return execution_phase == "transform"


def transform_deferral_active(ctx, execution_phase: Any) -> bool:
    """Defer legacy consume only when the final guard really installed."""
    if not should_defer_legacy_consume(execution_phase):
        return False
    state = getattr(ctx, "_proof_run_state", None)
    if not isinstance(state, dict):
        return False
    return state.get("authorizer_guard") == "installed"


def host_capabilities(ctx) -> dict:
    getter = getattr(ctx, "checked_execution_capabilities", None)
    if not callable(getter):
        return {
            "version": "checked-execution/v0",
            "final_authorization": False,
            "mandatory_receipts": False,
            "bound_context": False,
            "plugin_host_supported": False,
            "refusal_reason": "checked execution API is not available on this host",
        }
    caps = getter()
    if not isinstance(caps, dict):
        return {
            "version": "checked-execution/v0",
            "final_authorization": False,
            "mandatory_receipts": False,
            "bound_context": False,
            "plugin_host_supported": False,
            "refusal_reason": "checked execution capabilities were unreadable",
        }
    return copy.deepcopy(caps)


def capabilities_ok(caps: dict) -> bool:
    if not isinstance(caps, dict):
        return False
    version = caps.get("version")
    if not isinstance(version, str) or not version.startswith("checked-execution/"):
        return False
    return all(caps.get(key) is True for key in _REQUIRED_CAPS)


def _sid(session_id) -> str:
    return session_id or "default"


def _block(message: str) -> dict:
    return {"action": "block", "message": f"Proof Run: {message}"}


def _contracts_root(ctx, store=None):
    if store is not None and getattr(store, "root", None) is not None:
        return store.root
    if getattr(ctx, "home", None) is not None:
        return ctx.home
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir("proof-run")


def _state(ctx) -> dict:
    state = getattr(ctx, "_proof_run_state", None)
    if not isinstance(state, dict):
        raise WorkflowError("AUTH_CONTEXT_MISSING", "plugin state is missing")
    return state


def _tool_kind(tool_name: str) -> str:
    meta = DEFAULT_CATALOG.get(tool_name) or {}
    kind = meta.get("kind")
    if kind in {"read", "effect", "status"}:
        return kind
    if tool_name in core.READ_ONLY_TOOLS:
        return "read"
    if isinstance(tool_name, str) and "operation_status" in tool_name:
        return "status"
    return "unknown"


def bind_controller_frame(ctx, *, tool_call_id: str, frame: dict) -> None:
    state = _state(ctx)
    lock = state.get("lock")
    if lock is None:
        raise WorkflowError("AUTH_CONTEXT_MISSING", "plugin lock is missing")
    with lock:
        pending = state.setdefault(_FRAME_KEY, {})
        if len(pending) >= _PENDING_MAX:
            raise WorkflowError(
                "FENCE_STALE", "too many unfinished checked frames; restart the session"
            )
        pending[tool_call_id] = copy.deepcopy(frame)


def pop_controller_frame(ctx, tool_call_id: str) -> dict | None:
    state = getattr(ctx, "_proof_run_state", None)
    if not isinstance(state, dict):
        return None
    lock = state.get("lock")
    if lock is None:
        return None
    with lock:
        pending = state.get(_FRAME_KEY) or {}
        return pending.pop(tool_call_id, None)


def lookup_controller_frame(ctx, tool_call_id: str) -> dict | None:
    state = getattr(ctx, "_proof_run_state", None)
    if not isinstance(state, dict) or not tool_call_id:
        return None
    lock = state.get("lock")
    if lock is None:
        return None
    with lock:
        pending = state.get(_FRAME_KEY) or {}
        frame = pending.get(tool_call_id)
        return copy.deepcopy(frame) if isinstance(frame, dict) else None


def evaluate_legacy_gate(
    ctx,
    tool_name: str,
    args: dict,
    *,
    session_id: str,
    tool_call_id: str = "",
    consume: bool,
    store=None,
) -> dict | None:
    """Same schema-2 gate as the legacy pre-hook. consume=False is pure."""
    state = ctx._proof_run_state
    session = _sid(session_id)
    args = args or {}
    with state["lock"]:
        cache = state["runtime"].setdefault(session, {})
        if tool_name in core.READ_ONLY_TOOLS:
            if consume:
                cache.pop(core.fact_key(tool_name, args), None)
            return None
        try:
            contracts = core.list_contracts(_contracts_root(ctx, store))
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
                        "step": copy.deepcopy(contract["steps"][verdict["step_index"]]),
                    }
                )
            if not consume:
                return None
            if checks:
                if not tool_call_id:
                    return _block("a guarded action requires a host tool_call_id")
                if len(state["pending"]) >= _PENDING_MAX:
                    return _block(
                        "too many unfinished guarded actions; restart the session"
                    )
                state["pending"][(session, tool_call_id)] = checks
            cache.clear()
            return None
        except (core.ContractError, OSError, ValueError, TypeError, KeyError):
            return _block("contract is unreadable or invalid; repair or re-record it")


def _protected_run_context(frame: dict, *, mutation: bool) -> dict:
    intent = copy.deepcopy(frame.get("intent_ref") or {})
    if not mutation:
        intent = {}
    return {
        "run_id": frame.get("run_id") or "",
        "profile_id": frame.get("profile_id"),
        "session_id": frame.get("session_id"),
        "workflow_digest": frame.get("workflow_digest"),
        "fence": frame.get("fence"),
        "intent_ref": intent,
    }


def _protected_status_context(frame: dict) -> dict:
    intent_raw = frame.get("intent_ref")
    intent = intent_raw if isinstance(intent_raw, dict) else {}
    key = intent.get("operation_key")
    return {
        "run_id": frame.get("run_id") or "",
        "profile_id": frame.get("profile_id"),
        "session_id": frame.get("session_id"),
        "workflow_digest": frame.get("workflow_digest"),
        "fence": frame.get("fence"),
        "intent_ref": {"operation_key": key} if isinstance(key, str) and key else {},
    }


def _own_run(store, run_id: str) -> bool:
    if store is None or not isinstance(run_id, str) or not run_id.startswith("R-"):
        return False
    try:
        store.get_run(run_id)
    except (WorkflowError, OSError, KeyError, TypeError, ValueError):
        return False
    return True


def make_legacy_authorizer(ctx, store):
    def authorizer(tool_name, tool_args, *, phase, **context):
        if phase not in {"check", "commit"}:
            return _block("unknown authorizer phase")
        session_id = context.get("session_id") or ""
        tool_call_id = context.get("tool_call_id") or ""
        frame = lookup_controller_frame(ctx, tool_call_id)
        if frame is None:
            if _own_run(store, context.get("run_id") or ""):
                return _block("missing protected controller frame for this run")
            return evaluate_legacy_gate(
                ctx,
                tool_name,
                tool_args or {},
                session_id=session_id,
                tool_call_id=tool_call_id,
                consume=(phase == "commit"),
                store=store,
            )
        if frame.get("tool_name") != tool_name:
            return _block("tool does not match the protected controller frame")
        supplied_session = context.get("session_id")
        if supplied_session not in (None, "", frame.get("session_id")):
            return _block("session does not match the protected controller frame")
        supplied_profile = context.get("profile_id")
        if supplied_profile not in (None, "", frame.get("profile_id")):
            return _block("profile does not match the protected controller frame")
        kind = frame.get("kind") or "unknown"
        mutation = kind == "effect"
        if mutation or kind == "status":
            expected = frame.get("expected_args") or {}
            if (tool_args or {}) != expected:
                if mutation:
                    return _block("final args do not match the protected original payload")
                return _block("final args do not match the protected status lookup")
        if store is not None and frame.get("run_id"):
            try:
                if kind == "status":
                    store.admit_status_lookup(
                        _protected_status_context(frame),
                        tool_name,
                        tool_args or {},
                    )
                else:
                    store.admit_dispatch(
                        _protected_run_context(frame, mutation=mutation),
                        tool_name,
                        tool_args or {},
                    )
            except WorkflowError as exc:
                return _block(f"{exc.code}: {exc.message}")
        return evaluate_legacy_gate(
            ctx,
            tool_name,
            tool_args or {},
            session_id=session_id or frame.get("session_id") or "",
            tool_call_id=tool_call_id,
            consume=(phase == "commit"),
            store=store,
        )

    return authorizer


def make_receipt_sink(ctx, store):
    root = _contracts_root(ctx, store)

    def sink(receipt, phase=None, **kwargs):
        if not isinstance(receipt, dict):
            return None
        rec = copy.deepcopy(receipt)
        if phase and not rec.get("phase"):
            rec["phase"] = phase
        session_id = rec.get("session_id") or kwargs.get("session_id") or ""
        profile_id = rec.get("profile_id") or ""
        if not profile_id:
            name = getattr(ctx, "profile_name", None)
            profile_id = name if isinstance(name, str) else ""
        run_id = rec.get("run_id") or ""
        capture.ingest_receipt(
            root,
            rec,
            session_id=session_id,
            profile_id=profile_id,
            store=store,
            run_id=run_id,
        )
        return None

    return sink


class CheckedHostPort:
    def __init__(
        self,
        ctx,
        store,
        *,
        session_id: str,
        profile_id: str,
        tenant_id: str = "",
        routing_intent_epoch: str = "initial",
    ):
        self.ctx = ctx
        self.store = store
        self.session_id = session_id
        self.profile_id = profile_id
        self.tenant_id = tenant_id or "tenant-A"
        self.routing_intent_epoch = routing_intent_epoch or "initial"

    def capabilities(self) -> dict:
        return host_capabilities(self.ctx)

    def authenticated_context(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "routing_intent_epoch": self.routing_intent_epoch,
        }

    def execute_checked(self, tool_name: str, args: dict, *, run_context: dict) -> dict:
        caps = self.capabilities()
        if not capabilities_ok(caps):
            reason = caps.get("refusal_reason") or "checked execution is unavailable"
            raise WorkflowError("HOST_CAPABILITY_MISSING", str(reason))
        execute = getattr(self.ctx, "execute_checked", None)
        if not callable(execute):
            raise WorkflowError(
                "HOST_CAPABILITY_MISSING",
                "execute_checked is missing; raw dispatch_tool is not a fallback",
            )
        context = run_context or {}
        intent = context.get("intent_ref") or {}
        kind = _tool_kind(tool_name)
        if kind == "unknown":
            kind = "read"
        tool_call_id = "pr-" + secrets.token_hex(16)
        frame = {
            "tool_name": tool_name,
            "expected_args": copy.deepcopy(args or {}),
            "run_id": context.get("run_id") or "",
            "profile_id": context.get("profile_id") or self.profile_id,
            "session_id": context.get("session_id") or self.session_id,
            "workflow_digest": context.get("workflow_digest") or "",
            "fence": context.get("fence"),
            "intent_ref": copy.deepcopy(intent) if isinstance(intent, dict) else {},
            "kind": kind,
            "actor": {
                "profile_id": context.get("profile_id") or self.profile_id,
                "session_id": context.get("session_id") or self.session_id,
            },
        }
        bind_controller_frame(self.ctx, tool_call_id=tool_call_id, frame=frame)
        try:
            return execute(
                tool_name,
                copy.deepcopy(args),
                run_id=frame["run_id"],
                operation_identity=intent.get("operation_key") or "",
                session_id=frame["session_id"],
                policy_ref=frame["workflow_digest"] or "",
                tool_call_id=tool_call_id,
            )
        finally:
            pop_controller_frame(self.ctx, tool_call_id)

    def operation_status(self, intent: dict, *, run_context: dict) -> dict:
        tool = (intent or {}).get("status_tool") or "example_ticket_operation_status"
        args = {"operation_key": (intent or {}).get("operation_key")}
        receipt = self.execute_checked(tool, args, run_context=run_context)
        from .workflow import parse_json_result

        decoded = parse_json_result(receipt.get("result"))
        if isinstance(decoded, dict):
            return decoded
        return {"state": "unknown", "operation_key": (intent or {}).get("operation_key")}

    def progress(self, message: str, *, run_context: dict) -> None:
        messages = getattr(self.ctx, "progress_messages", None)
        if isinstance(messages, list):
            messages.append(message)
        post = getattr(self.ctx, "post_message", None)
        if callable(post):
            post(message)
