"""New /proof commands: capture, prepare, inspect, promote, run, recover.

Trusted actor identity is taken from PluginContext.profile_name and the
session keywords the handler declared, never from user JSON. session_surface()
is a string surface name and is not used as a context dict.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
from pathlib import Path
from typing import Any

from . import capture, compiler, core, follower, hostport
from .engine import WorkflowRunner
from .store import WorkflowStore
from .workflow import WorkflowError

_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
_RUN_ID_RE = re.compile(r"^R-[0-9a-f]{16}$")
_LEGACY = {"start", "stop", "cancel", "learn", "rebind", "status", "unbind"}
_NEW = {
    "capture",
    "prepare",
    "inspect",
    "promote",
    "run",
    "runs",
    "show",
    "cancel-run",
    "resume",
}


class _RejectingDecoder(json.JSONDecoder):
    def __init__(self, **kwargs):
        kwargs.setdefault("object_pairs_hook", self._pairs)
        kwargs.setdefault("parse_constant", self._const)
        super().__init__(**kwargs)

    @staticmethod
    def _const(value: str):
        raise WorkflowError("INPUT_INVALID", "non-finite number")

    @staticmethod
    def _pairs(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise WorkflowError("INPUT_INVALID", "duplicate JSON key")
            out[key] = value
        return out


def parse_json_object(text: str) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise WorkflowError("INPUT_INVALID", "JSON object required")
    try:
        value = json.loads(text, cls=_RejectingDecoder)
    except WorkflowError:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise WorkflowError("INPUT_INVALID", "malformed JSON object") from exc
    if not isinstance(value, dict):
        raise WorkflowError("INPUT_INVALID", "inputs must be a JSON object")
    return value


def parse_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise WorkflowError(
            "INPUT_INVALID",
            "invalid name: use letters, digits, hyphens, underscores (max 64)",
        )
    return name


def parse_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise WorkflowError("INPUT_INVALID", "malformed run id")
    return run_id


def parse_revision(token: str) -> int:
    if not isinstance(token, str) or not token.isdigit() or str(int(token)) != token:
        raise WorkflowError("INPUT_INVALID", "malformed revision")
    return int(token)


def parse_proof_args(raw: str) -> dict | None:
    text = (raw or "").strip()
    if not text:
        return None
    first, _, rest = text.partition(" ")
    first = first.strip()
    rest = rest.strip()
    if first in _LEGACY or first not in _NEW:
        return None
    if first == "capture":
        action, _, remainder = rest.partition(" ")
        action = action.strip()
        remainder = remainder.strip()
        if action == "start":
            if not remainder:
                raise WorkflowError("INPUT_INVALID", "capture start requires a name")
            name, _, intent = remainder.partition(" ")
            return {
                "verb": "capture",
                "action": "start",
                "name": parse_name(name),
                "intent": intent.strip(),
            }
        if action in {"finish", "cancel"} and not remainder:
            return {"verb": "capture", "action": action}
        raise WorkflowError("INPUT_INVALID", "Usage: capture start <name> [intent] | finish | cancel")
    if first == "runs" and not rest:
        return {"verb": "runs"}
    if first == "prepare":
        name, _, payload = rest.partition(" ")
        parsed = {"verb": "prepare", "name": parse_name(name)}
        if payload.strip():
            parsed["review"] = parse_json_object(payload.strip())
            compiler.reject_unknown_review_fields(parsed["review"])
        else:
            parsed["review"] = {}
        return parsed
    if first == "inspect":
        return {"verb": "inspect", "name": parse_name(rest)}
    if first == "promote":
        name, _, rev = rest.partition(" ")
        return {
            "verb": "promote",
            "name": parse_name(name),
            "revision": parse_revision(rev.strip()),
        }
    if first == "run":
        name, _, payload = rest.partition(" ")
        parsed = {"verb": "run", "name": parse_name(name), "inputs": {}}
        if payload.strip():
            parsed["inputs"] = parse_json_object(payload.strip())
        return parsed
    if first == "show":
        return {"verb": "show", "run_id": parse_run_id(rest)}
    if first == "cancel-run":
        return {"verb": "cancel-run", "run_id": parse_run_id(rest)}
    if first == "resume":
        return {"verb": "resume", "run_id": parse_run_id(rest)}
    return None


def trusted_actor(
    ctx,
    session_id: str = "default",
    session_key: str | None = None,
    user_payload: Any = None,
    **kwargs,
) -> dict:
    profile = getattr(ctx, "profile_name", None)
    if not isinstance(profile, str) or not profile.strip():
        raise WorkflowError("AUTH_CONTEXT_MISSING", "profile_name is required")
    session = session_id if isinstance(session_id, str) and session_id.strip() else "default"
    if isinstance(session_key, str) and session_key.strip():
        actor_id = session_key.strip()
    else:
        actor_id = f"{profile}:{session}"
    return {
        "id": actor_id,
        "profile_id": profile,
        "session_id": session,
    }


def _root(ctx, root=None) -> Path:
    if root is not None:
        return Path(root)
    home = getattr(ctx, "home", None)
    if home is not None:
        return Path(home)
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir("proof-run")


def _format_error(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    if code:
        return f"{code}: {message}"
    return message


_TERMINAL_RUN_STATES = {
    "completed",
    "verified",
    "blocked",
    "failed",
    "needs_review",
    "unknown_outcome",
    "canceled",
}


def _persist_scheduled_failure(store: WorkflowStore, run_id: str, exc: BaseException, *, cancelled: bool = False) -> None:
    try:
        run = store.get_run(run_id)
    except (WorkflowError, OSError, KeyError, TypeError, ValueError):
        return
    if run.get("state") in _TERMINAL_RUN_STATES:
        return
    dispatched = bool(run.get("dispatch_started"))
    if cancelled:
        try:
            store.request_cancel(run_id)
        except (WorkflowError, OSError, KeyError, TypeError, ValueError):
            pass
        code = "CANCELLED"
        state = "unknown_outcome" if dispatched else "canceled"
        try:
            store.update_run(run_id, state=state, error_code=code)
        except (WorkflowError, OSError, KeyError, TypeError, ValueError):
            return
        return
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not code:
        code = type(exc).__name__ or "RUNNER_ERROR"
    state = "unknown_outcome" if dispatched else "failed"
    try:
        store.update_run(run_id, state=state, error_code=code)
    except (WorkflowError, OSError, KeyError, TypeError, ValueError):
        return


def _schedule(ctx, runner: WorkflowRunner, run_id: str, method: str) -> str:
    spawn = getattr(ctx, "spawn_task", None)
    if not callable(spawn):
        return (
            f"created {run_id}; background drive requires ctx.spawn_task. "
            f"Resume with /proof resume {run_id}"
        )

    async def job():
        fn = getattr(runner, method)
        copied = contextvars.copy_context()
        try:
            return await asyncio.to_thread(copied.run, fn, run_id)
        except asyncio.CancelledError:
            _persist_scheduled_failure(runner.store, run_id, asyncio.CancelledError(), cancelled=True)
            raise
        except Exception as exc:
            _persist_scheduled_failure(runner.store, run_id, exc)
            return None

    coro = job()
    try:
        spawn(coro, name=f"proof-run:{run_id}")
    except (RuntimeError, TypeError) as exc:
        if hasattr(coro, "close"):
            coro.close()
        return (
            f"created {run_id}; could not schedule ({exc}). "
            f"Resume with /proof resume {run_id}"
        )
    return f"scheduled off-loop as {run_id}"


def _host_port(ctx, store: WorkflowStore, actor: dict):
    tenant = getattr(ctx, "tenant_id", None)
    if not isinstance(tenant, str) or not tenant:
        tenant = "tenant-A"
    epoch = getattr(ctx, "routing_intent_epoch", None)
    if not isinstance(epoch, str) or not epoch:
        epoch = "initial"
    return hostport.CheckedHostPort(
        ctx,
        store,
        session_id=actor["session_id"],
        profile_id=actor["profile_id"],
        tenant_id=tenant,
        routing_intent_epoch=epoch,
    )


def _follower_for_workflow(wf: dict):
    route = wf.get("model_route")
    if not route:
        return None
    provider = route.get("provider")
    if provider in {"deterministic", "none", "no-model", "controlled-test", "scripted"}:
        if route.get("inference"):
            return follower.follower_for_route(route, allow_network=False)
        return follower.DeterministicFollower(
            identity={
                "provider": provider or "deterministic",
                "model": route.get("model") or "no-model",
                "inference": False,
                "fallback": False,
            }
        )
    return follower.follower_for_route(route, allow_network=True)


def handle(
    ctx,
    raw: str,
    session_id: str = "default",
    session_key: str | None = None,
    platform: str | None = None,
    source: str | None = None,
    root=None,
    **kwargs,
) -> str | None:
    try:
        parsed = parse_proof_args(raw)
    except WorkflowError as exc:
        return _format_error(exc)
    if parsed is None:
        return None
    actor = trusted_actor(ctx, session_id=session_id, session_key=session_key)
    data_root = _root(ctx, root)
    store = WorkflowStore(data_root)
    verb = parsed["verb"]
    try:
        if verb == "capture":
            return _cmd_capture(data_root, parsed, actor, store)
        if verb == "prepare":
            return _cmd_prepare(data_root, parsed, store)
        if verb == "inspect":
            return _cmd_inspect(parsed, store)
        if verb == "promote":
            return _cmd_promote(parsed, actor, store)
        if verb == "run":
            return _cmd_run(ctx, parsed, actor, store)
        if verb == "runs":
            return _cmd_runs(store)
        if verb == "show":
            return _cmd_show(parsed, store)
        if verb == "cancel-run":
            return _cmd_cancel(parsed, store)
        if verb == "resume":
            return _cmd_resume(ctx, parsed, actor, store)
    except (WorkflowError, capture.CaptureError, follower.FollowerError) as exc:
        return _format_error(exc)
    return None


def _cmd_capture(root: Path, parsed: dict, actor: dict, store: WorkflowStore) -> str:
    action = parsed["action"]
    session_id = actor["session_id"]
    profile_id = actor["profile_id"]
    if action == "start":
        capture.start_trace(
            root,
            name=parsed["name"],
            session_id=session_id,
            profile_id=profile_id,
            intent=parsed.get("intent") or parsed["name"],
        )
        return (
            f"Trace-only capture '{parsed['name']}' started. Demonstrate the task, "
            "then /proof capture finish. This does not activate a schema-2 gate."
        )
    if action == "cancel":
        capture.cancel_trace(root, session_id=session_id)
        return "Capture cancelled in this session. Existing contracts and workflows are unchanged."
    trace = capture.finish_trace(root, session_id=session_id, store=store)
    complete = "complete" if trace.get("complete") else "incomplete"
    return (
        f"Capture '{trace['name']}' finished ({complete}, "
        f"{len(trace.get('receipts') or [])} receipts). "
        "Prepare a candidate with /proof prepare "
        f"{trace['name']}. No schema-2 contract was activated."
    )


def _cmd_prepare(root: Path, parsed: dict, store: WorkflowStore) -> str:
    name = parsed["name"]
    review = parsed.get("review") or {}
    compiler.reject_unknown_review_fields(review)
    trace = capture.load_trace(root, name)
    if trace is None:
        raise WorkflowError(
            "DEMO_EVIDENCE_INCOMPLETE",
            f"no captured trace named {name}; /proof capture start/finish first",
        )
    wf = compiler.compile_from_trace(trace, compiler.DEFAULT_CATALOG, review)
    stored = store.put_candidate(wf["workflow_id"], wf)
    unresolved = stored.get("unresolved") or []
    extra = ""
    if unresolved:
        extra = f" Unresolved ({len(unresolved)}): cannot promote until reviewed."
    return (
        f"Candidate '{stored['workflow_id']}' r{stored['revision']} "
        f"digest={stored['content_digest'][:19]}… status=candidate.{extra}"
    )


def _public_text(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, sort_keys=True)
    try:
        core._reject_secrets(text)
    except Exception:
        return "[redacted]"
    return text


def _format_ref(value: Any) -> str:
    if isinstance(value, dict):
        if "node" in value:
            return f"node:{value['node']}{value.get('pointer') or ''}"
        if "input" in value:
            return f"input:{value['input']}"
        if "constant" in value:
            return f"constant:{value['constant']}"
        if "literal" in value:
            return f"literal:{_public_text(value['literal'])}"
        if "effect_intent" in value:
            return f"effect_intent:{value['effect_intent']}{value.get('pointer') or ''}"
        if "host_context" in value:
            return f"host_context:{value['host_context']}"
        tag = next(iter(value), "")
        return f"{tag}:{_public_text(value.get(tag, ''))}"
    return _public_text(value)


def _format_check(check: Any) -> str:
    if not isinstance(check, dict):
        return _public_text(check)
    op = check.get("op") or "check"
    return f"{op} {_format_ref(check.get('left'))} == {_format_ref(check.get('right'))}"


def _format_input_domain(name: str, spec: Any) -> str:
    if not isinstance(spec, dict):
        return str(name)
    parts = [f"{name}:{spec.get('type') or 'any'}"]
    if spec.get("pattern"):
        parts.append(f"pattern={spec['pattern']}")
    if spec.get("enum"):
        parts.append("enum=" + ",".join(_public_text(item) for item in spec["enum"]))
    if spec.get("const") is not None:
        parts.append(f"const={_public_text(spec['const'])}")
    if spec.get("minLength") is not None:
        parts.append(f"minLength={spec['minLength']}")
    if spec.get("maxLength") is not None:
        parts.append(f"maxLength={spec['maxLength']}")
    return " ".join(parts)


def format_inspect(doc: dict) -> str:
    """Human-readable standing review. No raw private trace bodies."""
    unresolved = doc.get("unresolved") or []
    provenance = doc.get("provenance") or {}
    authority = doc.get("authority") or {}
    tools = list(authority.get("tools") or [])
    inputs = doc.get("inputs") or {}
    constants = doc.get("constants") or {}
    policy = doc.get("policy") or {}
    recovery = doc.get("recovery") or {}
    limits = doc.get("limits") or {}
    route = doc.get("model_route")
    refs = provenance.get("source_receipt_refs") or []
    lines = [
        (
            f"{doc.get('workflow_id')} r{doc.get('revision')} "
            f"status={doc.get('status')} promoted={doc.get('promoted')} "
            f"teacher={provenance.get('teacher_outcome_assurance')} "
            f"demo_receipt={provenance.get('actual_demonstration_receipt')} "
            f"unresolved={len(unresolved)} "
            f"inputs={list(inputs.keys())} "
            f"model_route={route}"
        )
    ]
    if tools:
        lines.append("authority_tools=" + ",".join(tools))
    tenant = authority.get("tenant")
    effects = authority.get("effects")
    if tenant or effects:
        extra = []
        if tenant:
            extra.append(f"tenant={tenant}")
        if effects:
            extra.append(f"effects={effects}")
        lines.append("authority " + " ".join(extra))
    if inputs:
        domains = [_format_input_domain(name, spec) for name, spec in inputs.items()]
        lines.append("input_domains=" + ",".join(domains))
    if constants:
        if isinstance(constants, dict):
            rendered = ",".join(
                f"{key}={_public_text(val)}" for key, val in constants.items()
            )
        else:
            rendered = _public_text(constants)
        lines.append("constants=" + rendered)
    lines.append("actions:")
    for node in doc.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        kind = node.get("kind")
        if kind not in {"read", "effect", "decide"}:
            continue
        tool = node.get("tool") or ""
        nid = node.get("id")
        if kind == "decide":
            cands = node.get("candidates") or []
            lines.append(
                f"  {nid} decide candidates={cands} source={node.get('source')}"
                f" allow_abstain={node.get('allow_abstain')}"
            )
            continue
        args = node.get("args") or {}
        bindings = []
        for key, binding in args.items():
            if key == "operation_key":
                continue
            if isinstance(binding, dict):
                tag = next(iter(binding), "")
                bindings.append(f"{key}={tag}:{binding.get(tag, '')}")
            else:
                bindings.append(f"{key}={binding}")
        bound = " ".join(bindings)
        ident_txt = ""
        ident = node.get("operation_identity")
        if kind == "effect" and isinstance(ident, dict):
            parts = ",".join(_format_ref(part) for part in (ident.get("parts") or []))
            ident_txt = f" identity={ident.get('namespace')}"
            if parts:
                ident_txt += f" parts={parts}"
        lines.append(f"  {nid} {kind} {tool} {bound}{ident_txt}".rstrip())
    verification = []
    for node in doc.get("nodes") or []:
        if not isinstance(node, dict) or node.get("kind") != "assert":
            continue
        nid = node.get("id") or "assert"
        checks = "; ".join(_format_check(item) for item in (node.get("checks") or []))
        extra = f" on_failure={node['on_failure']}" if node.get("on_failure") else ""
        verification.append(f"  {nid}{extra} {checks}".rstrip())
    if verification:
        lines.append("verification:")
        lines.extend(verification)
    if policy:
        rules = policy.get("rules") if isinstance(policy, dict) else None
        header = (
            "policy "
            f"revision={policy.get('revision')} "
            f"source={policy.get('source')}"
        )
        if isinstance(policy, dict) and policy.get("ambiguous"):
            header += f" ambiguous={policy.get('ambiguous')}"
        lines.append(header)
        if isinstance(rules, dict):
            lines.append("policy_rules:")
            for queue, hint in rules.items():
                lines.append(f"  {queue}: {_public_text(hint)}")
        elif rules:
            lines.append(f"policy_rules={_public_text(rules)}")
    if limits:
        bits = [f"{key}={_public_text(val)}" for key, val in limits.items()]
        lines.append("limits " + " ".join(bits))
    if route:
        shown = (
            "provider",
            "model",
            "inference",
            "fallback",
            "endpoint",
            "digest",
            "timeout_s",
            "num_ctx",
            "num_predict",
            "num_thread",
        )
        bits = []
        seen = set()
        for key in shown:
            if key in route and route[key] not in (None, ""):
                bits.append(f"{key}={_public_text(route[key])}")
                seen.add(key)
        for key, val in route.items():
            if key in seen or val in (None, ""):
                continue
            bits.append(f"{key}={_public_text(val)}")
        lines.append("model " + " ".join(bits))
    if recovery:
        rec_bits = [f"tool={recovery.get('tool')}", f"not_found={recovery.get('not_found')}"]
        for key, val in recovery.items():
            if key in {"tool", "not_found"} or val in (None, ""):
                continue
            rec_bits.append(f"{key}={_public_text(val)}")
        lines.append("recovery " + " ".join(rec_bits))
    if refs:
        lines.append("source_refs=" + ",".join(str(item.get("call_id") or item) for item in refs))
    else:
        lines.append("source_refs=none")
    if unresolved:
        reasons = [item.get("code") or item.get("message") or str(item) for item in unresolved]
        lines.append("unresolved_reasons=" + ",".join(str(item) for item in reasons))
    return "\n".join(lines)


def _cmd_inspect(parsed: dict, store: WorkflowStore) -> str:
    doc = store.inspect(parsed["name"])
    return format_inspect(doc)


def _cmd_promote(parsed: dict, actor: dict, store: WorkflowStore) -> str:
    doc = store.promote(parsed["name"], parsed["revision"], actor)
    return (
        f"Promoted {doc['workflow_id']} r{doc.get('revision')}. "
        "No business effect ran. Standing review is this revision; "
        "/proof run uses new inputs without per-run ceremony."
    )


def _cmd_run(ctx, parsed: dict, actor: dict, store: WorkflowStore) -> str:
    host = _host_port(ctx, store, actor)
    caps = host.capabilities()
    if not hostport.capabilities_ok(caps):
        reason = caps.get("refusal_reason") or "checked execution is unavailable"
        return f"HOST_CAPABILITY_MISSING: {reason}"
    guard = getattr(ctx, "_proof_run_state", None) or {}
    if isinstance(guard, dict) and guard.get("authorizer_guard") == "failed":
        return "HOST_CAPABILITY_MISSING: final authorizer is not installed; new runs are unavailable"
    run = store.create_run(parsed["name"], parsed.get("inputs") or {}, actor)
    wf = store.load_workflow_for_run(run)
    bound_follower = _follower_for_workflow(wf)
    runner = WorkflowRunner(store, host, bound_follower)
    scheduled = _schedule(ctx, runner, run["run_id"], "drive")
    route = wf.get("model_route") or {"provider": "none"}
    return (
        f"Run {run['run_id']}: {run['name']} r{run['revision']}; "
        f"inputs validated; follower {route.get('provider')}/{route.get('model')} ; "
        f"{scheduled}. Inspect with /proof show {run['run_id']}"
    )


def _cmd_runs(store: WorkflowStore) -> str:
    rows = store.list_runs(limit=20)
    if not rows:
        return "No workflow runs."
    lines = [
        f"{row['run_id']} {row['name']} r{row['revision']} state={row['state']}"
        for row in rows
    ]
    return "\n".join(lines)


def _cmd_show(parsed: dict, store: WorkflowStore) -> str:
    run = store.get_run(parsed["run_id"])
    return json.dumps(
        {
            "run_id": run["run_id"],
            "name": run["name"],
            "revision": run["revision"],
            "state": run["state"],
            "inputs": run.get("inputs"),
            "error_code": run.get("error_code"),
            "effect_readback": run.get("effect_readback"),
            "semantic_judgment": run.get("semantic_judgment"),
            "whole_goal": run.get("whole_goal"),
            "teacher_outcome_assurance": run.get("teacher_outcome_assurance"),
        },
        sort_keys=True,
    )


def _cmd_cancel(parsed: dict, store: WorkflowStore) -> str:
    run = store.request_cancel(parsed["run_id"])
    return f"Cancel requested for {run['run_id']}; state={run['state']}"


def _cmd_resume(ctx, parsed: dict, actor: dict, store: WorkflowStore) -> str:
    host = _host_port(ctx, store, actor)
    if not hostport.capabilities_ok(host.capabilities()):
        return "HOST_CAPABILITY_MISSING: checked execution is unavailable"
    run = store.get_run(parsed["run_id"])
    wf = store.load_workflow_for_run(run)
    runner = WorkflowRunner(store, host, _follower_for_workflow(wf))
    scheduled = _schedule(ctx, runner, run["run_id"], "resume")
    return f"Resume {run['run_id']}; {scheduled}"
