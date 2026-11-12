"""Operator-lane test helpers. INTERFACES host boundary is mocked and labeled.

These fixtures are not a live Hermes host, not inference, and not a frontier
teacher demonstration.
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

from proof_run.store import WorkflowStore


def receipt(
    call_id: str,
    *,
    phase: str = "terminal",
    tool_name: str = "example_ticket_get",
    args: dict | None = None,
    result: Any = None,
    session_id: str = "sess-1",
    profile_id: str = "test-profile",
    run_id: str = "",
    dispatched: bool | None = None,
    disposition: str | None = None,
    **extra: Any,
) -> dict:
    args = copy.deepcopy(args or {"ticket_id": "T-21"})
    if dispatched is None:
        dispatched = phase == "terminal"
    if disposition is None:
        disposition = "completed" if phase == "terminal" else "prepared"
    if result is None and phase == "terminal":
        result = json.dumps({"ok": True, "ticket_id": args.get("ticket_id", "T-21")})
    rec = {
        "schema": "ExecutionReceipt.v1",
        "phase": phase,
        "call_id": call_id,
        "profile_id": profile_id,
        "session_id": session_id,
        "run_id": run_id,
        "tool_name": tool_name,
        "proposed_args": copy.deepcopy(args),
        "final_args": copy.deepcopy(args),
        "dispatched": dispatched,
        "disposition": disposition,
        "result": result,
        "operation_identity": extra.get("operation_identity", ""),
        "policy_ref": extra.get("policy_ref", ""),
        "receipt_persisted": True,
    }
    rec.update(extra)
    return rec


def ticket_get_result(
    ticket_id: str = "T-21",
    *,
    body: str = "I was charged twice this month for the same invoice.",
    tenant_id: str = "tenant-A",
    source_revision: int = 4,
    routing_revision: int = 7,
    queue: str = "unassigned",
) -> dict:
    return {
        "tenant_id": tenant_id,
        "ticket_id": ticket_id,
        "source_revision": source_revision,
        "routing_revision": routing_revision,
        "body": body,
        "queue": queue,
        "last_routing_operation": None,
    }


def teaching_ticket_trace_receipts(
    *,
    session_id: str = "sess-1",
    profile_id: str = "test-profile",
    ticket_id: str = "T-21",
) -> list[dict]:
    """Scripted operator teacher receipts. Not a frontier-model demonstration."""
    got = ticket_get_result(ticket_id)
    routed = {
        "tenant_id": "tenant-A",
        "ticket_id": ticket_id,
        "expected_source_revision": 4,
        "expected_routing_revision": 7,
        "queue": "billing",
        "operation_key": "route-ticket-v1:demo",
    }
    after = ticket_get_result(ticket_id, queue="billing")
    after["last_routing_operation"] = "route-ticket-v1:demo"
    after["routing_revision"] = 8
    items = [
        ("c-get-1", "example_ticket_get", {"ticket_id": ticket_id}, json.dumps(got)),
        (
            "c-route-1",
            "example_ticket_route_if_version",
            routed,
            json.dumps({"ok": True, "queue": "billing"}),
        ),
        ("c-get-2", "example_ticket_get", {"ticket_id": ticket_id}, json.dumps(after)),
    ]
    out = []
    for call_id, tool, args, result in items:
        out.append(
            receipt(
                call_id,
                phase="before_dispatch",
                tool_name=tool,
                args=args,
                session_id=session_id,
                profile_id=profile_id,
                result=None,
            )
        )
        out.append(
            receipt(
                call_id,
                phase="terminal",
                tool_name=tool,
                args=args,
                result=result,
                session_id=session_id,
                profile_id=profile_id,
            )
        )
    return out


REVIEW_TICKET = {
    "inputs": {"ticket_id": {"type": "string", "pattern": "^T-[0-9]{1,8}$"}},
    "constants": {},
    "authority": {
        "tenant": "host-verified-context",
        "tools": [
            "example_ticket_get",
            "example_ticket_route_if_version",
            "example_ticket_operation_status",
        ],
        "queue_candidates": ["billing", "access", "incident"],
        "effects": "internal-ticket-routing-only",
        "arbitrary_tool_calls": False,
    },
    "adapter_id": "example-ticket-v1",
    "policy": {
        "revision": "routing-policy-1",
        "source": "operator-reviewed-domain-policy-not-inferred-from-one-demo",
        "rules": {
            "billing": "charges, invoices, duplicate payments or subscription fees",
            "access": "an individual user cannot sign in or recover access",
            "incident": "a shared service outage affecting multiple users",
        },
        "ambiguous": "abstain",
    },
    "model_route": {
        "provider": "controlled-test",
        "model": "scripted-follower",
        "fallback": False,
        "inference": False,
    },
    "decision": {
        "candidates": ["billing", "access", "incident"],
        "source_tool": "example_ticket_get",
        "source_field": "body",
        "allow_abstain": True,
    },
    "bindings": {
        "example_ticket_get.ticket_id": {"input": "/ticket_id"},
        "example_ticket_route_if_version.ticket_id": {
            "node": "ticket",
            "pointer": "/ticket_id",
        },
        "example_ticket_route_if_version.queue": {"node": "queue", "pointer": "/queue"},
    },
    "teacher_outcome_assurance": "effect_observed",
    "promotion_authority": "operator-reviewed",
    "idempotency": {
        "namespace": "route-ticket-v1",
        "cas": ["source_revision", "routing_revision"],
    },
    "goal_predicates": [
        {
            "op": "eq",
            "left": {"node": "after", "pointer": "/queue"},
            "right": {"effect_intent": "route", "pointer": "/payload/queue"},
        }
    ],
}


ADAPTER_CATALOG = {
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
    },
    "deploy_service": {"kind": "effect", "node_id": "deploy"},
    "deploy_operation_status": {"kind": "status"},
}


REVIEW_EXACT = {
    "inputs": {},
    "constants": {"service": "fixture-api", "version": "v1"},
    "authority": {
        "tenant": "host-verified-context",
        "tools": ["check_service_health", "deploy_service"],
        "effects": "internal-fixture-deploy-only",
        "arbitrary_tool_calls": False,
    },
    "adapter_id": "fixture-release-v1",
    "policy": {
        "revision": "exact-1",
        "source": "operator-reviewed-exact-job",
        "rules": {},
    },
    "model_route": None,
    "teacher_outcome_assurance": "effect_observed",
    "promotion_authority": "operator-reviewed",
    "idempotency": {"namespace": "ensure-fixture-release-v1"},
    "bindings": {
        "check_service_health.service": {"constant": "/service"},
        "deploy_service.service": {"constant": "/service"},
        "deploy_service.version": {"constant": "/version"},
    },
}


class FakeCtx:
    """Labeled INTERFACES mock of PluginContext. Not a live Hermes host."""

    def __init__(self, home: Path, *, profile_id="test-profile", session_id="sess-1"):
        self.home = Path(home)
        self.profile_id = profile_id
        self.session_id = session_id
        self.tenant_id = "tenant-A"
        self.routing_intent_epoch = "initial"
        self.commands = {}
        self.hooks = []
        self.authorizers = []
        self.sinks = []
        self.tools = {}
        self.spawned = []
        self.progress_messages = []
        self.execute_calls = []
        self.dispatch_calls = []
        self.capabilities_override = None
        self.checked_enabled = True
        self._proof_run_state = {
            "lock": threading.RLock(),
            "recorders": {},
            "runtime": {},
            "buffers": {},
            "pending": {},
            "unproven": set(),
            "checked_frames": {},
            "authorizer_guard": "unattempted",
        }
        self.store = None

    @property
    def profile_name(self) -> str:
        return self.profile_id

    def session_surface(self) -> str:
        return "cli"

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = {
            "handler": handler,
            "description": description,
            "args_hint": args_hint,
        }

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def checked_execution_capabilities(self):
        if self.capabilities_override is not None:
            return copy.deepcopy(self.capabilities_override)
        if not self.checked_enabled:
            return {
                "version": "checked-execution/v1",
                "final_authorization": False,
                "mandatory_receipts": False,
                "bound_context": False,
                "plugin_host_supported": False,
                "refusal_reason": "checked execution disabled in labeled mock",
            }
        return {
            "version": "checked-execution/v1",
            "final_authorization": True,
            "mandatory_receipts": True,
            "bound_context": True,
            "plugin_host_supported": False,
        }

    def register_tool_authorizer(self, callback):
        self.authorizers.append(callback)

    def register_execution_receipt_sink(self, callback):
        self.sinks.append(callback)

    def spawn_task(self, coro, *, name=None):
        self.spawned.append({"name": name})
        if hasattr(coro, "close"):
            coro.close()
        return {"name": name, "scheduled": True}

    def execute_checked(self, tool_name, proposed_args, **kwargs):
        self.execute_calls.append(
            {"tool": tool_name, "args": copy.deepcopy(proposed_args), "kwargs": kwargs}
        )
        if tool_name in self.tools:
            result = self.tools[tool_name](copy.deepcopy(proposed_args), **kwargs)
        else:
            result = json.dumps({"ok": True, "tool": tool_name})
        rec = receipt(
            f"host-{len(self.execute_calls):04d}",
            tool_name=tool_name,
            args=proposed_args,
            result=result if isinstance(result, str) else json.dumps(result),
            session_id=kwargs.get("session_id") or self.session_id,
            profile_id=self.profile_id,
            run_id=kwargs.get("run_id", ""),
            operation_identity=kwargs.get("operation_identity", ""),
        )
        for sink in self.sinks:
            sink(rec, phase="terminal")
        return rec

    def dispatch_tool(self, tool_name, args, **kwargs):
        self.dispatch_calls.append({"tool": tool_name, "args": copy.deepcopy(args)})
        raise AssertionError("dispatch_tool must not be used as a runner fallback")


def attach_store(ctx: FakeCtx, root: Path) -> WorkflowStore:
    ctx.store = WorkflowStore(root)
    return ctx.store
