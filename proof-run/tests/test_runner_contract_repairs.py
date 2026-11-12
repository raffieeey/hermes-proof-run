"""Regression tests for resume, admission scope, readback, route, and promotion."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

_FIX = Path(__file__).resolve().parent / "fixtures"
if str(_FIX) not in sys.path:
    sys.path.insert(0, str(_FIX))

from runtime_support import (  # noqa: E402
    EFFECT_TOOLS,
    FileFollower,
    FileHost,
    load_fixture,
    new_state_dir,
)

from proof_run.engine import WorkflowRunner  # noqa: E402
from proof_run.store import WorkflowStore  # noqa: E402
from proof_run.workflow import WorkflowError  # noqa: E402

ACTOR = {"id": "operator", "profile_id": "test-profile", "session_id": "sess-1"}


class UncertainHost(FileHost):
    """Receiver commits, caller only sees an unknown terminal receipt."""

    def execute_checked(self, tool_name, args, *, run_context):
        if tool_name not in EFFECT_TOOLS:
            return super().execute_checked(tool_name, args, run_context=run_context)
        self.store.admit_dispatch(run_context, tool_name, args)
        with self._locked() as fh:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
            self._inc("effect")
            self._apply_effect(tool_name, args)
        return {
            "schema": "ExecutionReceipt.v1",
            "phase": "terminal",
            "call_id": "lost-terminal",
            "profile_id": "test-profile",
            "session_id": "sess-1",
            "run_id": run_context["run_id"],
            "tool_name": tool_name,
            "final_args": copy.deepcopy(args),
            "proposed_args": copy.deepcopy(args),
            "dispatched": True,
            "disposition": "unknown",
            "result": None,
            "receipt_persisted": False,
        }


class CaptureFollower:
    def __init__(self, route, mismatch=False):
        self.route = copy.deepcopy(route)
        if mismatch:
            self.route.update(provider="unapproved-provider", model="unapproved-model")
        self.calls = 0
        self.packet = None

    def identity(self):
        return self.route

    def decide(self, packet):
        self.calls += 1
        self.packet = copy.deepcopy(packet)
        return {"kind": "select", "queue": "billing", "evidence_ids": ["s1"]}


class BlockedReadHost(FileHost):
    """Successful pre-effect reads; blocked post-effect reads."""

    def execute_checked(self, tool_name, args, *, run_context):
        rec = super().execute_checked(tool_name, args, run_context=run_context)
        if tool_name in EFFECT_TOOLS:
            return rec
        intents = self.store.list_effect_intents(run_context.get("run_id") or "")
        if intents:
            rec = copy.deepcopy(rec)
            rec["dispatched"] = False
            rec["disposition"] = "blocked"
            rec["result"] = json.dumps({"service": "fixture-api", "status": "healthy", "version": "v1"})
            return rec
        return rec


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "profile"
        self.root.mkdir()
        self.state = new_state_dir(Path(self.temp.name))
        self.store = WorkflowStore(self.root)
        self.host = FileHost(self.state, self.store)
        self.decision_path = Path(self.temp.name) / "decision.json"

    def _put_promote(self, fixture, host=None, *, patch=None):
        host = host or self.host
        doc = load_fixture(fixture)
        if patch:
            doc = copy.deepcopy(doc)
            patch(doc)
        stored = self.store.put_candidate(doc["workflow_id"], doc)
        return self.store.promote(stored["workflow_id"], stored["revision"], ACTOR), doc

    def _write_decision(self, payload):
        self.decision_path.write_text(json.dumps(payload), encoding="utf-8")
        return FileFollower(self.decision_path)

    def test_r1_unknown_resume_reconciles_committed_without_retry(self):
        host = UncertainHost(self.state, self.store)
        wf, _ = self._put_promote("exact_ensure_fixture_release.json", host=host)
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        first = WorkflowRunner(self.store, host).drive(run["run_id"])
        self.assertEqual(first["state"], "unknown_outcome")
        before = host.counters()
        drive_again = WorkflowRunner(self.store, host).drive(run["run_id"])
        self.assertEqual(drive_again["state"], "unknown_outcome")
        self.assertEqual(host.counters()["effect"], before["effect"])
        self.assertEqual(host.counters()["status"], before["status"])
        resumed = WorkflowRunner(WorkflowStore(self.root), host).resume(run["run_id"])
        after = host.counters()
        self.assertIn(resumed["state"], ("completed", "verified"))
        self.assertEqual(resumed["effect_readback"], "verified")
        self.assertEqual(after["effect"], before["effect"])
        self.assertEqual(after["effect"], 1)
        self.assertGreater(after["status"], before["status"])
        self.assertEqual(host.world()["deploy"]["version"], "v1")
        follow = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        follow_lease = self.store.acquire_lease(follow["run_id"], "W-probe")
        self.store.persist_effect_intent(
            run_id=follow["run_id"],
            node_id="deploy",
            operation_key="other-after-verify",
            resource_key=json.dumps(
                {"action": "deploy", "target": "fixture-api", "tenant": "tenant-A"},
                sort_keys=True,
                separators=(",", ":"),
            ),
            payload={"service": "fixture-api", "version": "v2"},
            fence=follow_lease["fence"],
        )

    def test_r1_not_found_status_stays_unknown_without_extra_effect(self):
        wf, _ = self._put_promote("exact_ensure_fixture_release.json")
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        self.host.configure(crash="after_admit")
        # Simulate an unknown terminal after dispatch with no committed ledger row.
        lease = self.store.acquire_lease(run["run_id"], "W-u")
        payload = {
            "service": "fixture-api",
            "version": "v1",
            "operation_key": "missing-op",
        }
        self.store.persist_effect_intent(
            run_id=run["run_id"],
            node_id="deploy",
            operation_key="missing-op",
            resource_key="not-found-resource",
            payload=payload,
            fence=lease["fence"],
        )
        self.store.mark_dispatch_started(run["run_id"], "deploy", lease["fence"])
        self.store.update_run(
            run["run_id"],
            state="unknown_outcome",
            error_code="UNKNOWN_OUTCOME",
            result={
                "effect_readback": "unknown",
                "semantic_judgment": "not_applicable",
                "whole_goal": "unverified",
            },
        )
        self.store.mark_hazard_unknown("not-found-resource", run["run_id"])
        before = self.host.counters()
        resumed = WorkflowRunner(self.store, self.host).resume(run["run_id"])
        after = self.host.counters()
        self.assertEqual(resumed["state"], "unknown_outcome")
        self.assertEqual(after["effect"], before["effect"])
        self.assertGreaterEqual(after["status"], before["status"] + 1)

    def test_r1_semantic_resume_keeps_unverified_without_model(self):
        host = UncertainHost(self.state, self.store)
        wf, doc = self._put_promote("semantic_route_ticket.json", host=host)
        follower = CaptureFollower(doc["model_route"])
        run = self.store.create_run(wf["workflow_id"], {"ticket_id": "T-21"}, ACTOR)
        first = WorkflowRunner(self.store, host, follower).drive(run["run_id"])
        self.assertEqual(first["state"], "unknown_outcome")
        self.assertEqual(follower.calls, 1)
        resumed = WorkflowRunner(WorkflowStore(self.root), host, follower=None).resume(
            run["run_id"]
        )
        self.assertIn(resumed["state"], ("completed", "verified"))
        self.assertEqual(resumed["semantic_judgment"], "not_independently_verified")
        self.assertEqual(resumed["whole_goal"], "unverified")
        self.assertNotIn(resumed["semantic_judgment"], ("independently_correct", "verified", "not_applicable"))
        self.assertEqual(follower.calls, 1)
        self.assertEqual(host.counters()["effect"], 1)

    def test_r2_admit_dispatch_rejects_forged_tool_scope_and_digest(self):
        wf, _ = self._put_promote("exact_ensure_fixture_release.json")
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        lease = self.store.acquire_lease(run["run_id"], "repair-probe")
        payload = {"service": "fixture-api", "version": "v1", "operation_key": "parent-key"}
        self.store.persist_effect_intent(
            run_id=run["run_id"],
            node_id="deploy",
            operation_key="parent-key",
            resource_key="parent-resource",
            payload=payload,
            fence=lease["fence"],
        )
        ctx = {
            "run_id": run["run_id"],
            "profile_id": "test-profile",
            "session_id": "sess-1",
            "workflow_digest": wf["content_digest"],
            "fence": lease["fence"],
            "intent_ref": {"operation_key": "parent-key"},
        }
        self.store.admit_dispatch(ctx, "deploy_service", payload)
        accepted = {}
        for field, value, tool in (
            ("tool_name", None, "other_mutation"),
            ("profile_id", "other-profile", "deploy_service"),
            ("session_id", "other-session", "deploy_service"),
            ("workflow_digest", "wrong-digest", "deploy_service"),
        ):
            bad = copy.deepcopy(ctx)
            if value is not None:
                bad[field] = value
            bad["approved"] = True
            bad["allowlist"] = True
            try:
                self.store.admit_dispatch(bad, tool, payload)
                accepted[field] = True
            except WorkflowError:
                accepted[field] = False
        self.assertFalse(any(accepted.values()), accepted)

    def test_r2_create_run_requires_trusted_actor_identifiers(self):
        wf, _ = self._put_promote("exact_ensure_fixture_release.json")
        with self.assertRaises(WorkflowError):
            self.store.create_run(wf["workflow_id"], {}, {"id": "operator", "kind": "test"})
        with self.assertRaises(WorkflowError):
            self.store.create_run(
                wf["workflow_id"],
                {},
                {"id": "operator", "profile_id": "", "session_id": "sess-1"},
            )

    def test_r2_authenticated_context_mismatch_blocks_before_effect(self):
        wf, _ = self._put_promote("exact_ensure_fixture_release.json")
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        self.host.set_context(profile_id="other-profile")
        result = WorkflowRunner(self.store, self.host).drive(run["run_id"])
        self.assertIn(result["state"], ("blocked", "failed"))
        self.assertEqual(self.host.counters()["effect"], 0)

    def test_r2_read_admission_does_not_require_effect_intent(self):
        wf, _ = self._put_promote("exact_ensure_fixture_release.json")
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        lease = self.store.acquire_lease(run["run_id"], "read-probe")
        ctx = {
            "run_id": run["run_id"],
            "profile_id": "test-profile",
            "session_id": "sess-1",
            "workflow_digest": wf["content_digest"],
            "fence": lease["fence"],
            "intent_ref": {},
        }
        self.store.admit_dispatch(ctx, "check_service_health", {"service": "fixture-api"})

    def test_r3_truncated_effect_is_not_verified(self):
        doc = load_fixture("exact_ensure_fixture_release.json")
        idx = next(i for i, n in enumerate(doc["nodes"]) if n["kind"] == "effect")
        doc["nodes"] = doc["nodes"][: idx + 1]
        try:
            stored = self.store.put_candidate(doc["workflow_id"], doc)
            wf = self.store.promote(stored["workflow_id"], stored["revision"], ACTOR)
        except WorkflowError as exc:
            self.assertTrue(exc.code)
            return
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        before = self.host.counters()["read"]
        result = WorkflowRunner(self.store, self.host).drive(run["run_id"])
        self.assertNotEqual(result.get("effect_readback"), "verified")
        self.assertNotEqual(result.get("whole_goal"), "verified")
        self.assertEqual(self.host.counters()["read"] - before, 1)

    def test_r3_blocked_post_effect_read_is_not_verified(self):
        host = BlockedReadHost(self.state, self.store)
        wf, _ = self._put_promote("exact_ensure_fixture_release.json", host=host)
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        result = WorkflowRunner(self.store, host).drive(run["run_id"])
        self.assertNotEqual(result.get("effect_readback"), "verified")
        self.assertNotEqual(result.get("whole_goal"), "verified")
        self.assertNotEqual(result["state"], "verified")

    def test_r4_packet_includes_reviewed_policy_and_rejects_unapproved_route(self):
        observations = []
        for mismatch in (False, True):
            temp = tempfile.TemporaryDirectory()
            try:
                root = Path(temp.name) / "profile"
                root.mkdir()
                state = new_state_dir(Path(temp.name))
                store = WorkflowStore(root)
                host = FileHost(state, store)
                doc = load_fixture("semantic_route_ticket.json")
                doc["policy"]["rules"] = {
                    "billing": "charges and invoices",
                    "access": "one user cannot sign in",
                    "incident": "shared multi-user outage",
                }
                stored = store.put_candidate(doc["workflow_id"], doc)
                wf = store.promote(stored["workflow_id"], stored["revision"], ACTOR)
                follower = CaptureFollower(doc["model_route"], mismatch)
                run = store.create_run(wf["workflow_id"], {"ticket_id": "T-21"}, ACTOR)
                result = WorkflowRunner(store, host, follower).drive(run["run_id"])
                observations.append(
                    {
                        "mismatched_identity": mismatch,
                        "model_calls": follower.calls,
                        "effect_calls": host.counters()["effect"],
                        "policy_in_packet": (follower.packet or {}).get("policy"),
                        "state": result["state"],
                        "error_code": result.get("error_code"),
                    }
                )
            finally:
                temp.cleanup()
        proper, wrong = observations
        self.assertIsNotNone(proper["policy_in_packet"])
        self.assertIn("rules", proper["policy_in_packet"])
        self.assertEqual(proper["policy_in_packet"]["rules"]["billing"], "charges and invoices")
        self.assertEqual(wrong["model_calls"], 0)
        self.assertEqual(wrong["effect_calls"], 0)
        self.assertNotEqual(wrong["state"], "completed")

    def test_r5_blocked_terminal_receipt_cannot_promote(self):
        rec = {
            "schema": "ExecutionReceipt.v1",
            "phase": "terminal",
            "call_id": "blocked-source",
            "profile_id": "test-profile",
            "session_id": "sess-1",
            "run_id": "teacher-source",
            "tool_name": "check_service_health",
            "proposed_args": {"service": "fixture-api"},
            "final_args": {"service": "fixture-api"},
            "dispatched": False,
            "disposition": "blocked",
            "result": None,
            "receipt_persisted": True,
        }
        self.store.record_receipt(rec, trace_id="blocked-source-trace")
        doc = load_fixture("exact_ensure_fixture_release.json")
        doc["provenance"].update(
            actual_demonstration_receipt=True,
            source_receipt_refs=[{"call_id": "blocked-source"}],
        )
        stored = self.store.put_candidate(doc["workflow_id"], doc)
        with self.assertRaises(WorkflowError) as ctx:
            self.store.promote(stored["workflow_id"], stored["revision"], ACTOR)
        self.assertEqual(ctx.exception.code, "DEMO_EVIDENCE_INCOMPLETE")

    def test_r5_operator_authored_no_demo_stays_separate(self):
        wf, doc = self._put_promote("exact_ensure_fixture_release.json")
        self.assertTrue(wf.get("promoted") or wf.get("status") == "promoted")
        self.assertFalse(doc["provenance"]["actual_demonstration_receipt"])
        self.assertEqual(doc["provenance"]["source_receipt_refs"], [])


if __name__ == "__main__":
    unittest.main()
