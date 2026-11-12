"""Engine tests: exact path, decide path, fail-closed controls, labels."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_FIX = Path(__file__).resolve().parent / "fixtures"
if str(_FIX) not in sys.path:
    sys.path.insert(0, str(_FIX))

from runtime_support import FileFollower, FileHost, load_fixture, new_state_dir  # noqa: E402

from proof_run.engine import WorkflowRunner  # noqa: E402
from proof_run.store import WorkflowStore  # noqa: E402
from proof_run.workflow import WorkflowError  # noqa: E402

ACTOR = {"id": "operator", "profile_id": "test-profile", "session_id": "sess-1"}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "profile"
        self.root.mkdir()
        self.state = new_state_dir(Path(self.temp.name))
        self.store = WorkflowStore(self.root)
        self.host = FileHost(self.state, self.store)
        self.decision_path = Path(self.temp.name) / "decision.json"

    def _promote(self, fixture: str) -> dict:
        doc = load_fixture(fixture)
        stored = self.store.put_candidate(doc["workflow_id"], doc)
        return self.store.promote(stored["workflow_id"], stored["revision"], ACTOR)

    def _write_decision(self, payload: dict) -> FileFollower:
        self.decision_path.write_text(json.dumps(payload), encoding="utf-8")
        return FileFollower(self.decision_path)

    def test_exact_read_assert_effect_readback(self):
        wf = self._promote("exact_ensure_fixture_release.json")
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        result = WorkflowRunner(self.store, self.host).drive(run["run_id"])
        self.assertIn(result["state"], ("completed", "verified"))
        self.assertEqual(result["effect_readback"], "verified")
        self.assertEqual(result["semantic_judgment"], "not_applicable")
        self.assertEqual(result["whole_goal"], "verified")
        self.assertEqual(self.host.counters()["effect"], 1)
        self.assertGreaterEqual(self.host.counters()["read"], 2)
        self.assertEqual(self.host.world()["deploy"]["version"], "v1")
        self.assertEqual(
            result.get("teacher_outcome_assurance")
            or self.store.inspect(wf["workflow_id"], wf["revision"])["provenance"][
                "teacher_outcome_assurance"
            ],
            "not_measured",
        )

    def test_parameterized_decide_path_and_wrong_select_not_independently_correct(self):
        wf = self._promote("semantic_route_ticket.json")
        follower = self._write_decision(
            {"kind": "select", "queue": "access", "evidence_ids": ["s1"]}
        )
        run = self.store.create_run(wf["workflow_id"], {"ticket_id": "T-21"}, ACTOR)
        result = WorkflowRunner(self.store, self.host, follower).drive(run["run_id"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["effect_readback"], "verified")
        self.assertEqual(result["semantic_judgment"], "not_independently_verified")
        self.assertEqual(result["whole_goal"], "unverified")
        self.assertNotIn(result["semantic_judgment"], ("independently_correct", "verified"))
        self.assertEqual(self.host.counters()["effect"], 1)
        self.assertEqual(self.host.world()["tickets"]["T-21"]["queue"], "access")
        follower_ok = self._write_decision(
            {"kind": "select", "queue": "billing", "evidence_ids": ["s1"]}
        )
        # Same ticket/source/epoch with a different selected queue must conflict.
        run2 = self.store.create_run(wf["workflow_id"], {"ticket_id": "T-21"}, ACTOR)
        second = WorkflowRunner(self.store, self.host, follower_ok).drive(run2["run_id"])
        self.assertIn(second.get("error_code") or second["state"], ("INTENT_CONFLICT", "blocked", "failed"))
        self.assertEqual(self.host.counters()["effect"], 1)

    def test_abstain_causes_zero_effect(self):
        wf = self._promote("semantic_route_ticket.json")
        follower = self._write_decision({"kind": "abstain", "reason": "ambiguous"})
        run = self.store.create_run(wf["workflow_id"], {"ticket_id": "T-21"}, ACTOR)
        result = WorkflowRunner(self.store, self.host, follower).drive(run["run_id"])
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(result.get("error_code"), "NEEDS_REVIEW")
        self.assertEqual(self.host.counters()["effect"], 0)
        self.assertEqual(self.host.world()["tickets"]["T-21"]["queue"], "unassigned")

    def test_wrong_tenant_stops_before_decide_or_effect(self):
        wf = self._promote("semantic_route_ticket.json")
        world = self.host.world()
        world["tickets"]["T-21"]["tenant_id"] = "tenant-B"
        self.host.set_world(tickets=world["tickets"])
        follower = self._write_decision(
            {"kind": "select", "queue": "billing", "evidence_ids": ["s1"]}
        )
        run = self.store.create_run(wf["workflow_id"], {"ticket_id": "T-21"}, ACTOR)
        result = WorkflowRunner(self.store, self.host, follower).drive(run["run_id"])
        self.assertIn(result["state"], ("blocked", "failed"))
        self.assertEqual(result.get("error_code"), "TARGET_IDENTITY_MISMATCH")
        self.assertEqual(self.host.counters()["effect"], 0)

    def test_off_contract_rewrite_is_not_dispatched(self):
        wf = self._promote("exact_ensure_fixture_release.json")
        self.host.configure(rewrite={"version": "v9"})
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        result = WorkflowRunner(self.store, self.host).drive(run["run_id"])
        self.assertIn(result["state"], ("blocked", "failed"))
        self.assertEqual(self.host.counters()["effect"], 0)
        self.assertEqual(self.host.world()["deploy"]["version"], "v0")

    def test_invalid_inputs_create_no_job(self):
        wf = self._promote("semantic_route_ticket.json")
        with self.assertRaises(WorkflowError) as ctx:
            self.store.create_run(wf["workflow_id"], {"ticket_id": "nope"}, ACTOR)
        self.assertEqual(ctx.exception.code, "INPUT_INVALID")
        self.assertEqual(self.store.list_runs(), [])
        self.assertEqual(self.host.counters()["effect"], 0)
        self.assertEqual(self.host.counters()["read"], 0)

    def test_missing_host_capability_and_missing_follower_zero_effect(self):
        wf = self._promote("exact_ensure_fixture_release.json")
        self.host.set_capabilities(final_authorization=False)
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        result = WorkflowRunner(self.store, self.host).drive(run["run_id"])
        self.assertEqual(result.get("error_code"), "HOST_CAPABILITY_MISSING")
        self.assertEqual(self.host.counters()["effect"], 0)
        self.host.set_capabilities(final_authorization=True)
        semantic = self._promote("semantic_route_ticket.json")
        run2 = self.store.create_run(semantic["workflow_id"], {"ticket_id": "T-21"}, ACTOR)
        result2 = WorkflowRunner(self.store, self.host, follower=None).drive(run2["run_id"])
        self.assertEqual(result2.get("error_code"), "MODEL_ROUTE_REQUIRED")
        self.assertEqual(self.host.counters()["effect"], 0)

    def test_independent_readback_catches_false_success(self):
        wf = self._promote("exact_ensure_fixture_release.json")
        self.host.configure(lie_success=True)
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        result = WorkflowRunner(self.store, self.host).drive(run["run_id"])
        self.assertEqual(result.get("error_code"), "VERIFICATION_FAILED")
        self.assertEqual(result["effect_readback"], "failed")
        self.assertNotEqual(result["state"], "verified")
        self.assertEqual(self.host.world()["deploy"]["version"], "v0")
        self.assertEqual(self.host.counters()["effect"], 1)

    def test_malformed_decision_zero_effect(self):
        wf = self._promote("semantic_route_ticket.json")
        follower = self._write_decision({"kind": "select", "queue": "billing"})
        run = self.store.create_run(wf["workflow_id"], {"ticket_id": "T-21"}, ACTOR)
        result = WorkflowRunner(self.store, self.host, follower).drive(run["run_id"])
        self.assertEqual(result.get("error_code"), "MODEL_OUTPUT_INVALID")
        self.assertEqual(self.host.counters()["effect"], 0)


if __name__ == "__main__":
    unittest.main()
