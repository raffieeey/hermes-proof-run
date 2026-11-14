"""Tests for live-transfer gaps in procedure save and bound inputs."""
from __future__ import annotations

from tests.test_guided_acceptance import GuidedCase, deploy, term
from proof_run import core


class GuidedTransferTests(GuidedCase):
    def test_read_only_request_id_must_be_declared_as_an_input(self):
        h = self.host
        h.proof("start reserve", "teach")
        self.ran("request_lookup", {"request_id": "REQ-001"}, {"sku": "A1", "quantity": 3}, "teach")
        self.ran("inventory_read", {"sku": "A1"}, {"available": 12, "snapshot_token": "t"}, "teach")
        self.ran("reserve_stock", {"request_id": "REQ-001", "sku": "A1", "quantity": 3}, {"committed": True}, "teach")
        self.ran("finish_task", {"request_id": "REQ-001", "status": "completed"}, {"accepted": True}, "teach")
        reply = h.proof("save sku=A1 quantity=3", "teach")
        self.assertTrue(reply.startswith("Procedure not saved:"), reply)
        self.assertIn("read argument", reply)
        self.assertIn("input name", reply)
        self.assertIn("request_id", reply)
        self.assertNotIn("started", h.proof("run reserve sku=B2 quantity=3", "run").lower())
        good = h.proof("save sku=A1 request_id=REQ-001", "teach")
        self.assertIn("saved", good.lower())

    def test_bound_value_cannot_change_recorded_quantity_inside_a_command(self):
        h = self.host
        h.proof("start count", "teach")
        self.ran("terminal", {"command": "count=5"}, term("ok"), "teach")
        self.assertIn("saved", h.proof("save count=5", "teach").lower())
        h.proof("run count=7", "run")
        self.ran("terminal", {"command": "count=7"}, term("ok"), "run")

    def test_dynamic_values_are_not_silently_used_for_the_guide(self):
        h = self.host
        h.proof("start stock", "teach")
        self.ran("inventory_read", {"sku": "A1"}, {"snapshot_token": "fresh", "version": 7}, "teach")
        self.ran("reserve_stock", {"sku": "A1", "snapshot_token": "fresh"}, {"committed": True}, "teach")
        reply = h.proof("save sku=A1", "teach")
        self.assertIn("saved", reply.lower())
        self.assertIn("snapshot_token", h.proof("inspect stock", "teach"))
        self.assertIn("started", h.proof("run stock sku=B2", "run").lower())
        self.assertNotIn('"fresh"', h.guide("run"))
        blocked = self.blocked("reserve_stock", {"sku": "B2", "snapshot_token": "fresh"}, {"committed": True}, "run")
        self.assertIn("fresh read data is missing", blocked)
        self.ran("inventory_read", {"sku": "B2"}, {"snapshot_token": "new", "version": 7}, "run")
        self.ran("reserve_stock", {"sku": "B2", "snapshot_token": "new"}, {"committed": True}, "run")
