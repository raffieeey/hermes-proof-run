"""Checked host adapter: no raw-dispatch fallback, phased authorizer, receipt sink."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

_FIX = Path(__file__).resolve().parent / "fixtures"
if str(_FIX) not in sys.path:
    sys.path.insert(0, str(_FIX))

from operator_support import FakeCtx, receipt  # noqa: E402

from proof_run import hostport  # noqa: E402
from proof_run.store import WorkflowStore  # noqa: E402
from proof_run.workflow import WorkflowError  # noqa: E402


class HostPortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "profile"
        self.root.mkdir()
        self.store = WorkflowStore(self.root)
        self.ctx = FakeCtx(self.root)
        self.ctx._proof_run_state = {
            "lock": threading.RLock(),
            "recorders": {},
            "runtime": {},
            "buffers": {},
            "pending": {},
            "unproven": set(),
            "checked_frames": {},
            "authorizer_guard": "unattempted",
        }

    def test_missing_checked_api_does_not_call_dispatch_tool(self):
        self.ctx.checked_enabled = False
        port = hostport.CheckedHostPort(
            self.ctx,
            self.store,
            session_id="sess-1",
            profile_id="test-profile",
            tenant_id="tenant-A",
        )
        caps = port.capabilities()
        self.assertFalse(caps.get("final_authorization"))
        with self.assertRaises(WorkflowError) as err:
            port.execute_checked("example_ticket_get", {"ticket_id": "T-21"}, run_context={"run_id": "R-x"})
        self.assertEqual(err.exception.code, "HOST_CAPABILITY_MISSING")
        self.assertEqual(self.ctx.dispatch_calls, [])

    def test_execute_checked_uses_host_api_not_dispatch_tool(self):
        port = hostport.CheckedHostPort(
            self.ctx,
            self.store,
            session_id="sess-1",
            profile_id="test-profile",
            tenant_id="tenant-A",
        )
        rec = port.execute_checked(
            "example_ticket_get",
            {"ticket_id": "T-21"},
            run_context={"run_id": "R-abc", "session_id": "sess-1", "profile_id": "test-profile"},
        )
        self.assertEqual(rec["schema"], "ExecutionReceipt.v1")
        self.assertEqual(len(self.ctx.execute_calls), 1)
        self.assertEqual(self.ctx.dispatch_calls, [])

    def test_authorizer_check_is_pure_commit_consumes_once(self):
        from proof_run import core

        rec = core.Recorder(self.root)
        rec.start("deploy", intent="deploy")
        rec.note_tool("check_service_health", {"service": "api"}, {"status": "healthy"})
        rec.note_tool("deploy_service", {"service": "api", "version": "2.1"}, {"deployed": True})
        rec.stop(success=True, bound=True)

        session = "sess-1"
        cache = self.ctx._proof_run_state["runtime"].setdefault(session, {})
        cache[core.fact_key("check_service_health", {"service": "api"})] = {
            "result": {"status": "healthy"},
            "observed_at": 0.0,
        }
        # Bypass monotonic expiry in tests by patching through a huge age window:
        import time

        cache[core.fact_key("check_service_health", {"service": "api"})]["observed_at"] = time.monotonic()

        authorizer = hostport.make_legacy_authorizer(self.ctx, self.store)
        check = authorizer(
            "deploy_service",
            {"service": "api", "version": "2.1"},
            phase="check",
            session_id=session,
            tool_call_id="t1",
        )
        self.assertTrue(check is None or check.get("action") != "block")
        self.assertTrue(
            core.fact_key("check_service_health", {"service": "api"})
            in self.ctx._proof_run_state["runtime"][session]
        )
        commit = authorizer(
            "deploy_service",
            {"service": "api", "version": "2.1"},
            phase="commit",
            session_id=session,
            tool_call_id="t1",
        )
        self.assertTrue(commit is None or commit.get("action") != "block")
        self.assertFalse(self.ctx._proof_run_state["runtime"].get(session))

    def test_receipt_sink_persists_interested_and_rejects_secrets_first(self):
        from proof_run import capture

        capture.start_trace(
            self.root, name="route-ticket", session_id="sess-1", profile_id="test-profile"
        )
        sink = hostport.make_receipt_sink(self.ctx, self.store)
        rec = receipt("c-ok", session_id="sess-1", profile_id="test-profile")
        sink(rec, phase="terminal")
        stored = self.store.get_receipts(session_id="sess-1")
        self.assertEqual(len(stored), 1)
        secret = receipt(
            "c-bad",
            session_id="sess-1",
            profile_id="test-profile",
            args={"password": "opaque-secret-value"},
        )
        with self.assertRaises(Exception):
            sink(secret, phase="terminal")
        self.assertEqual(len(self.store.get_receipts(session_id="sess-1")), 1)

    def test_authenticated_context_uses_profile_name_not_session_surface_dict(self):
        port = hostport.CheckedHostPort(
            self.ctx,
            self.store,
            session_id="sess-1",
            profile_id=self.ctx.profile_name,
            tenant_id="tenant-A",
        )
        auth = port.authenticated_context()
        self.assertEqual(auth["profile_id"], self.ctx.profile_name)
        self.assertEqual(auth["session_id"], "sess-1")
        self.assertIsInstance(self.ctx.session_surface(), str)
        self.assertNotIsInstance(self.ctx.session_surface(), dict)

    def test_transform_phase_defers_legacy_consume(self):
        self.assertTrue(hostport.should_defer_legacy_consume("transform"))
        self.assertFalse(hostport.should_defer_legacy_consume(None))
        self.assertFalse(hostport.should_defer_legacy_consume("commit"))
        self.ctx._proof_run_state["authorizer_guard"] = "installed"
        self.assertTrue(hostport.transform_deferral_active(self.ctx, "transform"))
        self.ctx._proof_run_state["authorizer_guard"] = "failed"
        self.assertFalse(hostport.transform_deferral_active(self.ctx, "transform"))

    def test_execute_checked_binds_tool_call_id_for_flat_authorizer(self):
        seen = []

        def authorizer(tool_name, tool_args, *, phase, **context):
            seen.append({"phase": phase, "tool_call_id": context.get("tool_call_id"), "args": dict(tool_args)})
            return None

        self.ctx.authorizers.append(authorizer)

        def execute(tool_name, proposed_args, **kwargs):
            context = {
                "session_id": kwargs.get("session_id"),
                "run_id": kwargs.get("run_id", ""),
                "tool_call_id": kwargs.get("tool_call_id"),
                "profile_id": self.ctx.profile_name,
            }
            for phase in ("check", "commit"):
                for callback in self.ctx.authorizers:
                    callback(tool_name, proposed_args, phase=phase, **context)
            from operator_support import receipt as mk

            return mk("bound", tool_name=tool_name, args=proposed_args)

        self.ctx.execute_checked = execute
        port = hostport.CheckedHostPort(
            self.ctx, self.store, session_id="sess-1", profile_id="test-profile"
        )
        port.execute_checked(
            "check_service_health",
            {"service": "fixture-api"},
            run_context={"run_id": "", "session_id": "sess-1", "profile_id": "test-profile"},
        )
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[0]["tool_call_id"])
        self.assertEqual(seen[0]["tool_call_id"], seen[1]["tool_call_id"])

    def test_status_recovery_on_unknown_outcome_uses_read_only_admission(self):
        from runtime_support import load_fixture

        actor = {"id": "operator", "profile_id": "test-profile", "session_id": "sess-1"}
        doc = load_fixture("semantic_route_ticket.json")
        stored = self.store.put_candidate(doc["workflow_id"], doc)
        self.store.promote(stored["workflow_id"], stored["revision"], actor)
        run = self.store.create_run(stored["workflow_id"], {"ticket_id": "T-21"}, actor)
        lease = self.store.acquire_lease(run["run_id"], "W-host-status")
        payload = {
            "tenant_id": "tenant-A",
            "ticket_id": "T-21",
            "expected_source_revision": 4,
            "expected_routing_revision": 7,
            "queue": "billing",
            "operation_key": "route-ticket-v1:status",
        }
        self.store.persist_effect_intent(
            run_id=run["run_id"],
            node_id="route",
            operation_key="route-ticket-v1:status",
            resource_key="tenant-A|T-21|route",
            payload=payload,
            fence=lease["fence"],
        )
        self.store.mark_dispatch_started(run["run_id"], "route", lease["fence"])
        self.store.update_run(
            run["run_id"],
            state="unknown_outcome",
            error_code="UNKNOWN_OUTCOME",
            result={
                "effect_readback": "unknown",
                "semantic_judgment": "not_independently_verified",
                "whole_goal": "unverified",
            },
        )
        self.ctx.authorizers.append(hostport.make_legacy_authorizer(self.ctx, self.store))
        seen = []

        def execute(tool_name, proposed_args, **kwargs):
            context = {
                "session_id": kwargs.get("session_id"),
                "run_id": kwargs.get("run_id", ""),
                "tool_call_id": kwargs.get("tool_call_id"),
                "profile_id": self.ctx.profile_name,
            }
            for phase in ("check", "commit"):
                for callback in self.ctx.authorizers:
                    directive = callback(
                        tool_name, proposed_args, phase=phase, **context
                    )
                    if isinstance(directive, dict) and directive.get("action") == "block":
                        return receipt(
                            "blocked-status",
                            tool_name=tool_name,
                            args=proposed_args,
                            run_id=kwargs.get("run_id", ""),
                            dispatched=False,
                            disposition="blocked",
                            result=json.dumps({"error": directive.get("message")}),
                            error_code="AUTHORIZER_BLOCKED",
                            error_message=directive.get("message"),
                        )
            seen.append({"tool": tool_name, "args": dict(proposed_args)})
            return receipt(
                "status-ok",
                tool_name=tool_name,
                args=proposed_args,
                run_id=kwargs.get("run_id", ""),
                result=json.dumps(
                    {
                        "state": "committed",
                        "operation_key": proposed_args.get("operation_key"),
                        "payload": payload,
                    }
                ),
            )

        self.ctx.execute_checked = execute
        port = hostport.CheckedHostPort(
            self.ctx, self.store, session_id="sess-1", profile_id="test-profile"
        )
        run_context = {
            "run_id": run["run_id"],
            "profile_id": "test-profile",
            "session_id": "sess-1",
            "workflow_digest": stored["content_digest"],
            "fence": lease["fence"],
            "intent_ref": {"operation_key": "route-ticket-v1:status"},
        }
        status = port.operation_status(
            {
                "operation_key": "route-ticket-v1:status",
                "payload": payload,
                "status_tool": "example_ticket_operation_status",
            },
            run_context=run_context,
        )
        self.assertEqual(status.get("state"), "committed")
        self.assertEqual(seen, [{"tool": "example_ticket_operation_status", "args": {"operation_key": "route-ticket-v1:status"}}])
        blocked = port.execute_checked(
            "example_ticket_route_if_version",
            payload,
            run_context=run_context,
        )
        self.assertEqual(blocked.get("disposition"), "blocked")
        self.assertIn("FENCE_STALE", blocked.get("error_message") or blocked.get("result") or "")


if __name__ == "__main__":
    unittest.main()
