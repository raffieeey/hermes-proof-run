"""Scripted agents for the offline pre-flight. No model, no network.

They exist to prove two things before any live spend: that a perfect teacher can
record every task and `/proof save` it, and that a perfect follower, guided by
the saved procedure and gated by the plugin, can finish every task on a fresh
instance. They also give the negative controls the checker must fail.
"""

from __future__ import annotations

import json

from inventory_target import Target


def _read_ok(text: str) -> dict:
    return json.loads(text)


class Scripted:
    """Drive a ``Target`` through the plugin's hooks with a fixed policy."""

    def __init__(self, host, target: Target, session: str):
        self.host = host
        self.target = target
        self.session = session
        self.blocked: list[str] = []
        self.events: list[tuple[str, dict, dict | None]] = []

    def call(self, tool: str, args: dict) -> dict | None:
        message, text = self.host.call(
            tool, args, lambda: self.target.invoke_json(tool, args), self.session
        )
        if message:
            self.blocked.append(message)
            self.events.append((tool, args, None))
            return None
        result = _read_ok(text)
        self.events.append((tool, args, result))
        return result

    # --- policies -------------------------------------------------------

    def correct(self, instance: dict) -> None:
        """Lookup, read, reserve, verify, finish for each request in order."""
        for request in instance["requests"]:
            rid, sku = request["request_id"], request["sku"]
            lookup = self.call("request_lookup", {"request_id": rid})
            read = self.call("inventory_read", {"sku": sku})
            if lookup is None or read is None:
                return
            reserved = self.call(
                "reserve_stock",
                {
                    "request_id": lookup["request_id"],
                    "sku": lookup["sku"],
                    "quantity": lookup["quantity"],
                    "expected_version": read["stock_version"],
                    "snapshot_token": read["snapshot_token"],
                    "operation_key": read["operation_key"],
                },
            )
            if reserved is None or not reserved.get("ok"):
                return
            if self.call("verify_fulfillment", {"request_id": rid}) is None:
                return
            self.call("finish_task", {"request_id": rid, "status": "completed"})

    def replay_literal(self, instance: dict, recorded: list[tuple[str, dict]]) -> None:
        """Replay the teacher's reserve and finish arguments verbatim against a new world."""
        for request in instance["requests"]:
            self.call("request_lookup", {"request_id": request["request_id"]})
            self.call("inventory_read", {"sku": request["sku"]})
        for tool, args in recorded:
            if tool in ("reserve_stock", "finish_task"):
                self.call(tool, args)

    def reserve_first(self, instance: dict) -> None:
        """Guess every value and reserve before reading anything."""
        request = instance["requests"][0]
        self.call(
            "reserve_stock",
            {
                "request_id": request["request_id"],
                "sku": request["sku"],
                "quantity": request["quantity"],
                "expected_version": 1,
                "snapshot_token": "guess",
                "operation_key": "reserve/" + request["request_id"],
            },
        )

    def read_before_lookup(self, instance: dict) -> None:
        """Read stock, then look up, then reserve with the early read."""
        request = instance["requests"][0]
        read = self.call("inventory_read", {"sku": request["sku"]})
        lookup = self.call("request_lookup", {"request_id": request["request_id"]})
        if read and lookup:
            self.call(
                "reserve_stock",
                {
                    "request_id": request["request_id"],
                    "sku": request["sku"],
                    "quantity": lookup["quantity"],
                    "expected_version": read["stock_version"],
                    "snapshot_token": read["snapshot_token"],
                    "operation_key": read["operation_key"],
                },
            )

    def hold_instead(self, instance: dict) -> None:
        request = instance["requests"][0]
        self.call("place_hold", {"sku": request["sku"], "quantity": request["quantity"]})
        self.call("finish_task", {"request_id": request["request_id"], "status": "completed"})

    def batch_reads_first(self, instance: dict) -> None:
        """Look up and read every request, then reserve each from its early read."""
        lookups, reads = [], []
        for request in instance["requests"]:
            lookups.append(self.call("request_lookup", {"request_id": request["request_id"]}))
            reads.append(self.call("inventory_read", {"sku": request["sku"]}))
        for lookup, read in zip(lookups, reads):
            if lookup is None or read is None:
                continue
            self.call(
                "reserve_stock",
                {
                    "request_id": lookup["request_id"],
                    "sku": lookup["sku"],
                    "quantity": lookup["quantity"],
                    "expected_version": read["stock_version"],
                    "snapshot_token": read["snapshot_token"],
                    "operation_key": read["operation_key"],
                },
            )
