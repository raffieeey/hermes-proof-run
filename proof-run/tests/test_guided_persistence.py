"""Persistence regression checks for guided procedure saves."""
from __future__ import annotations

import json
import sys
from unittest.mock import patch

try:
    # unittest discover -s tests imports siblings as top-level modules.
    from test_guided_acceptance import GuidedCase, deploy, term
except ModuleNotFoundError:
    from tests.test_guided_acceptance import GuidedCase, deploy, term


def _loaded_core():
    return sys.modules["proof_run_plugin_under_test.proof_run.core"]


class GuidedPersistenceTests(GuidedCase):
    def _record(self):
        h = self.host
        h.proof("start deploy", "teach")
        self.ran("terminal", deploy("2.1.0"), term("ok"), "teach")

    def test_workflow_transform_deferral_does_not_bypass_guided_gate(self):
        h = self.host
        self.teach_deploy()
        self.assertIn("started", h.proof("run deploy version=2.2.0", "run").lower())
        h._proof_run_state["authorizer_guard"] = "installed"
        verdict = h.hooks["pre_tool_call"](
            tool_name="terminal",
            args={"command": "rm -rf build"},
            session_id="run",
            tool_call_id="transform-call",
            execution_phase="transform",
        )
        self.assertEqual(verdict.get("action"), "block")
        self.assertIn("Nothing was executed.", verdict.get("message", ""))

    def test_typed_input_replaces_both_json_numbers_and_string_occurrences(self):
        h = self.host
        h.proof("start scale", "teach")
        self.ran(
            "terminal",
            {"command": "./scale.sh --replicas 5", "replicas": 5},
            term("ok"),
            "teach",
        )
        self.assertIn("saved", h.proof("save replicas=5", "teach").lower())
        self.assertIn("started", h.proof("run scale replicas=7", "run").lower())
        guide = h.guide("run")
        self.assertIn("./scale.sh --replicas 7", guide)
        self.ran(
            "terminal",
            {"command": "./scale.sh --replicas 7", "replicas": 7},
            term("ok"),
            "run",
        )

    def test_malformed_pin_quoting_returns_a_command_error(self):
        self.teach_health("health")
        reply = self.host.proof('pin health "check_service_health.status', "teach")
        self.assertTrue(reply.startswith("Not pinned:"), reply)

    def test_duplicate_demonstrated_values_are_refused(self):
        h = self.host
        h.proof("start duplicate", "teach")
        self.ran("reserve_stock", {"sku": "A1", "qty": 5, "count": 5}, {"ok": True}, "teach")
        reply = h.proof("save qty=5 count=5", "teach")
        self.assertTrue(reply.startswith("Procedure not saved:"), reply)
        self.assertIn("more than one input", reply)
        self.assertIn("teach", h._proof_run_state["recorders"])
        self.assertNotIn("started", h.proof("run duplicate qty=9 count=7", "run").lower())

    def test_corrupt_procedure_returns_a_run_error(self):
        self.teach_deploy()
        core = _loaded_core()
        contract = core.load_contract(self.root, "deploy")
        contract["procedure"]["steps"] = 5
        path = self.root / "contracts" / "deploy.json"
        path.write_text(json.dumps(contract))
        reply = self.host.proof("run deploy version=2.2.0", "run")
        self.assertTrue(reply.startswith("Run not started:"), reply)

    def test_full_inventory_reuse_uses_fresh_tokens_and_keys(self):
        h = self.host
        h.proof("start inventory fulfill", "teach")
        self.ran("request_lookup", {"request_id": "REQ-001"},
                 {"request_id": "REQ-001", "sku": "A1", "quantity": 3, "source_revision": "rev-1"}, "teach")
        self.ran("inventory_read", {"sku": "A1"},
                 {"sku": "A1", "available": 12, "stock_version": 7, "snapshot_token": "token-1"}, "teach")
        self.ran("reserve_stock", {
            "request_id": "REQ-001", "sku": "A1", "quantity": 3,
            "expected_version": 7, "snapshot_token": "token-1",
            "operation_key": "reserve/REQ-001/rev-1",
        }, {"committed": True}, "teach")
        self.ran("verify_fulfillment", {"request_id": "REQ-001"}, {"request_id": "REQ-001", "verified": True}, "teach")
        self.ran("finish_task", {"request_id": "REQ-001", "status": "completed"}, {"accepted": True}, "teach")
        saved = h.proof("save sku=A1 quantity=3 request_id=REQ-001", "teach")
        self.assertIn("saved", saved.lower())
        started = h.proof("run inventory sku=B2 quantity=4 request_id=REQ-002", "run")
        self.assertIn("started", started.lower(), started)
        guide = h.guide("run")
        self.assertIn("request_lookup", guide)
        self.assertNotIn("token-1", guide)
        self.ran("request_lookup", {"request_id": "REQ-002"},
                 {"request_id": "REQ-002", "sku": "B2", "quantity": 4, "source_revision": "rev-2"}, "run")
        self.ran("inventory_read", {"sku": "B2"},
                 {"sku": "B2", "available": 20, "stock_version": 9, "snapshot_token": "token-2"}, "run")
        self.ran("reserve_stock", {
            "request_id": "REQ-002", "sku": "B2", "quantity": 4,
            "expected_version": 9, "snapshot_token": "token-2",
            "operation_key": "reserve/REQ-002/rev-2",
        }, {"committed": True}, "run")
        self.ran("verify_fulfillment", {"request_id": "REQ-002"}, {"request_id": "REQ-002", "verified": True}, "run")
        self.ran("finish_task", {"request_id": "REQ-002", "status": "completed"}, {"accepted": True}, "run")
        self.assertIn("completed", h.guide("run").lower())

    def test_save_writes_the_complete_procedure_once(self):
        self._record()
        core = _loaded_core()
        original = core.save_contract
        with patch("proof_run_plugin_under_test.proof_run.core.save_contract", wraps=original) as save:
            reply = self.host.proof("save version=2.1.0", "teach")
        self.assertIn("saved", reply.lower())
        self.assertEqual(save.call_count, 1)
        stored = core.load_contract(self.root, "deploy")
        self.assertIsNotNone(stored)
        self.assertIn("procedure", stored)
        self.assertFalse(stored["bound"])

    def test_storage_failure_keeps_the_recording_retryable(self):
        self._record()
        core = _loaded_core()
        with patch("proof_run_plugin_under_test.proof_run.core.save_contract", side_effect=OSError("simulated disk failure")):
            reply = self.host.proof("save version=2.1.0", "teach")
        self.assertTrue(reply.startswith("Procedure not saved:"), reply)
        self.assertIn("teach", self.host._proof_run_state["recorders"])
        self.assertIsNone(core.load_contract(self.root, "deploy"))
        retry = self.host.proof("save version=2.1.0", "teach")
        self.assertIn("saved", retry.lower())
        self.assertIn("procedure", core.load_contract(self.root, "deploy"))
