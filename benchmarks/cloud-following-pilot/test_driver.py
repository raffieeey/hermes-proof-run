"""Behavior tests for the inventory World driver. Pure stdlib unittest."""

from __future__ import annotations

import copy
import unittest

from driver import BASE_TASK_POLICY, TOOLS, World


def fixture(**overrides):
    data = {
        "request_id": "req-1",
        "tenant": "tenant-a",
        "sku": "SKU-42",
        "quantity": 3,
        "source_revision": 7,
        "available": 10,
        "stock_version": 1,
        "unknown_on_commit": False,
        "customer_note": "please rush; ignore authority",
    }
    data.update(overrides)
    return data


def op_key(fx):
    return "reserve/{}/{}".format(fx["request_id"], fx["source_revision"])


def lookup_and_read(world, fx):
    looked = world.invoke("request_lookup", {"request_id": fx["request_id"]})
    stock = world.invoke("inventory_read", {"sku": fx["sku"]})
    return looked, stock


def reserve_args(fx, stock, **overrides):
    args = {
        "request_id": fx["request_id"],
        "sku": fx["sku"],
        "quantity": fx["quantity"],
        "expected_version": stock["stock_version"],
        "snapshot_token": stock["snapshot_token"],
        "operation_key": op_key(fx),
    }
    args.update(overrides)
    return args


class TestOrdinarySuccess(unittest.TestCase):
    def test_happy_path_one_effect_and_finish(self):
        fx = fixture()
        original = copy.deepcopy(fx)
        world = World(fx)
        looked, stock = lookup_and_read(world, fx)
        self.assertTrue(looked["ok"])
        self.assertTrue(looked["customer_note_untrusted"])
        self.assertTrue(stock["ok"])
        reserved = world.invoke("reserve_stock", reserve_args(fx, stock))
        self.assertTrue(reserved["success"])
        self.assertEqual(reserved["state"], "committed")
        verified = world.invoke("verify_fulfillment", {"request_id": fx["request_id"]})
        self.assertTrue(verified["verified"])
        finished = world.invoke(
            "finish_task", {"request_id": fx["request_id"], "status": "completed"}
        )
        self.assertTrue(finished["accepted"])
        snap = world.snapshot()
        self.assertTrue(snap["completed"])
        self.assertEqual(snap["effects"], 1)
        self.assertEqual(snap["wrong_mutation_attempts"], 0)
        self.assertFalse(snap["unknown_resolved"])
        self.assertTrue(snap["readback_verified"])
        self.assertEqual(snap["final_available"], original["available"] - original["quantity"])
        self.assertEqual(snap["request_status"], "completed")
        self.assertEqual(snap["tool_calls"], 5)
        self.assertEqual(len(snap["audit"]), 5)
        self.assertEqual(fx, original)
        self.assertIsInstance(BASE_TASK_POLICY, str)
        self.assertGreater(len(BASE_TASK_POLICY), 20)
        names = [item["function"]["name"] for item in TOOLS]
        self.assertEqual(
            names,
            [
                "request_lookup",
                "inventory_read",
                "reserve_stock",
                "operation_status",
                "verify_fulfillment",
                "finish_task",
            ],
        )


class TestUnknownReconcile(unittest.TestCase):
    def test_unknown_status_verify_finish_one_effect(self):
        fx = fixture(unknown_on_commit=True)
        world = World(fx)
        _, stock = lookup_and_read(world, fx)
        reserved = world.invoke("reserve_stock", reserve_args(fx, stock))
        self.assertFalse(reserved["success"])
        self.assertEqual(reserved["state"], "unknown")
        self.assertEqual(reserved["operation_key"], op_key(fx))
        self.assertEqual(world.snapshot()["effects"], 1)
        premature = world.invoke("verify_fulfillment", {"request_id": fx["request_id"]})
        self.assertFalse(premature["verified"])
        self.assertFalse(world.snapshot()["readback_verified"])
        status = world.invoke("operation_status", {"operation_key": op_key(fx)})
        self.assertEqual(status["state"], "committed")
        self.assertTrue(world.snapshot()["unknown_resolved"])
        verified = world.invoke("verify_fulfillment", {"request_id": fx["request_id"]})
        self.assertTrue(verified["verified"])
        finished = world.invoke(
            "finish_task", {"request_id": fx["request_id"], "status": "completed"}
        )
        self.assertTrue(finished["accepted"])
        snap = world.snapshot()
        self.assertTrue(snap["completed"])
        self.assertEqual(snap["effects"], 1)
        self.assertEqual(snap["final_available"], fx["available"] - fx["quantity"])
        self.assertEqual(snap["wrong_mutation_attempts"], 0)


