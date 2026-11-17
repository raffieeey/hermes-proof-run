"""Fidelity tests for what the guide tells a follower and how live values bind.

These lock the product claims that were found untrue in review:

1. The guide must state every step of the procedure, not only the current one,
   because the host delivers pre_llm_call once per user turn, not per model step.
2. A recorded number must bind to the read field it belongs to. Coincident
   values (quantity == available) must not collapse into one baked constant.
3. A live read older than the documented freshness limit must not satisfy a
   dynamic slot.
"""
from __future__ import annotations

import unittest
from unittest import mock

try:  # discover imports this module and its fixture as top-level names
    from test_guided_acceptance import GuidedCase, term
except ImportError:  # `python -m unittest tests.test_guided_fidelity`
    from tests.test_guided_acceptance import GuidedCase, term

from proof_run import guided as guided_mod


class GuideCompletenessTests(GuidedCase):
    def test_guide_states_every_step_with_its_arguments(self):
        h = self.host
        h.proof("start release", "teach")
        self.ran("terminal", {"command": "./build.sh 2.1.0"}, term("built"), "teach")
        self.ran("terminal", {"command": "./deploy.sh api 2.1.0"}, term("ok"), "teach")
        self.assertIn("saved", h.proof("save version=2.1.0", "teach").lower())

        h.proof("run release version=2.2.0", "run")
        guide = h.guide("run")
        self.assertIn("./build.sh 2.2.0", guide)
        self.assertIn("./deploy.sh api 2.2.0", guide)
        self.assertIn("2. [later]", guide)
        self.assertIn("1. [NOW]", guide)


class CoincidentValueBindingTests(GuidedCase):
    """quantity and available share a value during the demonstration."""

    def teach(self):
        h = self.host
        h.proof("start reserve", "teach")
        self.ran(
            "request_lookup",
            {"request_id": "REQ-001"},
            {"request_id": "REQ-001", "sku": "A1", "quantity": 5},
            "teach",
        )
        self.ran(
            "inventory_read",
            {"sku": "A1"},
            {"available": 5, "stock_version": 300, "snapshot_token": "tok-old"},
            "teach",
        )
        self.ran(
            "reserve_stock",
            {
                "request_id": "REQ-001",
                "sku": "A1",
                "quantity": 5,
                "expected_version": 300,
                "snapshot_token": "tok-old",
            },
            {"committed": True},
            "teach",
        )
        reply = h.proof("save request_id=REQ-001 sku=A1", "teach")
        self.assertIn("saved", reply.lower(), reply)

    def test_quantity_binds_to_the_request_not_to_the_coincident_stock(self):
        self.teach()
        h = self.host
        self.assertIn("started", h.proof("run reserve request_id=REQ-002 sku=B2", "run").lower())

        self.ran(
            "request_lookup",
            {"request_id": "REQ-002"},
            {"request_id": "REQ-002", "sku": "B2", "quantity": 2},
            "run",
        )
        self.ran(
            "inventory_read",
            {"sku": "B2"},
            {"available": 9, "stock_version": 8, "snapshot_token": "tok-new"},
            "run",
        )
        blocked = self.blocked(
            "reserve_stock",
            {
                "request_id": "REQ-002",
                "sku": "B2",
                "quantity": 5,
                "expected_version": 8,
                "snapshot_token": "tok-new",
            },
            {"committed": True},
            "run",
        )
        self.assertIn('"quantity": 2', blocked)
        self.ran(
            "reserve_stock",
            {
                "request_id": "REQ-002",
                "sku": "B2",
                "quantity": 2,
                "expected_version": 8,
                "snapshot_token": "tok-new",
            },
            {"committed": True},
            "run",
        )


class StaleReadTests(GuidedCase):
    def test_a_read_older_than_the_limit_cannot_satisfy_a_live_slot(self):
        h = self.host
        h.proof("start stock", "teach")
        self.ran(
            "inventory_read",
            {"sku": "A1"},
            {"snapshot_token": "tok-old", "stock_version": 7},
            "teach",
        )
        self.ran(
            "reserve_stock",
            {"sku": "A1", "snapshot_token": "tok-old", "expected_version": 7},
            {"committed": True},
            "teach",
        )
        self.assertIn("saved", h.proof("save sku=A1", "teach").lower())

        h.proof("run stock sku=B2", "run")
        clock = {"now": 1000.0}
        with mock.patch.object(guided_mod.time, "monotonic", lambda: clock["now"]):
            self.ran(
                "inventory_read",
                {"sku": "B2"},
                {"snapshot_token": "tok-new", "stock_version": 9},
                "run",
            )
            clock["now"] += guided_mod.FACT_MAX_AGE + 1
            message = self.blocked(
                "reserve_stock",
                {"sku": "B2", "snapshot_token": "tok-new", "expected_version": 9},
                {"committed": True},
                "run",
            )
        self.assertIn("fresh read data is missing", message)


if __name__ == "__main__":
    unittest.main()
