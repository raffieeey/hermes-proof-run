"""Command parsing, trusted actor, capture/prepare/promote/run journey."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_FIX = Path(__file__).resolve().parent / "fixtures"
if str(_FIX) not in sys.path:
    sys.path.insert(0, str(_FIX))

from operator_support import (  # noqa: E402
    ADAPTER_CATALOG,
    REVIEW_TICKET,
    FakeCtx,
    teaching_ticket_trace_receipts,
)

from proof_run import commands  # noqa: E402
from proof_run.store import WorkflowStore  # noqa: E402
from proof_run.workflow import WorkflowError  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_rejects_duplicate_keys_nan_and_non_object(self):
        with self.assertRaises(WorkflowError) as ctx:
            commands.parse_json_object('{"a": 1, "a": 2}')
        self.assertEqual(ctx.exception.code, "INPUT_INVALID")
        with self.assertRaises(WorkflowError) as ctx:
            commands.parse_json_object('{"n": NaN}')
        self.assertEqual(ctx.exception.code, "INPUT_INVALID")
        with self.assertRaises(WorkflowError) as ctx:
            commands.parse_json_object("[1, 2]")
        self.assertEqual(ctx.exception.code, "INPUT_INVALID")
        with self.assertRaises(WorkflowError) as ctx:
            commands.parse_json_object("1")
        self.assertEqual(ctx.exception.code, "INPUT_INVALID")
        ok = commands.parse_json_object('{"ticket_id": "T-21"}')
        self.assertEqual(ok["ticket_id"], "T-21")

    def test_does_not_repair_malformed_names_or_run_ids(self):
        with self.assertRaises(WorkflowError) as ctx:
            commands.parse_name("../../outside")
        self.assertEqual(ctx.exception.code, "INPUT_INVALID")
        with self.assertRaises(WorkflowError) as ctx:
            commands.parse_run_id("R-XYZ")
        self.assertEqual(ctx.exception.code, "INPUT_INVALID")
        with self.assertRaises(WorkflowError) as ctx:
            commands.parse_run_id("R-abcd")
        self.assertEqual(ctx.exception.code, "INPUT_INVALID")
        self.assertEqual(
            commands.parse_run_id("R-0123456789abcdef"), "R-0123456789abcdef"
        )

    def test_parse_commands(self):
        cap = commands.parse_proof_args("capture start route-ticket teach billing")
        self.assertEqual(cap["verb"], "capture")
        self.assertEqual(cap["action"], "start")
        self.assertEqual(cap["name"], "route-ticket")
        run = commands.parse_proof_args('run route-ticket {"ticket_id": "T-21"}')
        self.assertEqual(run["verb"], "run")
        self.assertEqual(run["inputs"]["ticket_id"], "T-21")
        extra = commands.parse_proof_args('run route-ticket {"ticket_id": "T-21", "extra": 1}')
        self.assertEqual(extra["inputs"]["extra"], 1)


class CommandJourneyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "profile"
        self.root.mkdir()
        self.ctx = FakeCtx(self.root, profile_id="test-profile", session_id="sess-1")
        self.store = WorkflowStore(self.root)
        self.ctx.store = self.store

    def _handle(self, raw, session_id="sess-1", session_key="audit-sess-1"):
        return commands.handle(
            self.ctx,
            raw,
            session_id=session_id,
            session_key=session_key,
            platform="cli",
            root=self.root,
        )

    def test_trusted_actor_from_profile_name_and_session_kwargs(self):
        actor = commands.trusted_actor(
            self.ctx, session_id="sess-1", session_key="audit-sess-1"
        )
        self.assertEqual(actor["profile_id"], self.ctx.profile_name)
        self.assertEqual(actor["session_id"], "sess-1")
        self.assertEqual(actor["id"], "audit-sess-1")
        self.assertTrue(actor["id"] and actor["profile_id"] and actor["session_id"])

    def test_actor_ignores_user_json_identity(self):
        actor = commands.trusted_actor(
            self.ctx,
            session_id="sess-1",
            session_key="audit-sess-1",
            user_payload={"id": "forged", "profile_id": "other", "session_id": "nope"},
        )
        self.assertEqual(actor["profile_id"], "test-profile")
        self.assertEqual(actor["session_id"], "sess-1")
        self.assertNotEqual(actor["id"], "forged")

    def test_capture_prepare_inspect_promote_run_without_per_run_ceremony(self):
        msg = self._handle("capture start route-ticket teach once")
        self.assertIn("capture", msg.lower())
        from proof_run import capture

        for rec in teaching_ticket_trace_receipts():
            capture.ingest_receipt(
                self.root,
                rec,
                session_id="sess-1",
                profile_id="test-profile",
                store=self.store,
            )
        finish = self._handle("capture finish")
        self.assertIn("route-ticket", finish)
        self.assertFalse((self.root / "contracts" / "route-ticket.json").exists())
        prepared = self._handle("prepare route-ticket " + json.dumps(REVIEW_TICKET))
        self.assertIn("candidate", prepared.lower() + prepared)
        inspected = self._handle("inspect route-ticket")
        self.assertIn("route-ticket", inspected)
        self.assertIn("example_ticket_get", inspected)
        self.assertIn("example_ticket_route_if_version", inspected)
        # revision 1
        promoted = self._handle("promote route-ticket 1")
        self.assertTrue("promoted" in promoted.lower() or "r1" in promoted.lower())
        spawned_before = len(self.ctx.spawned)
        ack = self._handle('run route-ticket {"ticket_id": "T-21"}')
        self.assertIn("R-", ack)
        self.assertGreaterEqual(len(self.ctx.spawned), spawned_before)
        listed = self._handle("runs")
        self.assertIn("R-", listed)
        # New input id, same standing review, no extra ceremony.
        ack2 = self._handle('run route-ticket {"ticket_id": "T-22"}')
        self.assertIn("R-", ack2)
        self.assertNotIn("review this run", ack2.lower())

    def test_invalid_inputs_create_no_run(self):
        # unpromoted
        reply = self._handle('run missing {"ticket_id": "T-21"}')
        self.assertTrue("WORKFLOW_NOT_PROMOTED" in reply or "not" in reply.lower())
        self.assertEqual(self.store.list_runs(), [])
        self.assertEqual(self.ctx.execute_calls, [])
        self.assertEqual(self.ctx.dispatch_calls, [])

    def test_unknown_prepare_fields_rejected_before_job(self):
        self._handle("capture start route-ticket")
        from proof_run import capture

        for rec in teaching_ticket_trace_receipts():
            capture.ingest_receipt(
                self.root, rec, session_id="sess-1", profile_id="test-profile", store=self.store
            )
        self._handle("capture finish")
        reply = self._handle('prepare route-ticket {"not_a_field": true}')
        self.assertIn("INPUT_INVALID", reply)
        with self.assertRaises(WorkflowError):
            self.store.inspect("route-ticket")

    def test_missing_host_capability_blocks_run_without_dispatch_tool(self):
        self.ctx.checked_enabled = False
        self._handle("capture start route-ticket")
        from proof_run import capture

        for rec in teaching_ticket_trace_receipts():
            capture.ingest_receipt(
                self.root, rec, session_id="sess-1", profile_id="test-profile", store=self.store
            )
        self._handle("capture finish")
        self._handle("prepare route-ticket " + json.dumps(REVIEW_TICKET))
        self._handle("promote route-ticket 1")
        reply = self._handle('run route-ticket {"ticket_id": "T-21"}')
        self.assertIn("HOST_CAPABILITY_MISSING", reply)
        self.assertEqual(self.ctx.dispatch_calls, [])
        self.assertEqual(self.store.list_runs(), [])

    def test_legacy_verbs_are_not_claimed_by_new_parser(self):
        parsed = commands.parse_proof_args("start deploy-api")
        self.assertIsNone(parsed)
        self.assertIsNone(commands.parse_proof_args("stop"))
        self.assertIsNone(commands.parse_proof_args("status"))
        self.assertIsNone(commands.parse_proof_args("cancel"))
        self.assertIsNone(commands.parse_proof_args("learn deploy 3"))

    def test_inspect_shows_reviewed_values_not_only_keys(self):
        self._handle("capture start route-ticket teach once")
        from proof_run import capture

        for rec in teaching_ticket_trace_receipts():
            capture.ingest_receipt(
                self.root,
                rec,
                session_id="sess-1",
                profile_id="test-profile",
                store=self.store,
            )
        self._handle("capture finish")
        review = dict(REVIEW_TICKET)
        review["constants"] = {"desk": "public-support"}
        review["recovery"] = {
            "tool": "example_ticket_operation_status",
            "not_found": "unknown",
        }
        self._handle("prepare route-ticket " + json.dumps(review))
        inspected = self._handle("inspect route-ticket")
        self.assertIn("^T-[0-9]{1,8}$", inspected)
        self.assertIn("charges, invoices, duplicate payments or subscription fees", inspected)
        self.assertIn("an individual user cannot sign in or recover access", inspected)
        self.assertIn("a shared service outage affecting multiple users", inspected)
        self.assertIn("desk=public-support", inspected)
        self.assertIn("effect_intent", inspected)
        self.assertIn("/queue", inspected)
        self.assertIn("route-ticket-v1", inspected)
        self.assertIn("TARGET_IDENTITY_MISMATCH", inspected)
        self.assertIn("max_model_calls", inspected)
        self.assertIn("example_ticket_operation_status", inspected)
        self.assertIn("not_found=unknown", inspected)
        self.assertNotIn("I was charged twice this month for the same invoice.", inspected)
        self.assertNotIn("Authorization: Bearer", inspected)


if __name__ == "__main__":
    unittest.main()
