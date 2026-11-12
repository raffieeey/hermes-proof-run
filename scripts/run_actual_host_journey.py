#!/usr/bin/env python3
"""Actual-host golden path: registered /proof command + public execute_checked.

Teacher: scripted operator through the real checked SDK (not a frontier demonstration).
Follower: deterministic no-model control using the reviewed policy.
Requires a verified Hermes source tree. This is not a 1B quality claim.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "proof-run"
sys.path.insert(0, str(PLUGIN))

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
        }
    ],
}


def _parse(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-source", dest="host", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, default=PLUGIN)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--keep-dir", type=Path)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse(argv)
    host = args.host.resolve()
    if not (host / "hermes_cli" / "plugin_command_dispatch.py").is_file():
        print(json.dumps({"ok": False, "error": "host source is missing plugin_command_dispatch.py"}))
        return 2
    sys.path.insert(0, str(host))
    os.environ.setdefault("HERMES_PLUGIN_HOST_PROCESS", "1")
    scratch = Path(tempfile.gettempdir())
    keep = args.keep_dir or Path(tempfile.mkdtemp(prefix="proof-run-actual-host-", dir=scratch))
    keep.mkdir(parents=True, exist_ok=True)
    home = keep / "hermes-home"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(
        json.dumps(
            {
                "plugins": {
                    "enabled": ["proof-run"],
                    "isolation": "in_process",
                    "hook_callback_timeout": 0,
                }
            }
        )
    )
    os.environ["HERMES_HOME"] = str(home)
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli import plugins
    from hermes_cli.plugin_command_dispatch import invoke_plugin_command
    from hermes_cli.plugins import PluginContext
    from hermes_cli.plugins_manifest import parse_manifest_file
    from plugins.plugin_storage import plugin_data_dir

    from proof_run import example_ticket

    token = set_hermes_home_override(home)
    manager = None
    outcome = {
        "ok": False,
        "host_boundary": "actual-public-SDK",
        "teacher": "scripted-operator",
        "teacher_note": "Not a frontier-model demonstration.",
        "follower": "deterministic-no-model",
        "claim_ceiling": "Actual registered-command mechanics only; no 1B or frontier claim.",
    }
    try:
        manager = plugins.PluginManager()
        manifest = parse_manifest_file(args.plugin / "plugin.yaml", args.plugin, "user", "")
        if manifest is None:
            raise RuntimeError("Cannot parse real manifest")
        manager._load_plugin(manifest)
        manager._discovered = True
        plugins._plugin_manager = manager
        loaded = manager._plugins.get("proof-run")
        if loaded is None or not loaded.enabled:
            raise RuntimeError(f"plugin load failed: {getattr(loaded, 'error', None)}")
        proof = plugins.get_plugin_command_handler("proof")
        if proof is None:
            raise RuntimeError("proof command was not registered")
        ctx = PluginContext(loaded.manifest, manager)
        root = plugin_data_dir("proof-run")
        example_ticket.enable_example(root)
        tickets_path = root / "tickets.sqlite"
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
        registered = example_ticket.register_example_ticket_tools(
            ctx, tickets, opt_in=True, tenant_id="tenant-A"
        )
        if not registered:
            raise RuntimeError("example ticket tools were not registered")

        session = "sess-1"

        def command(raw: str, session_id: str = session):
            return invoke_plugin_command(
                proof,
                raw,
                session_id=session_id,
                session_key=f"actual-{session_id}",
                platform="cli",
                source=None,
            )

        def teach(raw_args, session_id=session, **_kwargs):
            get_args = {"ticket_id": "T-21"}
            first = ctx.execute_checked("example_ticket_get", get_args, session_id=session_id)
            got = json.loads(first["result"])
            route_args = {
                "tenant_id": got["tenant_id"],
                "ticket_id": got["ticket_id"],
                "expected_source_revision": got["source_revision"],
                "expected_routing_revision": got["routing_revision"],
                "queue": "billing",
                "operation_key": "route-ticket-v1:demo",
            }
            routed = ctx.execute_checked(
                "example_ticket_route_if_version", route_args, session_id=session_id
            )
            after = ctx.execute_checked("example_ticket_get", get_args, session_id=session_id)
            return json.dumps(
                {
                    "first": first.get("disposition"),
                    "routed": routed.get("disposition"),
                    "after": after.get("disposition"),
                    "dispatched": [
                        first.get("dispatched"),
                        routed.get("dispatched"),
                        after.get("dispatched"),
                    ],
                }
            )

        handle = ctx.register_command("proof-example-teach", teach)
        if handle is None:
            raise RuntimeError("could not register example teach command")
        teach_handler = manager._plugin_commands["proof-example-teach"]["handler"]

        started = command("capture start route-ticket teach once")
        taught = invoke_plugin_command(
            teach_handler,
            "",
            session_id=session,
            session_key=f"actual-{session}",
            platform="cli",
            source=None,
        )
        finished = command("capture finish")
        prepared = command("prepare route-ticket " + json.dumps(REVIEW_TICKET))
        inspected = command("inspect route-ticket")
        promoted = command("promote route-ticket 1")

        async def _run_jobs():
            first = command('run route-ticket {"ticket_id": "T-21"}')
            second = command('run route-ticket {"ticket_id": "T-22"}')
            pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            if pending:
                await asyncio.wait(pending, timeout=60)
            return first, second

        run21, run22 = asyncio.run(_run_jobs())
        t21 = tickets.get("T-21", tenant_id="tenant-A")
        t22 = tickets.get("T-22", tenant_id="tenant-A")
        visible = all(
            name in str(inspected)
            for name in (
                "example_ticket_get",
                "example_ticket_route_if_version",
            )
        )
        outcome.update(
            {
                "ok": (
                    "capture" in str(started).lower()
                    and "route-ticket" in str(finished)
                    and "candidate" in str(prepared).lower()
                    and visible
                    and "Promoted" in str(promoted)
                    and "R-" in str(run21)
                    and "R-" in str(run22)
                    and t21.get("queue") == "billing"
                    and t22.get("queue") == "access"
                    and json.loads(taught)["dispatched"] == [True, True, True]
                ),
                "started": started,
                "taught": taught,
                "finished": finished,
                "prepared": prepared,
                "inspected": inspected,
                "promoted": promoted,
                "run_T21": run21,
                "run_T22": run22,
                "target_readback": {"T-21": t21.get("queue"), "T-22": t22.get("queue")},
                "handles": {
                    "keep_dir": str(keep),
                    "profile_root": str(root),
                    "run_store": str(root / "runner" / "runner.sqlite"),
                    "target_store": str(tickets_path),
                },
            }
        )
    except Exception as exc:
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if manager is not None:
                manager.unload()
        except Exception:
            pass
        reset_hermes_home_override(token)
    encoded = json.dumps(outcome, indent=2, sort_keys=True) + "\n"
    print(encoded, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    return 0 if outcome.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
