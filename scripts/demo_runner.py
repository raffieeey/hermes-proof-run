#!/usr/bin/env python3
"""Golden-path Proof Run operator journey (scripted teacher, real stores).

Teacher: scripted-operator (not a frontier-model demonstration).
Follower: deterministic no-model control using the reviewed policy.
Host boundary: labeled INTERFACES mock, not a live Hermes execute_checked kernel.

Writes a machine-readable JSON outcome plus independent target/run-store paths.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "proof-run"
sys.path.insert(0, str(PLUGIN))

from proof_run import capture, compiler, example_ticket, follower  # noqa: E402
from proof_run.engine import WorkflowRunner  # noqa: E402
from proof_run.store import WorkflowStore  # noqa: E402


def _receipt(call_id, *, phase, tool, args, result=None, session="sess-demo", profile="demo-profile"):
    dispatched = phase == "terminal"
    return {
        "schema": "ExecutionReceipt.v1",
        "phase": phase,
        "call_id": call_id,
        "profile_id": profile,
        "session_id": session,
        "run_id": "",
        "tool_name": tool,
        "proposed_args": dict(args),
        "final_args": dict(args),
        "dispatched": dispatched,
        "disposition": "completed" if dispatched else "prepared",
        "result": result,
        "operation_identity": "",
        "policy_ref": "",
        "receipt_persisted": True,
    }


class DemoHost:
    """Labeled INTERFACES mock used by this script. Not live Hermes."""

    def __init__(self, tickets: example_ticket.TicketStore, store: WorkflowStore, world: dict):
        self.tickets = tickets
        self.store = store
        self.world = world
        self.session_id = "sess-demo"
        self.profile_id = "demo-profile"
        self.tenant_id = "tenant-A"
        self.seq = 0
        self.effect_count = 0
        self.read_count = 0

    def capabilities(self):
        return {
            "version": "checked-execution/v1",
            "final_authorization": True,
            "mandatory_receipts": True,
            "bound_context": True,
            "plugin_host_supported": False,
        }

    def authenticated_context(self):
        return {
            "profile_id": self.profile_id,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "routing_intent_epoch": "initial",
        }

    def progress(self, message, *, run_context):
        return None

    def operation_status(self, intent, *, run_context):
        key = intent["operation_key"]
        if key in self.world.get("operations", {}):
            op = self.world["operations"][key]
            return {"state": op["state"], "payload": op["payload"], "operation_key": key}
        return self.tickets.operation_status(key)

    def execute_checked(self, tool_name, args, *, run_context):
        self.seq += 1
        call_id = f"demo-{self.seq:04d}"
        if tool_name in {"example_ticket_route_if_version", "deploy_service"}:
            self.store.admit_dispatch(run_context, tool_name, args)
            self.effect_count += 1
        else:
            self.read_count += 1
        if tool_name == "example_ticket_get":
            result = self.tickets.get(args["ticket_id"], tenant_id=self.tenant_id)
        elif tool_name == "example_ticket_route_if_version":
            result = self.tickets.route_if_version(args, tenant_id=self.tenant_id)
        elif tool_name == "example_ticket_operation_status":
            result = self.tickets.operation_status(args["operation_key"])
        elif tool_name == "check_service_health":
            result = dict(self.world["deploy"])
        elif tool_name == "deploy_service":
            self.world["deploy"] = {
                "service": args["service"],
                "status": "healthy",
                "version": args["version"],
            }
            key = args.get("operation_key")
            self.world.setdefault("operations", {})[key] = {
                "state": "committed",
                "payload": dict(args),
            }
            result = {"ok": True, "version": args["version"]}
        else:
            raise KeyError(tool_name)
        rec = {
            "schema": "ExecutionReceipt.v1",
            "phase": "terminal",
            "call_id": call_id,
            "profile_id": self.profile_id,
            "session_id": self.session_id,
            "run_id": run_context.get("run_id", ""),
            "tool_name": tool_name,
            "proposed_args": dict(args),
            "final_args": dict(args),
            "dispatched": True,
            "disposition": "completed",
            "result": json.dumps(result),
            "operation_identity": (run_context.get("intent_ref") or {}).get("operation_key", ""),
            "policy_ref": "",
            "receipt_persisted": True,
        }
        self.store.record_receipt(rec, trace_id=run_context.get("run_id", ""))
        return rec


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
        "provider": "deterministic",
        "model": "no-model",
        "fallback": False,
        "inference": False,
    },
    "decision": {
        "candidates": ["billing", "access", "incident"],
        "source_tool": "example_ticket_get",
        "source_field": "body",
        "allow_abstain": True,
    },
    "bindings": {"example_ticket_get.ticket_id": {"input": "/ticket_id"}},
    "teacher_outcome_assurance": "effect_observed",
    "promotion_authority": "operator-reviewed",
    "idempotency": {"namespace": "route-ticket-v1"},
    "goal_predicates": [
        {
            "op": "eq",
            "left": {"node": "after", "pointer": "/queue"},
            "right": {"effect_intent": "route", "pointer": "/payload/queue"},
        },
        {
            "op": "eq",
            "left": {"node": "after", "pointer": "/last_routing_operation"},
            "right": {"effect_intent": "route", "pointer": "/operation_key"},
        },
    ],
}

REVIEW_EXACT = {
    "inputs": {},
    "constants": {"service": "fixture-api", "version": "v1"},
    "authority": {
        "tools": ["check_service_health", "deploy_service"],
        "arbitrary_tool_calls": False,
    },
    "adapter_id": "fixture-release-v1",
    "policy": {"revision": "exact-1", "source": "operator-reviewed-exact-job", "rules": {}},
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

ACTOR = {"id": "demo-operator", "profile_id": "demo-profile", "session_id": "sess-demo"}


def teach_ticket(root: Path, store: WorkflowStore) -> dict:
    capture.start_trace(
        root, name="route-ticket", session_id="sess-demo", profile_id="demo-profile", intent="billing demo"
    )
    got = {
        "tenant_id": "tenant-A",
        "ticket_id": "T-21",
        "source_revision": 4,
        "routing_revision": 7,
        "body": "I was charged twice this month for the same invoice.",
        "queue": "unassigned",
        "last_routing_operation": None,
    }
    routed = {
        "tenant_id": "tenant-A",
        "ticket_id": "T-21",
        "expected_source_revision": 4,
        "expected_routing_revision": 7,
        "queue": "billing",
        "operation_key": "demo-route",
    }
    after = dict(got)
    after["queue"] = "billing"
    after["last_routing_operation"] = "demo-route"
    after["routing_revision"] = 8
    steps = [
        ("t-get-1", "example_ticket_get", {"ticket_id": "T-21"}, json.dumps(got)),
        ("t-route-1", "example_ticket_route_if_version", routed, json.dumps({"ok": True})),
        ("t-get-2", "example_ticket_get", {"ticket_id": "T-21"}, json.dumps(after)),
    ]
    for call_id, tool, args, result in steps:
        capture.ingest_receipt(
            root,
            _receipt(call_id, phase="before_dispatch", tool=tool, args=args),
            session_id="sess-demo",
            profile_id="demo-profile",
            store=store,
        )
        capture.ingest_receipt(
            root,
            _receipt(call_id, phase="terminal", tool=tool, args=args, result=result),
            session_id="sess-demo",
            profile_id="demo-profile",
            store=store,
        )
    return capture.finish_trace(root, session_id="sess-demo", store=store)


def teach_exact(root: Path, store: WorkflowStore) -> dict:
    capture.start_trace(
        root,
        name="ensure-fixture-release",
        session_id="sess-demo",
        profile_id="demo-profile",
    )
    health = json.dumps({"service": "fixture-api", "status": "healthy", "version": "v0"})
    deploy = {"service": "fixture-api", "version": "v1"}
    after = json.dumps({"service": "fixture-api", "status": "healthy", "version": "v1"})
    steps = [
        ("e-h1", "check_service_health", {"service": "fixture-api"}, health),
        ("e-d1", "deploy_service", deploy, json.dumps({"ok": True})),
        ("e-h2", "check_service_health", {"service": "fixture-api"}, after),
    ]
    for call_id, tool, args, result in steps:
        capture.ingest_receipt(
            root,
            _receipt(call_id, phase="before_dispatch", tool=tool, args=args),
            session_id="sess-demo",
            profile_id="demo-profile",
            store=store,
        )
        capture.ingest_receipt(
            root,
            _receipt(call_id, phase="terminal", tool=tool, args=args, result=result),
            session_id="sess-demo",
            profile_id="demo-profile",
            store=store,
        )
    return capture.finish_trace(root, session_id="sess-demo", store=store)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path)
    parser.add_argument("--keep-dir", type=Path)
    parser.add_argument(
        "--actual-host",
        action="store_true",
        help="Use the verified Hermes public SDK via scripts/run_actual_host_journey.py",
    )
    parser.add_argument("--host-source", type=Path)
    args = parser.parse_args(argv)
    if args.actual_host:
        if args.host_source is None:
            print("ERROR: --actual-host requires --host-source /path/to/hermes-agent", file=sys.stderr)
            return 2
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from run_actual_host_journey import main as actual_main

        extra = ["--host-source", str(args.host_source)]
        if args.out:
            extra.extend(["--out", str(args.out)])
        if args.keep_dir:
            extra.extend(["--keep-dir", str(args.keep_dir)])
        return actual_main(extra)
    keep = args.keep_dir or Path(tempfile.mkdtemp(prefix="proof-run-demo-"))
    keep.mkdir(parents=True, exist_ok=True)
    root = keep / "profile"
    root.mkdir(exist_ok=True)
    tickets_path = keep / "tickets.sqlite"
    tickets = example_ticket.TicketStore(tickets_path)
    tickets.put_ticket(
        {
            "tenant_id": "tenant-A",
            "ticket_id": "T-21",
            "source_revision": 4,
            "routing_revision": 7,
            "body": "I was charged twice this month for the same invoice.",
            "queue": "unassigned",
            "last_routing_operation": None,
        }
    )
    tickets.put_ticket(
        {
            "tenant_id": "tenant-A",
            "ticket_id": "T-22",
            "source_revision": 1,
            "routing_revision": 1,
            "body": "Only I cannot sign in after replacing my phone.",
            "queue": "unassigned",
            "last_routing_operation": None,
        }
    )
    store = WorkflowStore(root)
    host = DemoHost(tickets, store, {"deploy": {"service": "fixture-api", "status": "healthy", "version": "v0"}, "operations": {}})
    trace = teach_ticket(root, store)
    candidate = compiler.compile_from_trace(trace, compiler.DEFAULT_CATALOG, REVIEW_TICKET)
    stored = store.put_candidate(candidate["workflow_id"], candidate)
    promoted = store.promote(stored["workflow_id"], stored["revision"], ACTOR)
    det = follower.DeterministicFollower()
    runner = WorkflowRunner(store, host, det)
    run_det = store.create_run("route-ticket", {"ticket_id": "T-21"}, ACTOR)
    out_det = runner.drive(run_det["run_id"])
    run21 = out_det
    run22 = store.create_run("route-ticket", {"ticket_id": "T-22"}, ACTOR)
    out22 = runner.drive(run22["run_id"])
    out21 = run21
    exact_trace = teach_exact(root, store)
    exact_wf = compiler.compile_from_trace(exact_trace, compiler.DEFAULT_CATALOG, REVIEW_EXACT)
    exact_stored = store.put_candidate(exact_wf["workflow_id"], exact_wf)
    store.promote(exact_stored["workflow_id"], exact_stored["revision"], ACTOR)
    exact_run = store.create_run("ensure-fixture-release", {}, ACTOR)
    exact_out = WorkflowRunner(store, host, None).drive(exact_run["run_id"])
    outcome = {
        "ok": out21.get("state") in {"completed", "verified"}
        and out22.get("state") in {"completed", "verified"}
        and exact_out.get("state") in {"completed", "verified"}
        and tickets.get("T-21", tenant_id="tenant-A")["queue"] == "billing"
        and tickets.get("T-22", tenant_id="tenant-A")["queue"] == "access"
        and host.world["deploy"]["version"] == "v1",
        "teacher": "scripted-operator",
        "teacher_note": "Not a frontier-model demonstration.",
        "follower": "deterministic-no-model using reviewed policy.rules",
        "deterministic_control": {
            "run_id": out_det["run_id"],
            "state": out_det["state"],
            "semantic_judgment": out_det.get("semantic_judgment"),
            "note": "Deterministic control uses reviewed packet.policy.rules; it does not invent a rubric.",
        },
        "host_boundary": "labeled-INTERFACES-mock",
        "semantic": {
            "T-21": {
                "run_id": out21["run_id"],
                "state": out21["state"],
                "queue": tickets.get("T-21", tenant_id="tenant-A")["queue"],
                "effect_readback": out21.get("effect_readback"),
                "semantic_judgment": out21.get("semantic_judgment"),
                "whole_goal": out21.get("whole_goal"),
            },
            "T-22": {
                "run_id": out22["run_id"],
                "state": out22["state"],
                "queue": tickets.get("T-22", tenant_id="tenant-A")["queue"],
                "effect_readback": out22.get("effect_readback"),
                "semantic_judgment": out22.get("semantic_judgment"),
                "whole_goal": out22.get("whole_goal"),
            },
        },
        "exact": {
            "run_id": exact_out["run_id"],
            "state": exact_out["state"],
            "version": host.world["deploy"]["version"],
            "effect_readback": exact_out.get("effect_readback"),
            "whole_goal": exact_out.get("whole_goal"),
        },
        "handles": {
            "keep_dir": str(keep),
            "profile_root": str(root),
            "run_store": str(root / "runner" / "runner.sqlite"),
            "target_store": str(tickets_path),
            "promoted_revision": promoted.get("revision"),
        },
        "claim_ceiling": "Synthetic target mechanics and standing-review reuse only; no frontier parity or savings claim.",
    }
    encoded = json.dumps(outcome, indent=2, sort_keys=True) + "\n"
    print(encoded, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
