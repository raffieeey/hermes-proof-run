"""Proof Run workflow IR: strict bindings, predicates, digests, closed decisions.

The operator compiler (owned by another lane) constructs a ``proof.workflow/v1``
program and hands it to ``validate_workflow``. Two supported shapes:

* Exact, no-model: finite read/assert/effect/readback, empty ``inputs``,
  ``model_route`` null, zero decide nodes. See
  ``tests/fixtures/exact_ensure_fixture_release.json``.
* Parameterized, one decide: typed inputs, one bounded decide, one effect.
  See ``tests/fixtures/semantic_route_ticket.json``.

Bindings are tagged JSON objects (``literal``, ``input``, ``constant``,
``host_context``, ``node``+``pointer``, ``effect_intent``+``pointer``). There is
no eval, shell, env expansion, or implicit coercion. An effect may name its own
already-persisted intent, never its own future result.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any


class WorkflowError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
_NODE_ID_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,63}")
_BINDING_TAGS = (
    "literal",
    "input",
    "constant",
    "host_context",
    "node",
    "effect_intent",
)
_ALLOWED_TOP = {
    "format",
    "workflow_id",
    "revision",
    "content_digest",
    "status",
    "provenance",
    "inputs",
    "constants",
    "authority",
    "adapter_id",
    "limits",
    "model_route",
    "unresolved",
    "nodes",
    "policy",
    "recovery",
    "host_context_requirements",
    "host_context_semantics",
}
_DIGEST_SKIP = {"revision", "content_digest", "status"}
_TEACHER = {
    "effect_observed",
    "domain_goal_verified",
    "domain_goal_failed",
    "semantic_judgment_unverified",
    "not_measured",
}
_INPUT_TYPES = {"string", "integer", "number", "boolean", "object", "array"}
_ABSTAIN_REASONS = {
    "ambiguous",
    "missing_context",
    "unsupported",
    "conflicting_evidence",
}
_NODE_KINDS = {"read", "assert", "decide", "effect"}
_FORBIDDEN_BINDING_KEYS = {"eval", "shell", "env", "exec", "code", "python"}


def canonical_dumps(value: Any) -> str:
    _validate_json(value)
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )


def _validate_json(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise WorkflowError("INPUT_INVALID", "non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise WorkflowError("WORKFLOW_INVALID", "JSON object keys must be strings")
            _validate_json(item)
        return
    raise WorkflowError("WORKFLOW_INVALID", "value is not finite JSON")


def json_pointer_get(doc: Any, pointer: str) -> Any:
    if not isinstance(pointer, str):
        raise WorkflowError("BINDING_INVALID", "JSON Pointer must be a string")
    if pointer == "":
        return doc
    if not pointer.startswith("/"):
        raise WorkflowError("BINDING_INVALID", "JSON Pointer must start with /")
    current = doc
    for raw in pointer.split("/")[1:]:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if part not in current:
                raise WorkflowError("BINDING_MISSING", f"missing pointer segment {part}")
            current = current[part]
        elif isinstance(current, list):
            if not part.isdigit():
                raise WorkflowError("BINDING_INVALID", "array index must be digits")
            idx = int(part)
            if idx >= len(current):
                raise WorkflowError("BINDING_MISSING", "array index out of range")
            current = current[idx]
        else:
            raise WorkflowError("BINDING_MISSING", "cannot traverse pointer")
    return current


def workflow_digest(doc: dict) -> str:
    if not isinstance(doc, dict):
        raise WorkflowError("WORKFLOW_INVALID", "workflow must be an object")
    body = {k: copy.deepcopy(v) for k, v in doc.items() if k not in _DIGEST_SKIP}
    digest = hashlib.sha256(canonical_dumps(body).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def parse_json_result(result: Any) -> Any:
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, RecursionError):
            return result
        if isinstance(parsed, (dict, list)):
            return parsed
    return result


def _require_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise WorkflowError("WORKFLOW_INVALID", f"invalid {label}")
    return value


def _is_binding(value: Any) -> bool:
    return isinstance(value, dict) and any(k in value for k in _BINDING_TAGS)


def _validate_binding(binding: Any, *, allowed_nodes: set[str], self_effect: str | None) -> None:
    if not isinstance(binding, dict):
        raise WorkflowError("WORKFLOW_INVALID", "expected tagged binding")
    if any(k in binding for k in _FORBIDDEN_BINDING_KEYS):
        raise WorkflowError("WORKFLOW_INVALID", "eval/shell/env bindings are forbidden")
    tags = [k for k in _BINDING_TAGS if k in binding]
    if len(tags) != 1:
        raise WorkflowError("WORKFLOW_INVALID", "binding must have exactly one tag")
    tag = tags[0]
    extra = set(binding) - {tag, "pointer"}
    if tag in ("node", "effect_intent"):
        extra = set(binding) - {tag, "pointer"}
        if extra:
            raise WorkflowError("WORKFLOW_INVALID", "unexpected binding field")
        if "pointer" not in binding or not isinstance(binding["pointer"], str):
            raise WorkflowError("WORKFLOW_INVALID", "node/effect_intent requires pointer")
        target = binding[tag]
        if not isinstance(target, str) or not target:
            raise WorkflowError("WORKFLOW_INVALID", "invalid binding target")
        if tag == "node":
            if target not in allowed_nodes:
                raise WorkflowError("WORKFLOW_INVALID", "forward or unknown node reference")
        elif target != self_effect and target not in allowed_nodes:
            raise WorkflowError("WORKFLOW_INVALID", "unknown effect_intent reference")
        return
    if extra or "pointer" in binding:
        raise WorkflowError("WORKFLOW_INVALID", "unexpected binding field")
    if tag == "literal":
        _validate_json(binding["literal"])
        return
    if not isinstance(binding[tag], str) or not binding[tag].startswith("/"):
        raise WorkflowError("WORKFLOW_INVALID", f"{tag} binding must be a JSON Pointer")


def _validate_args(args: Any, *, allowed_nodes: set[str], self_effect: str | None) -> None:
    if not isinstance(args, dict) or not args:
        raise WorkflowError("WORKFLOW_INVALID", "node args must be a non-empty object")
    for key, value in args.items():
        if not isinstance(key, str) or not key:
            raise WorkflowError("WORKFLOW_INVALID", "invalid arg name")
        _validate_binding(value, allowed_nodes=allowed_nodes, self_effect=self_effect)


def _validate_predicate(pred: Any, *, allowed_nodes: set[str], self_effect: str | None) -> None:
    if not isinstance(pred, dict) or "op" not in pred:
        raise WorkflowError("WORKFLOW_INVALID", "invalid predicate")
    op = pred["op"]
    if op in {"and", "or"}:
        checks = pred.get("checks")
        if not isinstance(checks, list) or not checks:
            raise WorkflowError("WORKFLOW_INVALID", "compound predicate needs checks")
        for item in checks:
            _validate_predicate(item, allowed_nodes=allowed_nodes, self_effect=self_effect)
        return
    if op == "not":
        _validate_predicate(
            pred.get("check"), allowed_nodes=allowed_nodes, self_effect=self_effect
        )
        return
    if op == "exists":
        _validate_binding(
            pred.get("value"), allowed_nodes=allowed_nodes, self_effect=self_effect
        )
        return
    if op not in {"eq", "ne", "lt", "le", "gt", "ge", "in"}:
        raise WorkflowError("WORKFLOW_INVALID", f"unsupported predicate op {op}")
    _validate_binding(pred.get("left"), allowed_nodes=allowed_nodes, self_effect=self_effect)
    _validate_binding(pred.get("right"), allowed_nodes=allowed_nodes, self_effect=self_effect)


def validate_workflow(doc: dict) -> dict:
    if not isinstance(doc, dict):
        raise WorkflowError("WORKFLOW_INVALID", "workflow must be an object")
    unknown = set(doc) - _ALLOWED_TOP
    if unknown:
        raise WorkflowError("WORKFLOW_INVALID", f"unknown workflow fields {sorted(unknown)}")
    wf = copy.deepcopy(doc)
    if wf.get("format") != "proof.workflow/v1":
        raise WorkflowError("WORKFLOW_INVALID", "format must be proof.workflow/v1")
    _require_name(wf.get("workflow_id", ""), "workflow_id")
    if "revision" in wf and wf["revision"] is not None:
        if type(wf["revision"]) is not int or wf["revision"] < 0:
            raise WorkflowError("WORKFLOW_INVALID", "invalid revision")
    wf.setdefault("status", "candidate")
    if wf["status"] not in {"candidate", "promoted"}:
        raise WorkflowError("WORKFLOW_INVALID", "invalid status")
    provenance = wf.get("provenance")
    if not isinstance(provenance, dict):
        raise WorkflowError("WORKFLOW_INVALID", "provenance required")
    assurance = provenance.get("teacher_outcome_assurance")
    if assurance not in _TEACHER:
        raise WorkflowError("WORKFLOW_INVALID", "invalid teacher_outcome_assurance")
    if type(provenance.get("actual_demonstration_receipt")) is not bool:
        raise WorkflowError("WORKFLOW_INVALID", "actual_demonstration_receipt must be bool")
    refs = provenance.get("source_receipt_refs", [])
    if not isinstance(refs, list):
        raise WorkflowError("WORKFLOW_INVALID", "source_receipt_refs must be a list")
    provenance.setdefault("source_receipt_refs", refs)
    inputs = wf.get("inputs")
    if not isinstance(inputs, dict):
        raise WorkflowError("WORKFLOW_INVALID", "inputs must be an object")
    for name, spec in inputs.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            raise WorkflowError("WORKFLOW_INVALID", "invalid input spec")
        if spec.get("type") not in _INPUT_TYPES:
            raise WorkflowError("WORKFLOW_INVALID", f"invalid input type for {name}")
        if "pattern" in spec and not isinstance(spec["pattern"], str):
            raise WorkflowError("WORKFLOW_INVALID", "input pattern must be a string")
    wf.setdefault("constants", {})
    if not isinstance(wf["constants"], dict):
        raise WorkflowError("WORKFLOW_INVALID", "constants must be an object")
    _validate_json(wf["constants"])
    authority = wf.get("authority")
    if not isinstance(authority, dict) or not isinstance(authority.get("tools"), list):
        raise WorkflowError("WORKFLOW_INVALID", "authority.tools required")
    tools = authority["tools"]
    if any(not isinstance(t, str) or not t for t in tools):
        raise WorkflowError("WORKFLOW_INVALID", "invalid authority tool")
    adapter_id = wf.get("adapter_id")
    if not isinstance(adapter_id, str) or not adapter_id:
        raise WorkflowError("WORKFLOW_INVALID", "adapter_id required")
    limits = wf.get("limits")
    if not isinstance(limits, dict):
        raise WorkflowError("WORKFLOW_INVALID", "limits required")
    if limits.get("max_effects") != 1:
        raise WorkflowError("WORKFLOW_INVALID", "max_effects must be 1")
    if limits.get("max_model_calls") not in (0, 1):
        raise WorkflowError("WORKFLOW_INVALID", "max_model_calls must be 0 or 1")
    if limits.get("provider_fallback") not in (False, None):
        raise WorkflowError("WORKFLOW_INVALID", "provider fallback is forbidden")
    wf.setdefault("unresolved", [])
    if not isinstance(wf["unresolved"], list):
        raise WorkflowError("WORKFLOW_INVALID", "unresolved must be a list")
    nodes = wf.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise WorkflowError("WORKFLOW_INVALID", "nodes required")
    ids: list[str] = []
    decide_n = 0
    effect_n = 0
    allowed: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or node.get("kind") not in _NODE_KINDS:
            raise WorkflowError("WORKFLOW_INVALID", "invalid node kind")
        nid = node.get("id")
        if not isinstance(nid, str) or not _NODE_ID_RE.fullmatch(nid) or nid in ids:
            raise WorkflowError("WORKFLOW_INVALID", "invalid or duplicate node id")
        kind = node["kind"]
        self_effect = nid if kind == "effect" else None
        if kind in {"read", "effect"}:
            tool = node.get("tool")
            if not isinstance(tool, str) or tool not in tools:
                raise WorkflowError("WORKFLOW_INVALID", f"tool {tool!r} not in authority")
            _validate_args(node.get("args"), allowed_nodes=allowed, self_effect=self_effect)
        if kind == "read":
            fields = node.get("required_fields", [])
            if fields is not None and (
                not isinstance(fields, list) or any(not isinstance(f, str) for f in fields)
            ):
                raise WorkflowError("WORKFLOW_INVALID", "invalid required_fields")
        elif kind == "assert":
            checks = node.get("checks")
            if not isinstance(checks, list) or not checks:
                raise WorkflowError("WORKFLOW_INVALID", "assert requires checks")
            for check in checks:
                _validate_predicate(
                    check, allowed_nodes=allowed, self_effect=self_effect
                )
        elif kind == "decide":
            decide_n += 1
            _validate_binding(
                node.get("source"), allowed_nodes=allowed, self_effect=None
            )
            cands = node.get("candidates")
            if not isinstance(cands, list) or not cands or any(
                not isinstance(c, str) or not c for c in cands
            ):
                raise WorkflowError("WORKFLOW_INVALID", "decide candidates required")
            queues = authority.get("queue_candidates")
            if isinstance(queues, list) and any(c not in queues for c in cands):
                raise WorkflowError("WORKFLOW_INVALID", "candidate not in authority")
        elif kind == "effect":
            effect_n += 1
            identity = node.get("operation_identity")
            if not isinstance(identity, dict) or not isinstance(
                identity.get("namespace"), str
            ):
                raise WorkflowError("WORKFLOW_INVALID", "operation_identity required")
            parts = identity.get("parts")
            if not isinstance(parts, list) or not parts:
                raise WorkflowError("WORKFLOW_INVALID", "operation_identity.parts required")
            for part in parts:
                _validate_binding(part, allowed_nodes=allowed, self_effect=self_effect)
            resource = node.get("resource")
            if not isinstance(resource, dict):
                raise WorkflowError("WORKFLOW_INVALID", "effect resource required")
            for field in ("tenant", "target", "action"):
                _validate_binding(
                    resource.get(field), allowed_nodes=allowed, self_effect=self_effect
                )
        ids.append(nid)
        allowed.add(nid)
    if effect_n > 1 or decide_n > 1:
        raise WorkflowError("WORKFLOW_INVALID", "at most one effect and one decide")
    if decide_n == 1:
        if limits.get("max_model_calls") != 1 or not isinstance(wf.get("model_route"), dict):
            raise WorkflowError("WORKFLOW_INVALID", "decide requires model_route")
    else:
        if limits.get("max_model_calls") != 0 or wf.get("model_route") is not None:
            raise WorkflowError("WORKFLOW_INVALID", "exact workflow forbids model_route")
    wf["content_digest"] = workflow_digest(wf)
    _validate_json(wf)
    return wf


def validate_inputs(workflow: dict, inputs: dict) -> dict:
    if not isinstance(inputs, dict):
        raise WorkflowError("INPUT_INVALID", "inputs must be an object")
    spec = workflow.get("inputs") or {}
    if set(inputs) - set(spec):
        raise WorkflowError("INPUT_INVALID", "unexpected input")
    out = {}
    for name, schema in spec.items():
        required = schema.get("required", True)
        if name not in inputs:
            if required:
                raise WorkflowError("INPUT_INVALID", f"missing input {name}")
            continue
        value = inputs[name]
        expected = schema["type"]
        if expected == "string":
            if type(value) is not str:
                raise WorkflowError("INPUT_INVALID", f"{name} must be a string")
            pattern = schema.get("pattern")
            if pattern and not re.fullmatch(pattern, value):
                raise WorkflowError("INPUT_INVALID", f"{name} does not match pattern")
        elif expected == "integer":
            if type(value) is not int:
                raise WorkflowError("INPUT_INVALID", f"{name} must be an integer")
        elif expected == "number":
            if type(value) not in (int, float) or type(value) is bool:
                raise WorkflowError("INPUT_INVALID", f"{name} must be a number")
            if type(value) is float and not math.isfinite(value):
                raise WorkflowError("INPUT_INVALID", "non-finite number")
        elif expected == "boolean":
            if type(value) is not bool:
                raise WorkflowError("INPUT_INVALID", f"{name} must be a boolean")
        elif expected == "object":
            if not isinstance(value, dict):
                raise WorkflowError("INPUT_INVALID", f"{name} must be an object")
        elif expected == "array":
            if not isinstance(value, list):
                raise WorkflowError("INPUT_INVALID", f"{name} must be an array")
        try:
            _validate_json(value)
        except WorkflowError as exc:
            raise WorkflowError("INPUT_INVALID", exc.message) from exc
        out[name] = copy.deepcopy(value)
    return out


def validate_decision(
    decision: Any,
    *,
    candidates: list,
    evidence_ids: list,
    allow_abstain: bool = True,
) -> dict:
    if not isinstance(decision, dict):
        raise WorkflowError("MODEL_OUTPUT_INVALID", "decision must be an object")
    kind = decision.get("kind")
    if kind == "select":
        extra = set(decision) - {"kind", "queue", "evidence_ids"}
        if extra:
            raise WorkflowError("MODEL_OUTPUT_INVALID", "unexpected decision field")
        queue = decision.get("queue")
        ev = decision.get("evidence_ids")
        if queue not in candidates:
            raise WorkflowError("MODEL_OUTPUT_INVALID", "queue not in candidates")
        if not isinstance(ev, list) or not ev:
            raise WorkflowError("MODEL_OUTPUT_INVALID", "evidence_ids required")
        if len(set(ev)) != len(ev):
            raise WorkflowError("MODEL_OUTPUT_INVALID", "evidence_ids must be unique")
        allowed = set(evidence_ids)
        if any(item not in allowed for item in ev):
            raise WorkflowError("MODEL_OUTPUT_INVALID", "unknown evidence id")
        return copy.deepcopy(decision)
    if kind == "abstain":
        extra = set(decision) - {"kind", "reason"}
        if extra:
            raise WorkflowError("MODEL_OUTPUT_INVALID", "unexpected decision field")
        if not allow_abstain:
            raise WorkflowError("MODEL_OUTPUT_INVALID", "abstain is not allowed")
        if decision.get("reason") not in _ABSTAIN_REASONS:
            raise WorkflowError("MODEL_OUTPUT_INVALID", "invalid abstain reason")
        return copy.deepcopy(decision)
    raise WorkflowError("MODEL_OUTPUT_INVALID", "decision kind must be select or abstain")


def resolve_binding(binding: dict, ctx: dict) -> Any:
    if not isinstance(binding, dict):
        raise WorkflowError("BINDING_INVALID", "expected tagged binding")
    if any(k in binding for k in _FORBIDDEN_BINDING_KEYS):
        raise WorkflowError("BINDING_INVALID", "eval/shell/env bindings are forbidden")
    tags = [k for k in _BINDING_TAGS if k in binding]
    if len(tags) != 1:
        raise WorkflowError("BINDING_INVALID", "binding must have exactly one tag")
    tag = tags[0]
    if tag == "literal":
        if set(binding) != {"literal"}:
            raise WorkflowError("BINDING_INVALID", "unexpected binding field")
        value = binding["literal"]
        _validate_json(value)
        return copy.deepcopy(value)
    if tag == "input":
        if set(binding) != {"input"}:
            raise WorkflowError("BINDING_INVALID", "unexpected binding field")
        return copy.deepcopy(json_pointer_get(ctx.get("inputs") or {}, binding["input"]))
    if tag == "constant":
        if set(binding) != {"constant"}:
            raise WorkflowError("BINDING_INVALID", "unexpected binding field")
        return copy.deepcopy(
            json_pointer_get(ctx.get("constants") or {}, binding["constant"])
        )
    if tag == "host_context":
        if set(binding) != {"host_context"}:
            raise WorkflowError("BINDING_INVALID", "unexpected binding field")
        return copy.deepcopy(
            json_pointer_get(ctx.get("host_context") or {}, binding["host_context"])
        )
    if tag == "node":
        if set(binding) != {"node", "pointer"}:
            raise WorkflowError("BINDING_INVALID", "node binding requires pointer")
        nid = binding["node"]
        nodes = ctx.get("nodes") or {}
        if nid not in nodes:
            raise WorkflowError("BINDING_MISSING", f"node {nid} is not completed")
        return copy.deepcopy(json_pointer_get(nodes[nid], binding["pointer"]))
    if set(binding) != {"effect_intent", "pointer"}:
        raise WorkflowError("BINDING_INVALID", "effect_intent binding requires pointer")
    eid = binding["effect_intent"]
    intents = ctx.get("effect_intents") or {}
    if eid not in intents:
        raise WorkflowError("BINDING_MISSING", f"effect intent {eid} is not persisted")
    return copy.deepcopy(json_pointer_get(intents[eid], binding["pointer"]))


def bind_args(args_spec: dict, ctx: dict, *, skip_self_intent: str | None = None) -> dict:
    bound = {}
    for key, spec in args_spec.items():
        if (
            skip_self_intent
            and isinstance(spec, dict)
            and spec.get("effect_intent") == skip_self_intent
        ):
            continue
        bound[key] = resolve_binding(spec, ctx)
    return bound


def eval_predicate(pred: dict, ctx: dict) -> bool:
    op = pred["op"]
    if op == "and":
        return all(eval_predicate(item, ctx) for item in pred["checks"])
    if op == "or":
        return any(eval_predicate(item, ctx) for item in pred["checks"])
    if op == "not":
        return not eval_predicate(pred["check"], ctx)
    if op == "exists":
        try:
            resolve_binding(pred["value"], ctx)
            return True
        except WorkflowError:
            return False
    try:
        left = resolve_binding(pred["left"], ctx)
        right = resolve_binding(pred["right"], ctx)
    except WorkflowError:
        return False
    if op == "eq":
        try:
            return canonical_dumps(left) == canonical_dumps(right)
        except WorkflowError:
            return False
    if op == "ne":
        try:
            return canonical_dumps(left) != canonical_dumps(right)
        except WorkflowError:
            return False
    if op == "in":
        return isinstance(right, list) and left in right
    if op in {"lt", "le", "gt", "ge"}:
        if type(left) is bool or type(right) is bool:
            return False
        if type(left) not in (int, float) or type(right) not in (int, float):
            return False
        if not math.isfinite(left) or not math.isfinite(right):
            return False
        if op == "lt":
            return left < right
        if op == "le":
            return left <= right
        if op == "gt":
            return left > right
        return left >= right
    return False


def compute_operation_key(spec: dict, ctx: dict) -> str:
    parts = [resolve_binding(part, ctx) for part in spec["parts"]]
    body = canonical_dumps({"namespace": spec["namespace"], "parts": parts})
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"{spec['namespace']}:{digest}"


def compute_resource_key(spec: dict, ctx: dict) -> str:
    return canonical_dumps(
        {
            "tenant": resolve_binding(spec["tenant"], ctx),
            "target": resolve_binding(spec["target"], ctx),
            "action": resolve_binding(spec["action"], ctx),
        }
    )


def segment_evidence(text: Any) -> list[dict]:
    if not isinstance(text, str):
        text = canonical_dumps(text)
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    if not parts:
        parts = [text]
    return [{"id": f"s{i + 1}", "text": part} for i, part in enumerate(parts[:3])]