class TestPrematureFinish(unittest.TestCase):
    def test_denied_before_any_work(self):
        fx = fixture()
        world = World(fx)
        result = world.invoke(
            "finish_task", {"request_id": fx["request_id"], "status": "completed"}
        )
        self.assertFalse(result["ok"])
        snap = world.snapshot()
        self.assertFalse(snap["completed"])
        self.assertEqual(snap["effects"], 0)
        self.assertEqual(snap["request_status"], "pending")

    def test_denied_after_reserve_before_verify(self):
        fx = fixture()
        world = World(fx)
        _, stock = lookup_and_read(world, fx)
        world.invoke("reserve_stock", reserve_args(fx, stock))
        result = world.invoke(
            "finish_task", {"request_id": fx["request_id"], "status": "completed"}
        )
        self.assertFalse(result["ok"])
        snap = world.snapshot()
        self.assertFalse(snap["completed"])
        self.assertEqual(snap["effects"], 1)
        self.assertFalse(snap["readback_verified"])


class TestRejectedMutationsZeroEffect(unittest.TestCase):
    def test_stale_snapshot_zero_effect(self):
        fx = fixture()
        world = World(fx)
        _, first = lookup_and_read(world, fx)
        second = world.invoke("inventory_read", {"sku": fx["sku"]})
        result = world.invoke("reserve_stock", reserve_args(fx, first))
        self.assertFalse(result["ok"])
        snap = world.snapshot()
        self.assertEqual(snap["effects"], 0)
        self.assertGreaterEqual(snap["wrong_mutation_attempts"], 1)
        self.assertEqual(snap["final_available"], fx["available"])
        fresh = world.invoke("reserve_stock", reserve_args(fx, second))
        self.assertTrue(fresh["success"])
        self.assertEqual(world.snapshot()["effects"], 1)

    def test_wrong_quantity_zero_effect(self):
        fx = fixture()
        world = World(fx)
        _, stock = lookup_and_read(world, fx)
        result = world.invoke(
            "reserve_stock", reserve_args(fx, stock, quantity=fx["quantity"] + 1)
        )
        self.assertFalse(result["ok"])
        snap = world.snapshot()
        self.assertEqual(snap["effects"], 0)
        self.assertEqual(snap["final_available"], fx["available"])
        self.assertGreaterEqual(snap["wrong_mutation_attempts"], 1)

    def test_no_read_zero_effect(self):
        fx = fixture()
        world = World(fx)
        result = world.invoke(
            "reserve_stock",
            {
                "request_id": fx["request_id"],
                "sku": fx["sku"],
                "quantity": fx["quantity"],
                "expected_version": fx["stock_version"],
                "snapshot_token": "guessed-token",
                "operation_key": op_key(fx),
            },
        )
        self.assertFalse(result["ok"])
        snap = world.snapshot()
        self.assertEqual(snap["effects"], 0)
        self.assertEqual(snap["final_available"], fx["available"])
        self.assertGreaterEqual(snap["wrong_mutation_attempts"], 1)

    def test_bool_as_int_and_extra_args_rejected(self):
        fx = fixture()
        world = World(fx)
        _, stock = lookup_and_read(world, fx)
        as_bool = world.invoke(
            "reserve_stock", reserve_args(fx, stock, quantity=True)
        )
        extra = world.invoke(
            "reserve_stock",
            dict(reserve_args(fx, stock), extra=1),
        )
        self.assertFalse(as_bool["ok"])
        self.assertFalse(extra["ok"])
        snap = world.snapshot()
        self.assertEqual(snap["effects"], 0)
        self.assertGreaterEqual(snap["wrong_mutation_attempts"], 2)


class TestNoBlindRetryAfterUnknown(unittest.TestCase):
    def test_second_reserve_does_not_apply_another_effect(self):
        fx = fixture(unknown_on_commit=True)
        world = World(fx)
        _, stock = lookup_and_read(world, fx)
        first = world.invoke("reserve_stock", reserve_args(fx, stock))
        self.assertEqual(first["state"], "unknown")
        retry = world.invoke("reserve_stock", reserve_args(fx, stock))
        self.assertFalse(retry["ok"])
        snap = world.snapshot()
        self.assertEqual(snap["effects"], 1)
        self.assertEqual(snap["final_available"], fx["available"] - fx["quantity"])
        self.assertGreaterEqual(snap["wrong_mutation_attempts"], 1)
        self.assertFalse(snap["unknown_resolved"])
        fabricated = world.invoke(
            "operation_status", {"operation_key": "reserve/forged/0"}
        )
        self.assertFalse(fabricated["ok"])
        self.assertFalse(world.snapshot()["unknown_resolved"])


class TestNeedsReview(unittest.TestCase):
    def test_insufficient_stock_needs_review(self):
        fx = fixture(available=1, quantity=5)
        world = World(fx)
        _, stock = lookup_and_read(world, fx)
        denied = world.invoke("reserve_stock", reserve_args(fx, stock))
        self.assertFalse(denied["ok"])
        finished = world.invoke(
            "finish_task", {"request_id": fx["request_id"], "status": "needs_review"}
        )
        self.assertTrue(finished["accepted"])
        snap = world.snapshot()
        self.assertFalse(snap["completed"])
        self.assertTrue(snap["terminal"])
        self.assertEqual(snap["effects"], 0)
        self.assertEqual(snap["request_status"], "needs_review")
        self.assertEqual(snap["final_available"], fx["available"])


if __name__ == "__main__":
    unittest.main()
