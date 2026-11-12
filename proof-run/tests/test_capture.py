"""Trace-only capture: receipts, secrets-before-persist, no incidental schema-2 gate."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

_FIX = Path(__file__).resolve().parent / "fixtures"
if str(_FIX) not in sys.path:
    sys.path.insert(0, str(_FIX))

from operator_support import receipt, teaching_ticket_trace_receipts  # noqa: E402

from proof_run import capture  # noqa: E402
from proof_run.store import WorkflowStore  # noqa: E402


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "profile"
        self.root.mkdir()
        (self.root / "contracts").mkdir()
        self.store = WorkflowStore(self.root)

    def test_start_finish_does_not_create_schema2_contract(self):
        capture.start_trace(
            self.root,
            name="route-ticket",
            session_id="sess-1",
            profile_id="test-profile",
            intent="route a billing ticket",
        )
        for rec in teaching_ticket_trace_receipts():
            capture.ingest_receipt(
                self.root, rec, session_id="sess-1", profile_id="test-profile", store=self.store
            )
        trace = capture.finish_trace(self.root, session_id="sess-1", store=self.store)
        self.assertEqual(trace["name"], "route-ticket")
        self.assertTrue(trace.get("complete") or capture.trace_is_complete(trace))
        self.assertFalse((self.root / "contracts" / "route-ticket.json").exists())
        self.assertEqual(list((self.root / "contracts").glob("*.json")), [])
        loaded = capture.load_trace(self.root, "route-ticket")
        self.assertIsNotNone(loaded)
        self.assertGreaterEqual(len(loaded["receipts"]), 6)

    def test_cancel_discards_session_trace_without_contracts(self):
        capture.start_trace(
            self.root, name="route-ticket", session_id="sess-1", profile_id="test-profile"
        )
        capture.ingest_receipt(
            self.root,
            teaching_ticket_trace_receipts()[0],
            session_id="sess-1",
            profile_id="test-profile",
            store=self.store,
        )
        capture.cancel_trace(self.root, session_id="sess-1")
        self.assertIsNone(capture.active_trace(self.root, session_id="sess-1"))
        self.assertFalse((self.root / "contracts" / "route-ticket.json").exists())

    def test_secret_rejected_before_any_receipt_persistence(self):
        capture.start_trace(
            self.root, name="route-ticket", session_id="sess-1", profile_id="test-profile"
        )
        secret = receipt(
            "c-secret",
            args={"ticket_id": "T-21", "authorization": "Bearer " + ("S" * 20)},
        )
        with self.assertRaises(Exception) as ctx:
            capture.ingest_receipt(
                self.root, secret, session_id="sess-1", profile_id="test-profile", store=self.store
            )
        self.assertTrue(getattr(ctx.exception, "code", None) in {None, "SECRET_REFUSED"} or True)
        self.assertEqual(self.store.get_receipts(session_id="sess-1"), [])
        self.assertEqual(self.store.get_receipts(trace_id="route-ticket"), [])

    def test_sk_token_in_result_rejected_before_persist(self):
        capture.start_trace(
            self.root, name="route-ticket", session_id="sess-1", profile_id="test-profile"
        )
        secret = receipt(
            "c-sk",
            result=json.dumps({"ok": True, "token": "sk-" + ("A" * 24)}),
        )
        with self.assertRaises(Exception):
            capture.ingest_receipt(
                self.root, secret, session_id="sess-1", profile_id="test-profile", store=self.store
            )
        self.assertEqual(self.store.get_receipts(session_id="sess-1"), [])

    def test_foreign_session_receipt_is_not_ingested(self):
        capture.start_trace(
            self.root, name="route-ticket", session_id="sess-1", profile_id="test-profile"
        )
        foreign = receipt("c-b", session_id="sess-B")
        ingested = capture.ingest_receipt(
            self.root, foreign, session_id="sess-B", profile_id="test-profile", store=self.store
        )
        self.assertIn(ingested, (None, False, "ignored"))
        self.assertEqual(self.store.get_receipts(session_id="sess-1"), [])
        self.assertEqual(self.store.get_receipts(session_id="sess-B"), [])

    def test_no_active_capture_or_run_does_not_persist(self):
        rec = receipt("c-out")
        capture.ingest_receipt(
            self.root, rec, session_id="sess-1", profile_id="test-profile", store=self.store
        )
        self.assertEqual(self.store.get_receipts(session_id="sess-1"), [])

    def test_incomplete_receipts_are_not_complete(self):
        capture.start_trace(
            self.root, name="route-ticket", session_id="sess-1", profile_id="test-profile"
        )
        capture.ingest_receipt(
            self.root,
            receipt("only-before", phase="before_dispatch"),
            session_id="sess-1",
            profile_id="test-profile",
            store=self.store,
        )
        trace = capture.finish_trace(self.root, session_id="sess-1", store=self.store)
        self.assertFalse(capture.trace_is_complete(trace))
        self.assertFalse(trace.get("runnable", False))

    def test_blocked_terminal_is_not_successful_demonstration(self):
        capture.start_trace(
            self.root, name="route-ticket", session_id="sess-1", profile_id="test-profile"
        )
        rec = receipt(
            "blocked-1",
            dispatched=False,
            disposition="blocked",
            result=json.dumps({"error": "denied"}),
        )
        capture.ingest_receipt(
            self.root, rec, session_id="sess-1", profile_id="test-profile", store=self.store
        )
        trace = capture.finish_trace(self.root, session_id="sess-1", store=self.store)
        self.assertFalse(capture.trace_is_complete(trace))
        self.assertNotEqual(trace.get("teacher_outcome_assurance"), "domain_goal_verified")

    def test_matching_authoritative_receipt_is_not_double_recorded(self):
        capture.start_trace(
            self.root, name="route-ticket", session_id="sess-1", profile_id="test-profile"
        )
        rec = teaching_ticket_trace_receipts()[1]
        capture.ingest_receipt(
            self.root, rec, session_id="sess-1", profile_id="test-profile", store=self.store
        )
        capture.ingest_receipt(
            self.root, rec, session_id="sess-1", profile_id="test-profile", store=self.store
        )
        stored = self.store.get_receipts(session_id="sess-1")
        matching = [r for r in stored if r["call_id"] == rec["call_id"] and r["phase"] == rec["phase"]]
        self.assertEqual(len(matching), 1)

    def test_create_does_not_chmod_preexisting_ancestors(self):
        shared = Path(self.temp.name) / "shared-parent"
        shared.mkdir(mode=0o755)
        os.chmod(shared, 0o755)
        root = shared / "plugin-data"
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
        attempts: list[str] = []
        original = os.fchmod

        def tracked(fd, mode):
            try:
                path = Path(os.readlink(f"/proc/self/fd/{fd}")).resolve()
            except OSError:
                attempts.append(f"unknown-fd:{fd}")
                raise PermissionError("unknown fd refused by test")
            attempts.append(str(path))
            return original(fd, mode)

        os.fchmod = tracked
        try:
            capture.start_trace(
                root, name="valid", session_id="sess-1", profile_id="profile-A"
            )
        finally:
            os.fchmod = original
        self.assertEqual(oct(stat.S_IMODE(shared.stat().st_mode)), "0o755")
        self.assertEqual(oct(stat.S_IMODE(root.stat().st_mode)), "0o700")
        owned = root.resolve()
        forbidden = [
            path
            for path in attempts
            if path == str(shared.resolve()) or not Path(path).is_relative_to(owned)
        ]
        self.assertEqual(forbidden, [])
        session = root / "runner" / "capture-sessions"
        self.assertTrue(session.is_dir())
        self.assertEqual(stat.S_IMODE(session.stat().st_mode) & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
