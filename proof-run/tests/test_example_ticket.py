"""Synthetic ticket adapter: opt-in, CAS, dedupe, identity, restart hazard."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from proof_run import example_ticket


class TicketAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tickets.sqlite"
        self.store = example_ticket.TicketStore(self.path)
        self.store.put_ticket(
            {
                "tenant_id": "tenant-A",
                "ticket_id": "T-21",
                "source_revision": 4,
                "routing_revision": 7,
                "body": "I was charged twice this month for the same invoice.",
                "queue": "unassigned",
                "last_routing_operation": None,
            }
        )

    def test_register_requires_explicit_opt_in(self):
        class Ctx:
            def __init__(self):
                self.tools = []

            def register_tool(self, **kwargs):
                self.tools.append(kwargs)

        ctx = Ctx()
        registered = example_ticket.register_example_ticket_tools(
            ctx, self.store, opt_in=False, tenant_id="tenant-A"
        )
        self.assertFalse(registered)
        self.assertEqual(ctx.tools, [])
        registered = example_ticket.register_example_ticket_tools(
            ctx, self.store, opt_in=True, tenant_id="tenant-A"
        )
        self.assertTrue(registered)
        names = {t.get("name") or t.get("tool") for t in ctx.tools}
        self.assertIn("example_ticket_get", names)
        self.assertIn("example_ticket_route_if_version", names)
        self.assertIn("example_ticket_operation_status", names)

    def test_wrong_tenant_cannot_read_or_route(self):
        with self.assertRaises(Exception) as ctx:
            self.store.get("T-21", tenant_id="tenant-B")
        self.assertTrue(
            getattr(ctx.exception, "code", "") in {"TARGET_IDENTITY_MISMATCH", "TENANT_MISMATCH"}
            or "tenant" in str(ctx.exception).lower()
            or "identity" in str(ctx.exception).lower()
        )
        with self.assertRaises(Exception):
            self.store.route_if_version(
                {
                    "tenant_id": "tenant-B",
                    "ticket_id": "T-21",
                    "expected_source_revision": 4,
                    "expected_routing_revision": 7,
                    "queue": "billing",
                    "operation_key": "k1",
                },
                tenant_id="tenant-B",
            )
        self.assertEqual(self.store.get("T-21", tenant_id="tenant-A")["queue"], "unassigned")

    def test_cas_both_revisions_and_independent_readback(self):
        result = self.store.route_if_version(
            {
                "tenant_id": "tenant-A",
                "ticket_id": "T-21",
                "expected_source_revision": 4,
                "expected_routing_revision": 7,
                "queue": "billing",
                "operation_key": "k-bill",
            },
            tenant_id="tenant-A",
        )
        self.assertTrue(result.get("ok") or result.get("queue") == "billing")
        readback = self.store.get("T-21", tenant_id="tenant-A")
        self.assertEqual(readback["queue"], "billing")
        self.assertEqual(readback["last_routing_operation"], "k-bill")
        self.assertEqual(readback["routing_revision"], 8)
        with self.assertRaises(Exception):
            self.store.route_if_version(
                {
                    "tenant_id": "tenant-A",
                    "ticket_id": "T-21",
                    "expected_source_revision": 4,
                    "expected_routing_revision": 7,
                    "queue": "access",
                    "operation_key": "k-stale",
                },
                tenant_id="tenant-A",
            )
        self.assertEqual(self.store.get("T-21", tenant_id="tenant-A")["queue"], "billing")

    def test_same_key_same_payload_dedupes_different_payload_conflicts(self):
        args = {
            "tenant_id": "tenant-A",
            "ticket_id": "T-21",
            "expected_source_revision": 4,
            "expected_routing_revision": 7,
            "queue": "billing",
            "operation_key": "k-same",
        }
        first = self.store.route_if_version(args, tenant_id="tenant-A")
        second = self.store.route_if_version(args, tenant_id="tenant-A")
        self.assertEqual(
            self.store.get("T-21", tenant_id="tenant-A")["last_routing_operation"], "k-same"
        )
        self.assertTrue(second.get("deduped") or second.get("ok") or first.get("ok"))
        other = dict(args)
        other["queue"] = "access"
        with self.assertRaises(Exception) as ctx:
            self.store.route_if_version(other, tenant_id="tenant-A")
        self.assertTrue(
            getattr(ctx.exception, "code", "") in {"INTENT_CONFLICT", "PAYLOAD_CONFLICT"}
            or "conflict" in str(ctx.exception).lower()
            or "payload" in str(ctx.exception).lower()
        )

    def test_uncertain_operation_survives_reopen(self):
        self.store.mark_operation_unknown(
            "k-unknown",
            payload={
                "tenant_id": "tenant-A",
                "ticket_id": "T-21",
                "queue": "billing",
                "operation_key": "k-unknown",
            },
        )
        reopened = example_ticket.TicketStore(self.path)
        status = reopened.operation_status("k-unknown")
        self.assertIn(status.get("state"), {"unknown", "pending", "uncertain"})
        self.assertNotEqual(status.get("state"), "not_found")

    def test_status_committed_matches_payload(self):
        args = {
            "tenant_id": "tenant-A",
            "ticket_id": "T-21",
            "expected_source_revision": 4,
            "expected_routing_revision": 7,
            "queue": "billing",
            "operation_key": "k-stat",
        }
        self.store.route_if_version(args, tenant_id="tenant-A")
        status = self.store.operation_status("k-stat")
        self.assertEqual(status["state"], "committed")
        self.assertEqual(status["payload"]["queue"], "billing")


if __name__ == "__main__":
    unittest.main()
