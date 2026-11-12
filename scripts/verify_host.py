"""Behavior probes through the real Hermes plugin host interfaces.

No provider calls, no installs, no live profile state. A failing probe reports a
product defect, not a harness success. Synthetic tools only; no model-learning or production-outcome claim.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

PARSER = argparse.ArgumentParser()
PARSER.add_argument(
    "--area", default="all", choices=["all", "host", "outcomes", "state", "actions"]
)
PARSER.add_argument(
    "--plugin", type=Path, default=Path(__file__).resolve().parents[1] / "proof-run"
)
PARSER.add_argument("--host-source", dest="host", type=Path, required=True)
PARSER.add_argument("--output", type=Path)
ARGS = PARSER.parse_args()
SCRATCH = Path(tempfile.gettempdir())
BOOT = tempfile.TemporaryDirectory(prefix="proof-run-audit-bootstrap-", dir=SCRATCH)
os.environ["HERMES_HOME"] = BOOT.name
os.environ["HERMES_PLUGIN_HOST_PROCESS"] = "1"
sys.path.insert(0, str(ARGS.host))
NETWORK_ATTEMPTS = []


def deny_network(event, payload):
    if event == "socket.connect":
        NETWORK_ATTEMPTS.append(repr(payload))
        raise RuntimeError("Network forbidden during local Proof Run audit")


sys.addaudithook(deny_network)
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from hermes_cli import plugins
from hermes_cli.plugins_manifest import parse_manifest_file
from hermes_cli.plugin_command_dispatch import invoke_plugin_command

RESULTS = []


@contextlib.contextmanager
def host():
    """Use a real loader and real context, not the historical FakeState/Ctx."""
    with tempfile.TemporaryDirectory(prefix="proof-run-audit-home-", dir=SCRATCH) as td:
        home = Path(td)
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
        token = set_hermes_home_override(home)
        old_env = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = td
        manager = plugins.PluginManager()
        manifest = parse_manifest_file(
            ARGS.plugin / "plugin.yaml", ARGS.plugin, "user", ""
        )
        if manifest is None:
            raise RuntimeError("Cannot parse real manifest")
        manager._load_plugin(manifest)
        manager._discovered = True
        plugins._plugin_manager = manager
        loaded = manager._plugins.get("proof-run")
        if loaded is None or not loaded.enabled:
            raise RuntimeError(
                f"Real plugin load failed: {getattr(loaded, 'error', None)}"
            )
        try:
            yield Harness(home, manager)
        finally:
            manager.unload()
            reset_hermes_home_override(token)
            if old_env is not None:
                os.environ["HERMES_HOME"] = old_env


class Harness:
    def __init__(self, home, manager):
        self.home, self.manager = home, manager

    def command(self, raw, session="A"):
        handler = plugins.get_plugin_command_handler("proof")
        assert handler is not None
        return invoke_plugin_command(
            handler,
            raw,
            session_id=session,
            session_key=f"audit-{session}",
            platform="local",
            source=None,
        )

    def pre(self, tool, args, session="A"):
        message, modified = plugins._dispatch_pre_tool_call_hooks(
            tool, args, session_id=session, tool_call_id=uuid.uuid4().hex
        )
        return {
            "blocked": message is not None,
            "message": message,
            "modified": modified,
        }

    def post(self, tool, args, result, session="A", **metadata):
        # Actual host normalization/emitter, including host-only status metadata.
        from model_tools import _emit_post_tool_call_hook

        _emit_post_tool_call_hook(
            function_name=tool,
            function_args=args,
            result=json.dumps(result) if isinstance(result, dict) else result,
            session_id=session,
            tool_call_id=uuid.uuid4().hex,
            **metadata,
        )

    def contract(self, name):
        return json.loads(
            (
                self.home / "plugin-data" / "proof-run" / "contracts" / f"{name}.json"
            ).read_text()
        )

    def demonstrate(self, name="deploy", session="A"):
        self.command(f"start {name}", session)
        self.post(
            "check_service_health", {"service": "api"}, {"status": "healthy"}, session
        )
        self.post(
            "deploy_service",
            {"service": "api", "version": "2.1"},
            {"deployed": True},
            session,
        )
        return self.command("stop", session)


def probe(area, name, criterion, function):
    if ARGS.area not in ("all", area):
        return
    try:
        expected, observed, passed = function()
        entry = {
            "area": area,
            "id": name,
            "criterion": criterion,
            "passed": bool(passed),
            "expected": expected,
            "observed": observed,
        }
    except Exception as exc:
        entry = {
            "area": area,
            "id": name,
            "criterion": criterion,
            "passed": False,
            "harness_error": f"{type(exc).__name__}: {exc}",
        }
    RESULTS.append(entry)
    print(json.dumps(entry, sort_keys=True), flush=True)


def lifecycle():
    with host() as h:
        unrelated = h.pre("read_file", {"path": "synthetic"})
        reply = h.demonstrate()
        contract = h.contract("deploy")
        h.post("check_service_health", {"service": "api"}, {"status": "healthy"})
        bad = h.pre("deploy_service", {"service": "worker", "version": "2.1"})
        good = h.pre("deploy_service", {"service": "api", "version": "2.1"})
        h.command("unbind deploy")
        unbound = h.pre("deploy_service", {"service": "worker", "version": "2.1"})
        observed = {
            "reply": reply,
            "bound": contract["bound"],
            "unrelated": unrelated,
            "bad": bad,
            "good": good,
            "unbound": unbound,
        }
        return (
            "real loader+command+JSON-string hooks create and enforce a live contract; unrelated/unbound calls pass",
            observed,
            contract["bound"]
            and bad["blocked"]
            and not good["blocked"]
            and not unrelated["blocked"]
            and not unbound["blocked"],
        )


def learning():
    with host() as h:
        h.post("check_service_health", {"service": "api"}, {"status": "healthy"})
        h.post(
            "deploy_service", {"service": "api", "version": "2.1"}, {"deployed": True}
        )
        reply = h.command("learn learned 2")
        unknown_other_session = h.pre(
            "deploy_service", {"service": "api", "version": "2.1"}, "B"
        )
        return (
            "learn uses session A buffer, session B has unknown preconditions",
            {"reply": reply, "session_B": unknown_other_session},
            "live" in reply and unknown_other_session["blocked"],
        )


def registry_flow():
    with host() as h:
        from model_tools import handle_function_call
        from tools.registry import registry

        calls = []
        world = {"status": "healthy"}

        def health(args, **kwargs):
            return json.dumps(world)

        def deploy(args, **kwargs):
            calls.append(dict(args))
            return json.dumps({"deployed": True})

        for name, callback in [
            ("check_service_health", health),
            ("deploy_service", deploy),
        ]:
            registry.register(
                name=name,
                toolset="proof-run-audit",
                schema={
                    "name": name,
                    "description": "Synthetic audit tool; no external action",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "service": {"type": "string"},
                            "version": {"type": "string"},
                        },
                    },
                },
                handler=callback,
                override=True,
            )

        def call(name, args):
            return handle_function_call(
                name, args, session_id="A", tool_call_id=uuid.uuid4().hex
            )

        h.command("start actual-registry")
        call("check_service_health", {"service": "api"})
        call("deploy_service", {"service": "api", "version": "2.1"})
        stopped = h.command("stop")
        count_after_record = len(calls)
        call("check_service_health", {"service": "api"})
        allowed = call("deploy_service", {"service": "api", "version": "2.1"})
        count_after_allow = len(calls)
        world["status"] = "unhealthy"
        call("check_service_health", {"service": "api"})
        blocked = call("deploy_service", {"service": "api", "version": "2.1"})
        return (
            "real handle_function_call runs matching mutation and stops drift before handler execution",
            {
                "stop": stopped,
                "allowed": allowed,
                "blocked": blocked,
                "counts": [count_after_record, count_after_allow, len(calls)],
            },
            count_after_record == 1
            and count_after_allow == 2
            and len(calls) == 2
            and "current unhealthy" in blocked,
        )


def rebind_flow():
    with host() as h:
        h.demonstrate()
        reply = h.command("rebind deploy")
        h.post("check_service_health", {"service": "api"}, {"status": "healthy"})
        h.post(
            "deploy_service", {"service": "api", "version": "2.2"}, {"deployed": True}
        )
        stopped = h.command("stop")
        c = h.contract("deploy")
        h.post("check_service_health", {"service": "api"}, {"status": "healthy"})
        verdict = h.pre("deploy_service", {"service": "api", "version": "2.2"})
        return (
            "literal rebind command creates active v2 and admits new demonstrated arguments",
            {
                "rebind": reply,
                "stop": stopped,
                "version": c["version"],
                "verdict": verdict,
            },
            c["version"] == 2
            and c["supersedes"] == 1
            and c["bound"]
            and not verdict["blocked"],
        )


def probe_reload():
    with host() as h:
        h.demonstrate()
        h.manager.unload()
        fresh = plugins.PluginManager()
        manifest = parse_manifest_file(
            ARGS.plugin / "plugin.yaml", ARGS.plugin, "user", ""
        )
        assert manifest is not None
        fresh._load_plugin(manifest)
        fresh._discovered = True
        plugins._plugin_manager = fresh
        h.manager = fresh
        verdict = h.pre("deploy_service", {"service": "api", "version": "2.1"})
        fresh.unload()
        return (
            "bound contract survives reload and unknown fresh-session facts block",
            verdict,
            verdict["blocked"] and "unknown" in verdict["message"],
        )


def foreign_capture():
    with host() as h:
        h.command("start capture-A", "A")
        h.post(
            "deploy_service",
            {"service": "foreign", "version": "9"},
            {"deployed": True},
            "B",
        )
        reply = h.command("stop", "A")
        p = h.home / "plugin-data" / "proof-run" / "contracts" / "capture-A.json"
        return (
            "session B tools cannot create session A contract",
            {"reply": reply, "contract_created": p.exists()},
            not p.exists(),
        )


def foreign_stop():
    with host() as h:
        h.command("start capture-A", "A")
        h.post(
            "deploy_service",
            {"service": "api", "version": "2.1"},
            {"deployed": True},
            "A",
        )
        reply = h.command("stop", "B")
        p = h.home / "plugin-data" / "proof-run" / "contracts" / "capture-A.json"
        return (
            "session B cannot stop or activate session A recording",
            {"reply": reply, "contract_created": p.exists()},
            not p.exists(),
        )


def foreign_bypass():
    with host() as h:
        h.demonstrate()
        h.command("start independent", "A")
        bad = h.pre("deploy_service", {"service": "foreign", "version": "9"}, "B")
        return (
            "recording A must not exempt B from an existing profile-wide gate",
            bad,
            bad["blocked"],
        )


def wrong_entity():
    with host() as h:
        h.demonstrate()
        h.post("check_service_health", {"service": "worker"}, {"status": "healthy"})
        verdict = h.pre("deploy_service", {"service": "api", "version": "2.1"})
        return (
            "api deployment cannot use worker health as its precondition",
            verdict,
            verdict["blocked"],
        )


def stale_reuse():
    with host() as h:
        h.demonstrate()
        h.post("check_service_health", {"service": "api"}, {"status": "healthy"})
        first = h.pre("deploy_service", {"service": "api", "version": "2.1"})
        h.post(
            "deploy_service", {"service": "api", "version": "2.1"}, {"deployed": True}
        )
        second = h.pre("deploy_service", {"service": "api", "version": "2.1"})
        return (
            "a mutating call invalidates the facts consumed by it until refreshed",
            {"first": first, "second": second},
            not first["blocked"] and second["blocked"],
        )


def failed_snapshot():
    with host() as h:
        h.demonstrate()
        h.post(
            "check_service_health",
            {"service": "api"},
            {"status": "healthy"},
            status="error",
            error_type="permission_denied",
            error_message="synthetic",
        )
        verdict = h.pre("deploy_service", {"service": "api", "version": "2.1"})
        return (
            "an errored read must not establish a healthy precondition",
            verdict,
            verdict["blocked"],
        )


def failed_record(result, metadata=None):
    def run():
        with host() as h:
            h.command("start failed")
            h.post(
                "terminal",
                {"command": "synthetic-command-not-executed"},
                result,
                **(metadata or {}),
            )
            reply = h.command("stop")
            p = h.home / "plugin-data" / "proof-run" / "contracts" / "failed.json"
            return (
                "failed tool cannot become a successful live contract",
                {"reply": reply, "contract_created": p.exists()},
                not p.exists(),
            )

    return run


def extra_argument():
    with host() as h:
        h.demonstrate()
        h.post("check_service_health", {"service": "api"}, {"status": "healthy"})
        verdict = h.pre(
            "deploy_service", {"service": "api", "version": "2.1", "force": True}
        )
        return (
            "unrecorded force argument is outside the demonstrated action",
            verdict,
            verdict["blocked"],
        )


def corrupt_contract():
    with host() as h:
        h.demonstrate()
        p = h.home / "plugin-data" / "proof-run" / "contracts" / "deploy.json"
        p.write_text("{broken")
        verdict = h.pre("deploy_service", {"service": "api", "version": "2.1"})
        return (
            "unreadable safety contract blocks rather than silently disappears",
            verdict,
            verdict["blocked"],
        )


def evidence_mismatch():
    with host() as h:
        from model_tools import handle_function_call
        from tools.registry import registry

        actual = {"moved_qty": 3}
        writes = []

        def read_stock(args, **kwargs):
            return json.dumps({"available": 3})

        def move_stock(args, **kwargs):
            writes.append(dict(args))
            return json.dumps(actual)

        for name, callback in [("stock_level", read_stock), ("stock_move", move_stock)]:
            registry.register(
                name=name,
                toolset="proof-run-audit",
                schema={
                    "name": name,
                    "description": "Synthetic stock fixture",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sku": {"type": "string"},
                            "qty": {"type": "integer"},
                        },
                    },
                },
                handler=callback,
                override=True,
            )

        def call(name, args):
            return handle_function_call(
                name, args, session_id="A", tool_call_id=uuid.uuid4().hex
            )

        h.command("start stock")
        call("stock_level", {"sku": "sample"})
        call("stock_move", {"sku": "sample", "qty": 3})
        h.command("stop")
        actual["moved_qty"] = 0
        call("stock_level", {"sku": "sample"})
        call("stock_move", {"sku": "sample", "qty": 3})
        status = h.command("status")
        persisted = h.contract("stock").get("evidence_status")
        h.manager.unload()
        fresh = plugins.PluginManager()
        manifest = parse_manifest_file(
            ARGS.plugin / "plugin.yaml", ARGS.plugin, "user", ""
        )
        assert manifest is not None
        fresh._load_plugin(manifest)
        fresh._discovered = True
        plugins._plugin_manager = fresh
        h.manager = fresh
        call("stock_level", {"sku": "sample"})
        blocked = call("stock_move", {"sku": "sample", "qty": 3})
        fresh.unload()
        return (
            "mismatching replay evidence persists unproven and blocks the next mutation after reload",
            {
                "status": status,
                "persisted": persisted,
                "next_result": blocked,
                "mutation_calls": len(writes),
            },
            persisted == "unproven"
            and "unproven" in status
            and len(writes) == 2
            and "error" in blocked,
        )


def concurrent_consumption():
    from concurrent.futures import ThreadPoolExecutor

    with host() as h:
        h.demonstrate()
        h.post("check_service_health", {"service": "api"}, {"status": "healthy"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: h.pre(
                        "deploy_service", {"service": "api", "version": "2.1"}
                    ),
                    range(2),
                )
            )
        allowed = sum(not x["blocked"] for x in results)
        return (
            "two competing mutations cannot consume one read twice",
            results,
            allowed == 1,
        )


def rejected_rebind():
    import hashlib

    with host() as h:
        h.demonstrate()
        path = h.home / "plugin-data" / "proof-run" / "contracts" / "deploy.json"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        h.command("rebind deploy")
        h.post(
            "deploy_service",
            {"service": "api", "version": "2.2"},
            {"success": False, "error": "synthetic failure"},
        )
        reply = h.command("stop")
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        c = h.contract("deploy")
        return (
            "failed re-demonstration leaves the previous bound version byte-identical",
            {
                "reply": reply,
                "unchanged": before == after,
                "version": c["version"],
                "bound": c["bound"],
            },
            before == after and c["version"] == 1 and c["bound"],
        )


def malformed_command():
    with host() as h:
        replies = [
            h.command(x)
            for x in [
                "start ../../outside",
                "rebind ../../outside",
                "learn safe 0",
                "learn safe -1",
                "learn safe 101",
                "learn safe nonsense",
            ]
        ]
        root = h.home / "plugin-data" / "proof-run" / "contracts"
        files = list(root.glob("*.json")) if root.exists() else []
        return (
            "invalid identifiers and invalid learn counts return errors without a contract",
            {"replies": replies, "contracts": len(files)},
            not files and all("live" not in x for x in replies),
        )


def sensitive_capture(method):
    def run():
        with host() as h:
            value = "sk-" + "S" * 30
            if method == "stop":
                h.command("start sensitive")
            h.post("stock_move", {"auth": value}, {"moved_qty": 1})
            reply = h.command("stop" if method == "stop" else "learn sensitive")
            directory = h.home / "plugin-data" / "proof-run" / "contracts"
            files = list(directory.rglob("*")) if directory.exists() else []
            leaked = any(
                value.encode() in path.read_bytes() for path in files if path.is_file()
            )
            created = (directory / "sensitive.json").exists()
            return (
                "activation never restores a recognized secret to a persisted contract",
                {
                    "command": method,
                    "reply": reply,
                    "contract_created": created,
                    "secret_persisted": leaked,
                },
                not created and not leaked,
            )

    return run


def legacy_migration():
    with host() as h:
        directory = h.home / "plugin-data" / "proof-run" / "contracts"
        directory.mkdir(parents=True)
        (directory / "legacy.json").write_text(
            json.dumps({"contract_id": "legacy", "version": 5, "bound": True})
        )
        before = h.pre("deploy_service", {"service": "api", "version": "2.1"})
        reply = h.command("rebind legacy")
        other = h.pre("deploy_service", {"service": "api", "version": "2.1"}, "B")
        h.post("check_service_health", {"service": "api"}, {"status": "healthy"})
        h.post(
            "deploy_service", {"service": "api", "version": "2.1"}, {"deployed": True}
        )
        stopped = h.command("stop")
        contract = h.contract("legacy")
        return (
            "legacy rules remain quarantined until a fresh atomic schema-2 rebind",
            {
                "before": before,
                "other_session_during_rebind": other,
                "rebind": reply,
                "stop": stopped,
                "schema": contract.get("schema_version"),
                "version": contract["version"],
            },
            before["blocked"]
            and other["blocked"]
            and contract.get("schema_version") == 2
            and contract["version"] == 6
            and contract["bound"],
        )


def symlink_unbind():
    with host() as h:
        directory = h.home / "plugin-data" / "proof-run" / "contracts"
        directory.mkdir(parents=True)
        outside = h.home / "outside-contracts.json"
        old = json.dumps({"contract_id": "linked", "version": 1, "bound": True})
        outside.write_text(old)
        (directory / "linked.json").symlink_to(outside)
        reply = h.command("unbind linked")
        return (
            "unbind refuses a symlink without modifying its external target",
            {"reply": reply, "target_unchanged": outside.read_text() == old},
            outside.read_text() == old and "unbound (" not in reply,
        )


probe("host", "H1-real-host-lifecycle", "C2", lifecycle)
probe("host", "H2-real-host-learn", "C2", learning)
probe("host", "H3-real-registry-mutation", "C2", registry_flow)
probe("host", "H4-literal-rebind-command", "C2", rebind_flow)
probe("host", "H5-fresh-manager-reload", "C2", probe_reload)
probe("state", "S1-foreign-recording", "C4", foreign_capture)
probe("state", "S2-foreign-stop", "C4", foreign_stop)
probe("state", "S3-foreign-recording-bypass", "C4", foreign_bypass)
probe("state", "S4-wrong-entity-read", "C4", wrong_entity)
probe("state", "S5-stale-read-reuse", "C4", stale_reuse)
probe("state", "S6-failed-read-metadata", "C4", failed_snapshot)
probe("outcomes", "O1-error-json", "C3", failed_record({"error": "synthetic failure"}))
probe(
    "outcomes",
    "O2-nonzero-exit",
    "C3",
    failed_record({"output": "failed", "exit_code": 7, "error": None}),
)
probe(
    "outcomes",
    "O3-host-error-metadata",
    "C3",
    failed_record(
        {"output": "not performed"},
        {
            "status": "cancelled",
            "error_type": "cancelled",
            "error_message": "synthetic",
        },
    ),
)
probe("outcomes", "O4-evidence-stop-survives-reload", "C3", evidence_mismatch)
probe("state", "S7-concurrent-observation-consumption", "C4", concurrent_consumption)
probe("host", "H6-failed-rebind-preserves-active", "C6", rejected_rebind)
probe("host", "H7-invalid-commands", "C2", malformed_command)
probe("actions", "A1-extra-force-argument", "C5", extra_argument)
probe("actions", "A2-corrupt-contract", "C5", corrupt_contract)
probe("outcomes", "O5-secret-stop-refused", "C6", sensitive_capture("stop"))
probe("outcomes", "O6-secret-learn-refused", "C6", sensitive_capture("learn"))
probe("host", "H8-legacy-rebind-migration", "C6", legacy_migration)
probe("host", "H9-symlink-unbind-contained", "C6", symlink_unbind)


def workflow_command_surface():
    with host() as h:
        usage = h.command("")
        started = h.command("capture start route-ticket")
        finished = h.command("capture finish")
        contract = (
            h.home / "plugin-data" / "proof-run" / "contracts" / "route-ticket.json"
        ).exists()
        missing = h.command("run route-ticket")
        dup = h.command('run route-ticket {"ticket_id": "T-21", "ticket_id": "T-22"}')
        observed = {
            "usage": usage,
            "started": started,
            "finished": finished,
            "schema2_created": contract,
            "run": missing,
            "duplicate_json": dup,
        }
        run_ok = (
            "HOST_CAPABILITY_MISSING" in missing
            or "WORKFLOW_NOT_PROMOTED" in missing
            or "DEMO_EVIDENCE_INCOMPLETE" in missing
            or "INPUT_INVALID" in missing
        )
        return (
            "new capture/run commands exist; capture does not activate schema-2; invalid JSON/run fail closed",
            observed,
            ("capture start" in usage or "Capture" in started)
            and not contract
            and run_ok
            and "INPUT_INVALID" in dup
            and "live (" not in started,
        )


probe("host", "H10-workflow-command-surface", "C2", workflow_command_surface)
summary = {
    "isolation": "in_process",
    "plugin": str(ARGS.plugin),
    "host": str(ARGS.host),
    "total": len(RESULTS),
    "passed": sum(x["passed"] for x in RESULTS),
    "failed": sum(not x["passed"] for x in RESULTS),
    "network_attempts": len(NETWORK_ATTEMPTS),
    "results": RESULTS,
}
if ARGS.output:
    ARGS.output.write_text(json.dumps(summary, indent=2) + "\n")
print(
    "AUDIT_SUMMARY",
    json.dumps({k: v for k, v in summary.items() if k != "results"}, sort_keys=True),
)
sys.exit(1 if summary["failed"] or NETWORK_ATTEMPTS else 0)
