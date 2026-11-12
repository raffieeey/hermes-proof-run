"""SQLite WorkflowStore tests: receipts, promotion provenance, hazards, fencing."""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

_FIX = Path(__file__).resolve().parent / "fixtures"
if str(_FIX) not in sys.path:
    sys.path.insert(0, str(_FIX))

from runtime_support import load_fixture  # noqa: E402

from proof_run.store import WorkflowStore  # noqa: E402
from proof_run.workflow import WorkflowError  # noqa: E402

ACTOR = {"id": "operator", "profile_id": "test-profile", "session_id": "sess-1"}
RUN_ID_RE = re.compile(r"^R-[0-9a-f]{16}$")


def _receipt(call_id: str, phase: str = "terminal", **extra) -> dict:
    rec = {
        "schema": "ExecutionReceipt.v1",
        "phase": phase,
        "call_id": call_id,
        "profile_id": "test-profile",
        "session_id": "sess-1",
        "run_id": extra.get("run_id", ""),
        "tool_name": extra.get("tool_name", "example_ticket_get"),
        "proposed_args": {"ticket_id": "T-21"},
        "final_args": {"ticket_id": "T-21"},
        "dispatched": phase == "terminal",
        "disposition": "completed" if phase == "terminal" else "prepared",
        "result": json.dumps({"ok": True}) if phase == "terminal" else None,
        "operation_identity": "",
        "policy_ref": "",
        "receipt_persisted": True,
    }
    rec.update({k: v for k, v in extra.items() if k not in rec or k in extra})
    return rec


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "profile"
        self.root.mkdir()
        self.contracts = self.root / "contracts"
        self.contracts.mkdir()
        self.legacy = self.contracts / "sample.json"
        self.legacy.write_text('{"keep": true}', encoding="utf-8")
        self.store = WorkflowStore(self.root)

    def _put(self, fixture="exact_ensure_fixture_release.json", **patch):
        doc = load_fixture(fixture)
        doc.update(patch)
        return self.store.put_candidate(doc["workflow_id"], doc)

    def test_put_inspect_does_not_touch_contracts(self):
        stored = self._put()
        self.assertEqual(stored["revision"], 1)
        self.assertTrue(stored["content_digest"].startswith("sha256:"))
        inspected = self.store.inspect("ensure-fixture-release")
        self.assertEqual(inspected["revision"], 1)
        self.assertIn("unresolved", inspected)
        self.assertEqual(self.legacy.read_text(encoding="utf-8"), '{"keep": true}')
        self.assertTrue((self.root / "runner").exists())
        self.assertFalse(any(p.name == "sample.json" for p in (self.root / "runner").rglob("*")))
        second = self._put()
        self.assertEqual(second["revision"], 2)

    def test_promote_requires_real_terminal_receipts_not_boolean(self):
        stored = self._put(
            "semantic_route_ticket.json",
            provenance={
                "kind": "claimed-demo",
                "actual_demonstration_receipt": True,
                "teacher_outcome_assurance": "effect_observed",
                "source_receipt_refs": ["call-demo-1"],
                "promotion_authority": "none",
            },
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.store.promote(stored["workflow_id"], stored["revision"], ACTOR)
        self.assertEqual(ctx.exception.code, "DEMO_EVIDENCE_INCOMPLETE")
        self.store.record_receipt(_receipt("call-demo-1", "terminal"))
        promoted = self.store.promote(stored["workflow_id"], stored["revision"], ACTOR)
        self.assertTrue(promoted.get("promoted") or promoted.get("status") == "promoted")
        self.assertEqual(
            promoted["provenance"]["teacher_outcome_assurance"], "effect_observed"
        )

    def test_known_failed_teacher_cannot_promote(self):
        stored = self._put(
            provenance={
                "kind": "synthetic-fixture",
                "actual_demonstration_receipt": False,
                "teacher_outcome_assurance": "domain_goal_failed",
                "source_receipt_refs": [],
                "promotion_authority": "none",
            }
        )
        with self.assertRaises(WorkflowError) as ctx:
            self.store.promote(stored["workflow_id"], stored["revision"], ACTOR)
        self.assertIn(ctx.exception.code, ("DEMO_EVIDENCE_INCOMPLETE", "TEACHER_GOAL_FAILED"))

    def test_create_run_pins_promoted_revision_and_validates_inputs(self):
        stored = self._put()
        with self.assertRaises(WorkflowError) as ctx:
            self.store.create_run(stored["workflow_id"], {}, ACTOR)
        self.assertEqual(ctx.exception.code, "WORKFLOW_NOT_PROMOTED")
        self.store.promote(stored["workflow_id"], stored["revision"], ACTOR)
        run = self.store.create_run(stored["workflow_id"], {}, ACTOR)
        self.assertRegex(run["run_id"], RUN_ID_RE)
        self.assertEqual(run["revision"], stored["revision"])
        self.assertEqual(run["content_digest"], stored["content_digest"])
        self.assertEqual(self.store.get_run(run["run_id"])["run_id"], run["run_id"])
        self.assertEqual(len(self.store.list_runs()), 1)
        semantic = self._put("semantic_route_ticket.json")
        self.store.promote(semantic["workflow_id"], semantic["revision"], ACTOR)
        with self.assertRaises(WorkflowError) as ctx:
            self.store.create_run(semantic["workflow_id"], {"ticket_id": "bad"}, ACTOR)
        self.assertEqual(ctx.exception.code, "INPUT_INVALID")
        listed = self.store.list_runs()
        self.assertTrue(all(RUN_ID_RE.match(item["run_id"]) for item in listed))

    def test_record_receipt_conflict_and_filters(self):
        first = _receipt("call-1", "terminal", run_id="R-aaaabbbbccccdddd", session_id="sess-1")
        self.store.record_receipt(first, trace_id="tr-1")
        self.store.record_receipt(first, trace_id="tr-1")
        other = _receipt("call-1", "terminal", run_id="R-aaaabbbbccccdddd")
        other["disposition"] = "handler_error"
        with self.assertRaises(WorkflowError) as ctx:
            self.store.record_receipt(other, trace_id="tr-1")
        self.assertIn(ctx.exception.code, ("RECEIPT_CONFLICT", "CONFLICT"))
        self.store.record_receipt(_receipt("call-1", "before_dispatch", run_id="R-aaaabbbbccccdddd"))
        by_run = self.store.get_receipts(run_id="R-aaaabbbbccccdddd")
        self.assertGreaterEqual(len(by_run), 2)
        by_session = self.store.get_receipts(session_id="sess-1")
        self.assertGreaterEqual(len(by_session), 1)
        by_trace = self.store.get_receipts(trace_id="tr-1")
        self.assertGreaterEqual(len(by_trace), 1)

    def test_intent_hazard_same_key_conflict_and_reopen(self):
        stored = self._put()
        self.store.promote(stored["workflow_id"], stored["revision"], ACTOR)
        run = self.store.create_run(stored["workflow_id"], {}, ACTOR)
        lease = self.store.acquire_lease(run["run_id"], "W-worker1")
        payload = {"service": "fixture-api", "version": "v1"}
        intent = self.store.persist_effect_intent(
            run_id=run["run_id"],
            node_id="deploy",
            operation_key="ensure-fixture-release-v1:key",
            resource_key="tenant-A|fixture-api|deploy",
            payload=payload,
            expectations={"version": "v1"},
            fence=lease["fence"],
        )
        self.assertEqual(intent["operation_key"], "ensure-fixture-release-v1:key")
        again = self.store.persist_effect_intent(
            run_id=run["run_id"],
            node_id="deploy",
            operation_key="ensure-fixture-release-v1:key",
            resource_key="tenant-A|fixture-api|deploy",
            payload=payload,
            expectations={"version": "v1"},
            fence=lease["fence"],
        )
        self.assertEqual(again["operation_key"], intent["operation_key"])
        with self.assertRaises(WorkflowError) as ctx:
            self.store.persist_effect_intent(
                run_id=run["run_id"],
                node_id="deploy",
                operation_key="ensure-fixture-release-v1:key",
                resource_key="tenant-A|fixture-api|deploy",
                payload={"service": "fixture-api", "version": "v9"},
                expectations={"version": "v9"},
                fence=lease["fence"],
            )
        self.assertIn(ctx.exception.code, ("INTENT_CONFLICT", "PAYLOAD_MISMATCH"))
        run2 = self.store.create_run(stored["workflow_id"], {}, ACTOR)
        lease2 = self.store.acquire_lease(run2["run_id"], "W-worker2")
        with self.assertRaises(WorkflowError) as ctx:
            self.store.persist_effect_intent(
                run_id=run2["run_id"],
                node_id="deploy",
                operation_key="other-key",
                resource_key="tenant-A|fixture-api|deploy",
                payload={"service": "fixture-api", "version": "v1"},
                expectations={},
                fence=lease2["fence"],
            )
        self.assertIn(ctx.exception.code, ("HAZARD_CONFLICT", "RESOURCE_BUSY"))
        reopened = WorkflowStore(self.root)
        fetched = reopened.get_run(run["run_id"])
        self.assertEqual(fetched["run_id"], run["run_id"])

    def test_admit_dispatch_checks_payload_fence_and_cancel(self):
        stored = self._put()
        self.store.promote(stored["workflow_id"], stored["revision"], ACTOR)
        run = self.store.create_run(stored["workflow_id"], {}, ACTOR)
        lease = self.store.acquire_lease(run["run_id"], "W-worker1")
        payload = {"service": "fixture-api", "version": "v1", "operation_key": "K"}
        self.store.persist_effect_intent(
            run_id=run["run_id"],
            node_id="deploy",
            operation_key="K",
            resource_key="tenant-A|fixture-api|deploy",
            payload=payload,
            expectations={},
            fence=lease["fence"],
        )
        ctx = {
            "run_id": run["run_id"],
            "profile_id": "test-profile",
            "session_id": "sess-1",
            "workflow_digest": stored["content_digest"],
            "fence": lease["fence"],
            "intent_ref": {"operation_key": "K"},
        }
        self.store.admit_dispatch(ctx, "deploy_service", payload)
        with self.assertRaises(WorkflowError) as err:
            self.store.admit_dispatch(
                ctx, "deploy_service", {"service": "fixture-api", "version": "v9"}
            )
        self.assertIn(err.exception.code, ("PAYLOAD_MISMATCH", "INTENT_CONFLICT"))
        stale = dict(ctx)
        stale["fence"] = lease["fence"] - 1 if lease["fence"] else 0
        if stale["fence"] == lease["fence"]:
            stale["fence"] = 0
        with self.assertRaises(WorkflowError) as err:
            self.store.admit_dispatch(stale, "deploy_service", payload)
        self.assertEqual(err.exception.code, "FENCE_STALE")
        self.store.request_cancel(run["run_id"])
        with self.assertRaises(WorkflowError) as err:
            self.store.admit_dispatch(ctx, "deploy_service", payload)
        self.assertIn(err.exception.code, ("CANCELLED", "CANCEL_REQUESTED"))

    def _unknown_status_fixture(self):
        stored = self._put()
        self.store.promote(stored["workflow_id"], stored["revision"], ACTOR)
        run = self.store.create_run(stored["workflow_id"], {}, ACTOR)
        lease = self.store.acquire_lease(run["run_id"], "W-status")
        payload = {"service": "fixture-api", "version": "v1", "operation_key": "K-status"}
        self.store.persist_effect_intent(
            run_id=run["run_id"],
            node_id="deploy",
            operation_key="K-status",
            resource_key="tenant-A|fixture-api|deploy-status",
            payload=payload,
            expectations={},
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
        ctx = {
            "run_id": run["run_id"],
            "profile_id": "test-profile",
            "session_id": "sess-1",
            "workflow_digest": stored["content_digest"],
            "fence": lease["fence"],
            "intent_ref": {"operation_key": "K-status"},
        }
        return stored, run, payload, ctx

    def test_admit_status_lookup_on_unknown_outcome_does_not_admit_effects(self):
        stored, run, payload, ctx = self._unknown_status_fixture()
        with self.assertRaises(WorkflowError) as err:
            self.store.admit_dispatch(ctx, "deploy_service", payload)
        self.assertEqual(err.exception.code, "FENCE_STALE")
        with self.assertRaises(WorkflowError):
            self.store.admit_dispatch(
                ctx, "deploy_operation_status", {"operation_key": "K-status"}
            )
        self.store.admit_status_lookup(
            ctx, "deploy_operation_status", {"operation_key": "K-status"}
        )
        with self.assertRaises(WorkflowError) as err:
            self.store.admit_status_lookup(ctx, "deploy_service", payload)
        self.assertEqual(err.exception.code, "CONTEXT_MISMATCH")
        with self.assertRaises(WorkflowError) as err:
            self.store.admit_status_lookup(
                ctx, "deploy_operation_status", payload
            )
        self.assertIn(err.exception.code, ("PAYLOAD_MISMATCH", "CONTEXT_MISMATCH", "INTENT_MISSING"))
        with self.assertRaises(WorkflowError) as err:
            self.store.admit_status_lookup(
                ctx, "deploy_operation_status", {"operation_key": "other-key"}
            )
        self.assertIn(err.exception.code, ("INTENT_MISSING", "INTENT_CONFLICT", "CONTEXT_MISMATCH"))
        forged = dict(ctx)
        forged["profile_id"] = "other-profile"
        with self.assertRaises(WorkflowError) as err:
            self.store.admit_status_lookup(
                forged, "deploy_operation_status", {"operation_key": "K-status"}
            )
        self.assertEqual(err.exception.code, "CONTEXT_MISMATCH")
        completed = dict(ctx)
        self.store.update_run(run["run_id"], state="completed")
        with self.assertRaises(WorkflowError) as err:
            self.store.admit_status_lookup(
                completed, "deploy_operation_status", {"operation_key": "K-status"}
            )
        self.assertEqual(err.exception.code, "FENCE_STALE")
        self.store.update_run(run["run_id"], state="unknown_outcome")
        self.store.request_cancel(run["run_id"])
        self.store.admit_status_lookup(
            ctx, "deploy_operation_status", {"operation_key": "K-status"}
        )
        with self.assertRaises(WorkflowError) as err:
            self.store.admit_dispatch(ctx, "deploy_service", payload)
        self.assertIn(err.exception.code, ("CANCELLED", "FENCE_STALE"))


if __name__ == "__main__":
    unittest.main()
