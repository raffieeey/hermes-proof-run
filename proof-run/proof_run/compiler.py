"""Compile a proof.workflow/v1 candidate from teaching receipts plus review.

Exact order and arguments come from terminal receipts. Typed inputs, bindings,
authority, decision rubric/route and goal checks come only from the explicit
review bundle. One example does not invent extra branches or numeric ranges.
Teacher assurance is preserved and never upgraded by this compiler.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from . import capture
from .workflow import WorkflowError, parse_json_result, validate_workflow

_PREPARE_FIELDS = {
    "inputs",
    "bindings",
    "constants",
    "authority",
    "adapter_id",
    "policy",
    "model_route",
    "decision",
    "teacher_outcome_assurance",
    "promotion_authority",
    "idempotency",
    "goal_predicates",
    "limits",
    "recovery",
    "host_context_requirements",
    "unresolved",
    "identity_predicates",
    "effects",
}

DEFAULT_CATALOG = {
    "example_ticket_get": {
        "kind": "read",
        "required_fields": [
            "tenant_id",
            "ticket_id",
            "source_revision",
            "body",
            "queue",
            "last_routing_operation",
            "routing_revision",
        ],
        "node_id": "ticket",
        "identity_checks": True,
    },
    "example_ticket_route_if_version": {
        "kind": "effect",
        "node_id": "route",
        "status_tool": "example_ticket_operation_status",
    },
    "example_ticket_operation_status": {"kind": "status"},
    "check_service_health": {
        "kind": "read",
        "required_fields": ["service", "status", "version"],
        "node_id": "health",
        "identity_checks": False,
    },
    "deploy_service": {"kind": "effect", "node_id": "deploy"},
    "deploy_operation_status": {"kind": "status"},
}

_ASSURANCE_RANK = {
    "domain_goal_failed": 0,
    "not_measured": 1,
    "semantic_judgment_unverified": 2,
    "effect_observed": 3,
    "domain_goal_verified": 4,
}


def reject_unknown_review_fields(bundle: dict) -> None:
    if not isinstance(bundle, dict):
        raise WorkflowError("INPUT_INVALID", "review bundle must be an object")
    unknown = set(bundle) - _PREPARE_FIELDS
    if unknown:
        raise WorkflowError(
            "INPUT_INVALID", f"unknown review fields {sorted(unknown)}"
        )


def _successful_terminals(trace: dict) -> list[dict]:
    receipts = list(trace.get("receipts") or [])
    ordered = []
    seen = set()
    for rec in receipts:
        if rec.get("phase") != "terminal":
            continue
        call_id = rec.get("call_id")
        key = (call_id, rec.get("phase"))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(rec)
    return ordered


def _tool_steps(trace: dict) -> list[dict]:
    steps = []
    for rec in _successful_terminals(trace):
        ok = bool(rec.get("dispatched")) and rec.get("disposition") == "completed"
        steps.append(
            {
                "call_id": rec.get("call_id"),
                "tool": rec.get("tool_name"),
                "args": copy.deepcopy(rec.get("final_args") or rec.get("proposed_args") or {}),
                "result": rec.get("result"),
                "ok": ok,
                "receipt": rec,
            }
        )
    return steps


def _preserve_assurance(trace: dict, review: dict) -> str:
    source = trace.get("teacher_outcome_assurance") or "not_measured"
    offered = review.get("teacher_outcome_assurance")
    if offered is None:
        return source
    if _ASSURANCE_RANK.get(offered, 0) > _ASSURANCE_RANK.get(source, 0):
        return source
    return source


def _binding_for(tool: str, arg: str, value: Any, review: dict) -> dict:
    bindings = review.get("bindings") or {}
    key = f"{tool}.{arg}"
    if key in bindings:
        return copy.deepcopy(bindings[key])
    constants = review.get("constants") or {}
    for cname, cval in constants.items():
        if cval == value:
            return {"constant": f"/{cname}"}
    inputs = review.get("inputs") or {}
    if arg in inputs:
        return {"input": f"/{arg}"}
    return {"literal": copy.deepcopy(value)}


def compile_from_trace(trace: dict, adapter_catalog: dict, review_bundle: dict) -> dict:
    if not isinstance(trace, dict):
        raise WorkflowError("DEMO_EVIDENCE_INCOMPLETE", "trace required")
    review = copy.deepcopy(review_bundle or {})
    if review:
        reject_unknown_review_fields(review)
    catalog = copy.deepcopy(DEFAULT_CATALOG)
    if adapter_catalog:
        for key, meta in adapter_catalog.items():
            if isinstance(meta, dict):
                catalog[key] = {**catalog.get(key, {}), **meta}
            else:
                catalog[key] = meta
    steps = _tool_steps(trace)
    unresolved: list[dict] = []
    incomplete = capture.incomplete_call_ids(trace) if isinstance(trace, dict) else ["trace"]
    if not trace.get("complete") or incomplete:
        unresolved.append(
            {
                "code": "DEMO_EVIDENCE_INCOMPLETE",
                "message": "every expected teaching attempt needs a successful terminal receipt",
            }
        )
    if not steps or not any(s["ok"] for s in steps):
        if not any(item.get("code") == "DEMO_EVIDENCE_INCOMPLETE" for item in unresolved):
            unresolved.append(
                {
                    "code": "DEMO_EVIDENCE_INCOMPLETE",
                    "message": "no successful terminal teaching receipts",
                }
            )
    name = trace.get("name") or "unnamed"
    authority = copy.deepcopy(review.get("authority") or {})
    tools = list(authority.get("tools") or [])
    adapter_id = review.get("adapter_id") or ""
    if not tools:
        unresolved.append({"code": "REVIEW_REQUIRED", "message": "authority.tools required"})
        tools = [s["tool"] for s in steps if s.get("tool")]
        tools = list(dict.fromkeys(tools))
        authority["tools"] = tools
    if not adapter_id:
        unresolved.append({"code": "REVIEW_REQUIRED", "message": "adapter_id required"})
        adapter_id = "unspecified-adapter"
    decision = review.get("decision")
    queues = None
    if isinstance(authority.get("queue_candidates"), list):
        queues = list(authority["queue_candidates"])
    if decision and isinstance(decision.get("candidates"), list):
        queues = list(decision["candidates"])
    demonstrated_queues = []
    for step in steps:
        if catalog.get(step["tool"], {}).get("kind") == "effect":
            q = (step.get("args") or {}).get("queue")
            if isinstance(q, str):
                demonstrated_queues.append(q)
    if decision is None and queues is None and demonstrated_queues:
        unresolved.append(
            {
                "code": "REVIEW_REQUIRED",
                "message": "decision candidates/queues are not inferred from one example",
            }
        )
        queues = list(dict.fromkeys(demonstrated_queues))
    nodes: list[dict] = []
    used_ids: set[str] = set()
    read_count = 0
    effect_count = 0
    first_read_id = None
    effect_id = None
    decide_added = False
    effect_index = None

    def _nid(base: str) -> str:
        candidate = base
        n = 2
        while candidate in used_ids:
            candidate = f"{base}_{n}"
            n += 1
        used_ids.add(candidate)
        return candidate

    for step in steps:
        if not step["ok"]:
            unresolved.append(
                {
                    "code": "DEMO_EVIDENCE_INCOMPLETE",
                    "message": f"teaching call {step.get('call_id')} is not a successful terminal receipt",
                }
            )
            continue
        tool = step["tool"]
        meta = catalog.get(tool) or {}
        kind = meta.get("kind")
        if kind == "status":
            continue
        if kind not in {"read", "effect"}:
            unresolved.append(
                {
                    "code": "REVIEW_REQUIRED",
                    "message": f"tool {tool!r} is not in the adapter catalog",
                }
            )
            continue
        if tool not in tools:
            unresolved.append(
                {
                    "code": "POLICY_CONFLICT",
                    "message": f"tool {tool!r} is not in reviewed authority",
                }
            )
        args = {}
        for key, value in (step.get("args") or {}).items():
            if key == "operation_key":
                continue
            bound = _binding_for(tool, key, value, review)
            if bound.get("literal") is not None and review.get("inputs") and key in (review.get("inputs") or {}):
                bound = {"input": f"/{key}"}
            args[key] = bound
        if kind == "read":
            read_count += 1
            preferred = "ticket" if tool == "example_ticket_get" and "ticket" not in used_ids else meta.get("node_id") or "read"
            if tool == "example_ticket_get" and "ticket" in used_ids:
                preferred = "after"
            elif tool == "check_service_health" and "health_before" not in used_ids:
                preferred = "health_before"
            elif tool == "check_service_health":
                preferred = "health_after"
            nid = _nid(preferred)
            node = {
                "id": nid,
                "kind": "read",
                "tool": tool,
                "args": args or {"_": {"literal": True}},
            }
            fields = meta.get("required_fields")
            if fields:
                node["required_fields"] = list(fields)
            if first_read_id is None:
                first_read_id = nid
            if read_count > 1 and effect_count:
                node["freshness"] = "fresh-independent-after-effect"
            nodes.append(node)
            if (
                meta.get("identity_checks")
                and first_read_id == nid
                and (review.get("inputs") or review.get("identity_predicates"))
            ):
                checks = review.get("identity_predicates") or [
                    {
                        "op": "eq",
                        "left": {"node": nid, "pointer": "/tenant_id"},
                        "right": {"host_context": "/tenant_id"},
                    },
                    {
                        "op": "eq",
                        "left": {"node": nid, "pointer": "/ticket_id"},
                        "right": {"input": "/ticket_id"},
                    },
                ]
                nodes.append(
                    {
                        "id": _nid("identity"),
                        "kind": "assert",
                        "checks": checks,
                        "on_failure": "TARGET_IDENTITY_MISMATCH",
                    }
                )
        elif kind == "effect":
            effect_count += 1
            nid = _nid(meta.get("node_id") or "effect")
            effect_id = nid
            identity = review.get("idempotency") or {}
            namespace = identity.get("namespace") or f"{name}-v1"
            parts = []
            if "ticket_id" in (review.get("inputs") or {}):
                parts = [
                    {"node": first_read_id or nid, "pointer": "/tenant_id"},
                    {"node": first_read_id or nid, "pointer": "/ticket_id"},
                    {"node": first_read_id or nid, "pointer": "/source_revision"},
                    {"host_context": "/routing_intent_epoch"},
                ]
            else:
                constants = review.get("constants") or {}
                for cname in constants:
                    parts.append({"constant": f"/{cname}"})
                parts.append({"host_context": "/routing_intent_epoch"})
                if not constants:
                    parts = [{"literal": namespace}]
            args["operation_key"] = {"effect_intent": nid, "pointer": "/operation_key"}
            if first_read_id and tool == "example_ticket_route_if_version":
                args["tenant_id"] = {"node": first_read_id, "pointer": "/tenant_id"}
                args["ticket_id"] = {"node": first_read_id, "pointer": "/ticket_id"}
                args["expected_source_revision"] = {
                    "node": first_read_id,
                    "pointer": "/source_revision",
                }
                args["expected_routing_revision"] = {
                    "node": first_read_id,
                    "pointer": "/routing_revision",
                }
                if queues or decision:
                    args["queue"] = {"node": "queue", "pointer": "/queue"}
            resource = {
                "tenant": {"host_context": "/tenant_id"},
                "target": (
                    {"input": "/ticket_id"}
                    if "ticket_id" in (review.get("inputs") or {})
                    else {"constant": "/service"}
                    if "service" in (review.get("constants") or {})
                    else {"literal": name}
                ),
                "action": {
                    "literal": "route" if "route" in nid else "deploy"
                },
            }
            nodes.append(
                {
                    "id": nid,
                    "kind": "effect",
                    "tool": tool,
                    "args": args,
                    "operation_identity": {"namespace": namespace, "parts": parts},
                    "resource": resource,
                }
            )
            effect_index = len(nodes) - 1

    if effect_index is not None and (decision or queues):
        if not decision:
            unresolved.append(
                {
                    "code": "REVIEW_REQUIRED",
                    "message": "reviewed decision rubric required; not inferred from one example",
                }
            )
        candidates = (decision or {}).get("candidates") or queues or demonstrated_queues
        if decision and queues is None and demonstrated_queues:
            if list(candidates) == demonstrated_queues:
                pass
        source_tool = (decision or {}).get("source_tool") or "example_ticket_get"
        source_field = (decision or {}).get("source_field") or "body"
        source_node = first_read_id or "ticket"
        decide_node = {
            "id": "queue",
            "kind": "decide",
            "source": {"node": source_node, "pointer": f"/{source_field}"},
            "candidates": list(candidates),
            "output": "queue-decision/v1",
            "allow_abstain": bool((decision or {}).get("allow_abstain", True)),
            "on_abstain": "needs_review",
        }
        nodes.insert(effect_index, decide_node)
        used_ids.add("queue")
        decide_added = True
        if not (decision and (decision.get("candidates") or authority.get("queue_candidates"))):
            if decide_node["candidates"] == demonstrated_queues:
                unresolved.append(
                    {
                        "code": "REVIEW_REQUIRED",
                        "message": "authorized queue set must be reviewed, not copied from one billing example",
                    }
                )

    if first_read_id and not decide_added and not (review.get("inputs") or {}):
        pre_checks = review.get("preflight_predicates") or []
        if not pre_checks and "health_before" in used_ids:
            pre_checks = [
                {
                    "op": "eq",
                    "left": {"node": "health_before", "pointer": "/status"},
                    "right": {"literal": "healthy"},
                },
                {
                    "op": "eq",
                    "left": {"node": "health_before", "pointer": "/service"},
                    "right": {"constant": "/service"},
                },
            ]
        if pre_checks:
            insert_at = 1 if nodes and nodes[0]["kind"] == "read" else 0
            nodes.insert(
                insert_at,
                {
                    "id": _nid("preflight"),
                    "kind": "assert",
                    "checks": pre_checks,
                    "on_failure": "STATE_DRIFT",
                },
            )

    goal_predicates = review.get("goal_predicates") or []
    if not goal_predicates and effect_id and "health_after" in used_ids:
        goal_predicates = [
            {
                "op": "eq",
                "left": {"node": "health_after", "pointer": "/service"},
                "right": {"constant": "/service"},
            },
            {
                "op": "eq",
                "left": {"node": "health_after", "pointer": "/status"},
                "right": {"literal": "healthy"},
            },
            {
                "op": "eq",
                "left": {"node": "health_after", "pointer": "/version"},
                "right": {"effect_intent": effect_id, "pointer": "/payload/version"},
            },
        ]
    if goal_predicates:
        nodes.append({"id": _nid("goal"), "kind": "assert", "checks": goal_predicates})
    elif effect_id:
        unresolved.append(
            {"code": "REVIEW_REQUIRED", "message": "independent goal predicates required"}
        )

    if not review:
        unresolved.append(
            {"code": "REVIEW_REQUIRED", "message": "explicit review bundle required"}
        )

    refs = []
    for rec in _successful_terminals(trace):
        if rec.get("call_id") and rec.get("dispatched") and rec.get("disposition") == "completed":
            refs.append({"call_id": rec["call_id"]})
    actual = bool(refs) and not any(
        item.get("code") == "DEMO_EVIDENCE_INCOMPLETE" for item in unresolved
    )
    assurance = _preserve_assurance(trace, review)
    limits = copy.deepcopy(
        review.get("limits")
        or {
            "serial_effects": True,
            "max_effects": 1,
            "max_model_calls": 1 if decide_added else 0,
            "on_unknown": "stop-and-reconcile",
            "provider_fallback": False,
        }
    )
    if decide_added:
        limits["max_model_calls"] = 1
        model_route = copy.deepcopy(review.get("model_route"))
        if not isinstance(model_route, dict):
            unresolved.append({"code": "MODEL_ROUTE_REQUIRED", "message": "decide requires model_route"})
            model_route = {
                "provider": "unspecified",
                "model": "unspecified",
                "fallback": False,
                "inference": False,
            }
    else:
        limits["max_model_calls"] = 0
        model_route = None
        if review.get("model_route") not in (None, {}):
            if review.get("model_route"):
                unresolved.append(
                    {"code": "REVIEW_REQUIRED", "message": "exact workflow forbids model_route"}
                )
    policy = copy.deepcopy(review.get("policy") or {})
    if decide_added and not policy.get("rules"):
        unresolved.append(
            {
                "code": "REVIEW_REQUIRED",
                "message": "reviewed policy.rules required; compiler will not invent a rubric",
            }
        )
    wf = {
        "format": "proof.workflow/v1",
        "workflow_id": name,
        "status": "candidate",
        "provenance": {
            "kind": "captured-trace",
            "actual_demonstration_receipt": actual,
            "teacher_outcome_assurance": assurance,
            "source_receipt_refs": refs,
            "promotion_authority": review.get("promotion_authority") or "operator-reviewed",
            "source": trace.get("source") or "captured-teaching-trace",
        },
        "inputs": copy.deepcopy(review.get("inputs") or {}),
        "constants": copy.deepcopy(review.get("constants") or {}),
        "authority": authority,
        "adapter_id": adapter_id,
        "limits": limits,
        "model_route": model_route,
        "unresolved": unresolved,
        "nodes": nodes or [
            {
                "id": "placeholder",
                "kind": "assert",
                "checks": [
                    {"op": "eq", "left": {"literal": 1}, "right": {"literal": 0}}
                ],
            }
        ],
        "policy": policy,
    }
    if review.get("recovery"):
        wf["recovery"] = copy.deepcopy(review["recovery"])
    if review.get("host_context_requirements"):
        wf["host_context_requirements"] = copy.deepcopy(review["host_context_requirements"])
    try:
        return validate_workflow(wf)
    except WorkflowError:
        wf["unresolved"] = list(unresolved) + [
            {"code": "WORKFLOW_INVALID", "message": "candidate did not pass static validation"}
        ]
        wf["status"] = "candidate"
        return wf


def compile_schema2_import(contract: dict, review_bundle: dict | None = None) -> dict:
    review = copy.deepcopy(review_bundle or {})
    name = contract.get("contract_id") or "imported"
    wf = {
        "format": "proof.workflow/v1",
        "workflow_id": name,
        "status": "candidate",
        "provenance": {
            "kind": "schema2-import",
            "actual_demonstration_receipt": False,
            "teacher_outcome_assurance": "not_measured",
            "source_receipt_refs": [],
            "promotion_authority": "none",
        },
        "inputs": copy.deepcopy(review.get("inputs") or {}),
        "constants": copy.deepcopy(review.get("constants") or {}),
        "authority": copy.deepcopy(
            review.get("authority")
            or {"tools": ["check_service_health", "deploy_service"], "arbitrary_tool_calls": False}
        ),
        "adapter_id": review.get("adapter_id") or "schema2-import",
        "limits": {
            "serial_effects": True,
            "max_effects": 1,
            "max_model_calls": 0,
            "on_unknown": "stop-and-reconcile",
            "provider_fallback": False,
        },
        "model_route": None,
        "unresolved": [
            {
                "code": "DEMO_EVIDENCE_INCOMPLETE",
                "message": "schema-2 import has no authoritative execution receipts and is not runnable",
            }
        ],
        "nodes": [
            {
                "id": "blocked_import",
                "kind": "assert",
                "checks": [
                    {"op": "eq", "left": {"literal": True}, "right": {"literal": False}}
                ],
            }
        ],
        "policy": copy.deepcopy(review.get("policy") or {}),
    }
    return wf
