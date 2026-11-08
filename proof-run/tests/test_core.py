"""Proof Run — contract lifecycle tests (TDD RED phase).

All fixtures synthetic; no network, no real providers. The contract must be
drafted from a recorded success, bound only by the operator, enforced at the
tool boundary, and rebound only by a fresh demonstration.
"""

import unittest
from pathlib import Path
import tempfile

from proof_run import core


class RecorderTests(unittest.TestCase):
    def test_record_success_creates_contract(self):
        with tempfile.TemporaryDirectory() as td:
            rec = core.Recorder(root=Path(td))
            rec.start("move-stock", intent="Move SKU-7 to Bin B when stock >= 3")
            rec.note_tool("stock_level", {"sku": "SKU-7"}, {"available": 3})
            rec.note_tool("stock_move", {"sku": "SKU-7", "qty": 3}, {"moved_qty": 3})
            contract = rec.stop(success=True)
            self.assertEqual(contract["contract_id"], "move-stock")
            self.assertEqual(
                contract["action_window"]["mutating_tools"], ["stock_move"]
            )
            self.assertEqual(contract["success_evidence"][0]["value"], 3)
            self.assertTrue(
                any(c["kind"] == "tool_fact" for c in contract["preconditions"])
            )

    def test_failed_run_does_not_create_contract(self):
        with tempfile.TemporaryDirectory() as td:
            rec = core.Recorder(root=Path(td))
            rec.start("move-stock", intent="Move SKU-7")
            rec.note_tool("stock_move", {"sku": "SKU-7"}, {"error": "boom"})
            with self.assertRaises(core.ContractError):
                rec.stop(success=False)

    def test_contract_is_operator_reviewed_before_binding(self):
        with tempfile.TemporaryDirectory() as td:
            rec = core.Recorder(root=Path(td))
            rec.start("move-stock", intent="Move SKU-7")
            rec.note_tool("stock_move", {"sku": "SKU-7", "qty": 3}, {"moved_qty": 3})
            contract = rec.stop(success=True)
            self.assertEqual(contract["bound"], False)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.contract = {
            "version": 1,
            "contract_id": "move-stock",
            "intent": "Move SKU-7 to Bin B when stock >= 3",
            "action_window": {
                "mutating_tools": ["stock_move"],
                "allowed_arg_patterns": {
                    "stock_move": {"sku": "SKU-7", "qty": {"min": 1, "max": 3}}
                },
            },
            "preconditions": [
                {
                    "kind": "tool_fact",
                    "tool": "stock_level",
                    "args_match": {"sku": "SKU-7"},
                    "field": "available",
                    "op": ">=",
                    "value": 3,
                }
            ],
            "success_evidence": [
                {
                    "kind": "tool_result_field",
                    "tool": "stock_move",
                    "field": "moved_qty",
                    "op": "==",
                    "value": 3,
                }
            ],
        }

    def test_gate_allows_when_preconditions_hold(self):
        runtime = {"stock_level": {"available": 3}}
        verdict = core.check_contract(
            self.contract,
            runtime=runtime,
            proposed_call={"tool": "stock_move", "args": {"sku": "SKU-7", "qty": 2}},
        )
        self.assertEqual(verdict["action"], "allow")

    def test_gate_blocks_on_drift(self):
        runtime = {"stock_level": {"available": 1}}
        verdict = core.check_contract(
            self.contract,
            runtime=runtime,
            proposed_call={"tool": "stock_move", "args": {"sku": "SKU-7", "qty": 2}},
        )
        self.assertEqual(verdict["action"], "block")
        self.assertIn("stock_level.available >= 3", verdict["message"])
        self.assertIn("current 1", verdict["message"])

    def test_gate_blocks_unknown_as_unsafe(self):
        verdict = core.check_contract(
            self.contract,
            runtime={},
            proposed_call={"tool": "stock_move", "args": {"sku": "SKU-7", "qty": 2}},
        )
        self.assertEqual(verdict["action"], "block")
        self.assertIn("unknown", verdict["message"])

    def test_gate_does_not_touch_unrelated_tools(self):
        verdict = core.check_contract(
            self.contract,
            runtime={},
            proposed_call={"tool": "read_file", "args": {"path": "x"}},
        )
        self.assertIsNone(verdict)


class RebindTests(unittest.TestCase):
    def test_rebind_replaces_contract_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            rec = core.Recorder(root=Path(td))
            rec.start("move-stock", intent="Move SKU-7")
            rec.note_tool("stock_move", {"sku": "SKU-7", "qty": 3}, {"moved_qty": 3})
            old = rec.stop(success=True)
            rec2 = core.Recorder(root=Path(td))
            rec2.start("move-stock", intent="Move SKU-7")
            rec2.note_tool("stock_move", {"sku": "SKU-7", "qty": 2}, {"moved_qty": 2})
            new = rec2.stop(success=True)
            self.assertGreater(new["version"], old["version"])
            self.assertEqual(new["supersedes"], old["version"])
            self.assertTrue((Path(td) / "contracts" / "move-stock.json").exists())


if __name__ == "__main__":
    unittest.main()
