"""Offline tests for the live guided A/B benchmark. No model, no key, no network.

    python3 -m unittest discover -s benchmarks/live-guided -p 'test_*.py' -v

``DriverSelftest`` also drives the real Hermes ``AIAgent`` against a scripted
loopback provider. It needs the Python that has Hermes' dependencies:

    HERMES_PY=/path/to/hermes/venv/bin/python python3 -m unittest discover ...

and is skipped without it.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ab_stats  # noqa: E402
import task_set as T  # noqa: E402
from inventory_target import TOOL_NAMES, Target  # noqa: E402
from plugin_host import Host  # noqa: E402
from scripted_agents import Scripted  # noqa: E402


def world(**overrides) -> dict:
    base = {
        "salt": "feedface",
        "ledger_version": 4000,
        "version_step": 2,
        "requests": [
            {"request_id": "REQ-1", "sku": "SKU-A", "quantity": 3, "source_revision": "r1", "note": ""},
            {"request_id": "REQ-2", "sku": "SKU-B", "quantity": 2, "source_revision": "r2", "note": ""},
        ],
        "stock": {"SKU-A": 9, "SKU-B": 5},
    }
    base.update(overrides)
    return base


def reserve_args(lookup: dict, read: dict, **over) -> dict:
    args = {
        "request_id": lookup["request_id"],
        "sku": lookup["sku"],
        "quantity": lookup["quantity"],
        "expected_version": read["stock_version"],
        "snapshot_token": read["snapshot_token"],
        "operation_key": read["operation_key"],
    }
    args.update(over)
    return args


class TargetRules(unittest.TestCase):
    def setUp(self):
        self.t = Target(world())

    def test_happy_path_commits_once_and_moves_the_version(self):
        lookup = self.t.invoke("request_lookup", {"request_id": "REQ-1"})
        read = self.t.invoke("inventory_read", {"sku": "SKU-A"})
        self.assertEqual(read["stock_version"], 4000)
        done = self.t.invoke("reserve_stock", reserve_args(lookup, read))
        self.assertTrue(done["ok"], done)
        self.assertEqual(self.t.snapshot()["ledger_version"], 4002)
        self.assertEqual(self.t.snapshot()["stock"]["SKU-A"], 6)

    def test_reserve_before_any_lookup_or_read_fails_at_the_target(self):
        out = self.t.invoke(
            "reserve_stock",
            {"request_id": "REQ-1", "sku": "SKU-A", "quantity": 3, "expected_version": 4000,
             "snapshot_token": "x", "operation_key": "op-x"},
        )
        self.assertEqual(out["code"], "lookup_required")
        self.assertEqual(self.t.snapshot()["reservations"], {})

    def test_reserve_after_lookup_but_before_a_read_fails(self):
        lookup = self.t.invoke("request_lookup", {"request_id": "REQ-1"})
        out = self.t.invoke(
            "reserve_stock",
            {"request_id": "REQ-1", "sku": lookup["sku"], "quantity": 3, "expected_version": 4000,
             "snapshot_token": "x", "operation_key": "op-x"},
        )
        self.assertEqual(out["code"], "read_required")

    def test_a_read_made_before_the_lookup_does_not_count(self):
        read = self.t.invoke("inventory_read", {"sku": "SKU-A"})
        lookup = self.t.invoke("request_lookup", {"request_id": "REQ-1"})
        out = self.t.invoke("reserve_stock", reserve_args(lookup, read))
        self.assertEqual(out["code"], "read_before_lookup")
        again = self.t.invoke("inventory_read", {"sku": "SKU-A"})
        self.assertTrue(self.t.invoke("reserve_stock", reserve_args(lookup, again))["ok"])

    def test_another_worlds_operation_key_is_rejected_by_the_target(self):
        other = Target(world(salt="0ddba11"))
        other.invoke("request_lookup", {"request_id": "REQ-1"})
        o_read = other.invoke("inventory_read", {"sku": "SKU-A"})
        lookup = self.t.invoke("request_lookup", {"request_id": "REQ-1"})
        read = self.t.invoke("inventory_read", {"sku": "SKU-A"})
        self.assertNotEqual(o_read["operation_key"], read["operation_key"])
        out = self.t.invoke("reserve_stock", reserve_args(lookup, read, operation_key=o_read["operation_key"]))
        self.assertEqual(out["code"], "bad_operation_key")
        out = self.t.invoke("reserve_stock", reserve_args(lookup, read, snapshot_token=o_read["snapshot_token"]))
        self.assertEqual(out["code"], "stale_token")
        out = self.t.invoke("reserve_stock", reserve_args(lookup, read, expected_version=1))
        self.assertEqual(out["code"], "stale_version")
        self.assertEqual(self.t.snapshot()["reservations"], {})

    def test_the_first_reservation_makes_an_earlier_read_stale(self):
        l1 = self.t.invoke("request_lookup", {"request_id": "REQ-1"})
        l2 = self.t.invoke("request_lookup", {"request_id": "REQ-2"})
        r1 = self.t.invoke("inventory_read", {"sku": "SKU-A"})
        r2 = self.t.invoke("inventory_read", {"sku": "SKU-B"})
        self.assertTrue(self.t.invoke("reserve_stock", reserve_args(l1, r1))["ok"])
        out = self.t.invoke("reserve_stock", reserve_args(l2, r2))
        self.assertEqual(out["code"], "stale_version")
        fresh = self.t.invoke("inventory_read", {"sku": "SKU-B"})
        self.assertTrue(self.t.invoke("reserve_stock", reserve_args(l2, fresh))["ok"])

    def test_wrong_quantity_or_sku_is_a_mismatch(self):
        lookup = self.t.invoke("request_lookup", {"request_id": "REQ-1"})
        read = self.t.invoke("inventory_read", {"sku": "SKU-A"})
        self.assertEqual(self.t.invoke("reserve_stock", reserve_args(lookup, read, quantity=40))["code"], "request_mismatch")
        self.assertEqual(self.t.invoke("reserve_stock", reserve_args(lookup, read, sku="SKU-B"))["code"], "request_mismatch")

    def test_bool_is_not_an_integer_and_extra_arguments_are_refused(self):
        lookup = self.t.invoke("request_lookup", {"request_id": "REQ-1"})
        read = self.t.invoke("inventory_read", {"sku": "SKU-A"})
        self.assertEqual(self.t.invoke("reserve_stock", reserve_args(lookup, read, quantity=True))["code"], "bad_args")
        self.assertEqual(self.t.invoke("reserve_stock", reserve_args(lookup, read, extra=1))["code"], "bad_args")
        self.assertEqual(self.t.invoke("request_lookup", {"request_id": "REQ-1", "x": 1})["code"], "bad_args")
        self.assertEqual(self.t.invoke("nope", {})["code"], "unknown_tool")

    def test_finish_needs_a_reservation_and_a_readback(self):
        lookup = self.t.invoke("request_lookup", {"request_id": "REQ-1"})
        read = self.t.invoke("inventory_read", {"sku": "SKU-A"})
        self.assertEqual(self.t.invoke("finish_task", {"request_id": "REQ-1", "status": "completed"})["code"], "premature_finish")
        self.t.invoke("reserve_stock", reserve_args(lookup, read))
        self.assertEqual(self.t.invoke("finish_task", {"request_id": "REQ-1", "status": "completed"})["code"], "premature_finish")
        self.assertTrue(self.t.invoke("verify_fulfillment", {"request_id": "REQ-1"})["verified"])
        self.assertTrue(self.t.invoke("finish_task", {"request_id": "REQ-1", "status": "completed"})["ok"])
        self.assertEqual(self.t.invoke("finish_task", {"request_id": "REQ-1", "status": "completed"})["code"], "already_finished")

    def test_the_distractor_succeeds_and_changes_state_but_is_not_a_reservation(self):
        out = self.t.invoke("place_hold", {"sku": "SKU-A", "quantity": 3})
        self.assertTrue(out["ok"])
        snap = self.t.snapshot()
        self.assertEqual(snap["stock"]["SKU-A"], 6)
        self.assertEqual(snap["reservations"], {})
        self.assertEqual(len(snap["holds"]), 1)

    def test_insufficient_stock_is_refused(self):
        t = Target(world(stock={"SKU-A": 1, "SKU-B": 5}))
        lookup = t.invoke("request_lookup", {"request_id": "REQ-1"})
        read = t.invoke("inventory_read", {"sku": "SKU-A"})
        self.assertEqual(t.invoke("reserve_stock", reserve_args(lookup, read))["code"], "insufficient_stock")

    def test_tool_list_is_the_five_plus_one_distractor(self):
        self.assertEqual(
            set(TOOL_NAMES),
            {"request_lookup", "inventory_read", "reserve_stock", "verify_fulfillment", "finish_task", "place_hold"},
        )

    def test_audit_records_every_call_and_rejections_are_derived_from_it(self):
        self.t.invoke("request_lookup", {"request_id": "REQ-1"})
        self.t.invoke("request_lookup", {"request_id": "NOPE"})
        self.assertEqual(self.t.snapshot()["calls"], 2)
        self.assertEqual(self.t.rejections(), [{"tool": "request_lookup", "code": "unknown_request"}])


class TaskSet(unittest.TestCase):
    def test_at_least_twelve_tasks_across_the_three_bands(self):
        self.assertGreaterEqual(len(T.TASKS), 12)
        for band in T.BANDS:
            self.assertGreaterEqual(len([t for t in T.TASKS if t.band == band]), 3, band)
        self.assertEqual(len({t.id for t in T.TASKS}), len(T.TASKS))

    def test_selection(self):
        self.assertEqual(len(T.select_tasks("all")), len(T.TASKS))
        three = T.select_tasks("3")
        self.assertEqual({t.band for t in three}, set(T.BANDS))
        self.assertEqual([t.id for t in T.select_tasks("s01-basic,o01-stock-first")], ["s01-basic", "o01-stock-first"])
        with self.assertRaises(ValueError):
            T.select_tasks("nope")

    def test_instances_are_deterministic_and_follower_values_never_equal_the_teachers(self):
        for task in T.TASKS:
            demo = T.make_instance(task, "teacher", 0)
            self.assertEqual(demo, T.make_instance(task, "teacher", 0))
            for k in range(5):
                run = T.make_instance(task, "run", k, avoid=demo)
                self.assertEqual(run, T.make_instance(task, "run", k, avoid=demo))
                self.assertNotEqual(run["ledger_version"], demo["ledger_version"])
                self.assertNotEqual(run["salt"], demo["salt"])
                for a, b in zip(run["requests"], demo["requests"]):
                    self.assertNotEqual(a["request_id"], b["request_id"])
                    self.assertNotEqual(a["sku"], b["sku"])
                    self.assertNotEqual(a["quantity"], b["quantity"])

    def test_a_demonstration_has_slack_and_no_coincident_numbers(self):
        for task in T.TASKS:
            demo = T.make_instance(task, "teacher", 0)
            numbers = [r["quantity"] for r in demo["requests"]] + list(demo["stock"].values())
            self.assertEqual(len(set(numbers)), len(numbers), task.id)

    def test_exact_stock_tasks_are_exact_for_the_runs(self):
        for task in (t for t in T.TASKS if t.exact_stock):
            run = T.make_instance(task, "run", 0)
            need: dict = {}
            for r in run["requests"]:
                need[r["sku"]] = need.get(r["sku"], 0) + r["quantity"]
            self.assertEqual(run["stock"], need, task.id)

    def test_prompts_name_the_requests_and_hide_the_procedure(self):
        for task in T.TASKS:
            inst = T.make_instance(task, "run", 0)
            text = T.prompt_for(task, inst)
            for request in inst["requests"]:
                self.assertIn(request["request_id"], text)
            self.assertNotIn("operation_key", text)
            self.assertNotIn("snapshot_token", text)
            self.assertIn(T.TEACHER_NOTE, T.teacher_prompt_for(task, inst))
            self.assertNotIn("demonstrating", text)

    def test_budget_is_the_same_formula_for_both_arms(self):
        self.assertEqual(T.TASKS_BY_ID["s01-basic"].call_budget(), 10)
        self.assertEqual(T.TASKS_BY_ID["f01-two-skus"].call_budget(), 15)

    def test_the_checker_judges_state_not_prose(self):
        task = T.TASKS_BY_ID["s01-basic"]
        inst = T.make_instance(task, "run", 0)
        target = Target(inst)
        self.assertEqual(T.check(task, inst, target.snapshot())[0], False)
        Scripted(_NullHost(), target, "s").correct(inst)
        self.assertEqual(T.check(task, inst, target.snapshot()), (True, ""))

    def test_checker_failure_reasons(self):
        task = T.TASKS_BY_ID["s01-basic"]
        inst = T.make_instance(task, "run", 0)
        request = inst["requests"][0]
        t = Target(inst)
        t.invoke("place_hold", {"sku": request["sku"], "quantity": 1})
        self.assertIn("place_hold", T.check(task, inst, t.snapshot())[1])
        t = Target(inst)
        lookup = t.invoke("request_lookup", {"request_id": request["request_id"]})
        read = t.invoke("inventory_read", {"sku": request["sku"]})
        t.invoke("reserve_stock", reserve_args(lookup, read))
        self.assertIn("never read back", T.check(task, inst, t.snapshot())[1])
        t.invoke("verify_fulfillment", {"request_id": request["request_id"]})
        self.assertIn("not closed", T.check(task, inst, t.snapshot())[1])
        t.invoke("finish_task", {"request_id": request["request_id"], "status": "completed"})
        self.assertEqual(T.check(task, inst, t.snapshot()), (True, ""))


class _NullHost:
    """Calls the target directly, with no plugin in the way."""

    def call(self, tool, args, result, session):
        payload = result() if callable(result) else result
        return "", payload if isinstance(payload, str) else json.dumps(payload)


class ScriptedPreflight(unittest.TestCase):
    """A perfect teacher and a perfect guided follower must pass every task."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host = Host(Path(self.tmp.name))

    def teach(self, task):
        demo = T.make_instance(task, "teacher", 0)
        name, session = "bench-" + task.id, "teach-" + task.id
        self.assertIn("Recording", self.host.proof(f"start {name} fulfil", session))
        target = Target(demo)
        Scripted(self.host, target, session).correct(demo)
        self.assertEqual(T.check(task, demo, target.snapshot()), (True, ""))
        pairs = " ".join(f"{n}={v}" for n, v in T.input_pairs(task, demo))
        reply = self.host.proof("save " + pairs, session)
        self.assertIn("saved", reply, reply)
        return demo

    def start_run(self, task, demo, k=0, session=None):
        run = T.make_instance(task, "run", k, avoid=demo)
        session = session or f"run-{task.id}-{k}"
        pairs = " ".join(f"{n}={v}" for n, v in T.input_pairs(task, run))
        reply = self.host.proof(f"run bench-{task.id} {pairs}", session)
        self.assertIn("started", reply, reply)
        return run, session

    def test_every_task_records_saves_and_transfers_to_a_fresh_instance(self):
        for task in T.TASKS:
            with self.subTest(task=task.id):
                demo = self.teach(task)
                for k in range(2):
                    run, session = self.start_run(task, demo, k)
                    target = Target(run)
                    agent = Scripted(self.host, target, session)
                    agent.correct(run)
                    self.assertEqual(T.check(task, run, target.snapshot()), (True, ""))
                    self.assertEqual(agent.blocked, [])
                    self.assertEqual((self.host.run_state(session) or {})["state"], "completed")
                    self.host.proof("cancel", session)

    def test_replaying_the_teachers_literal_values_fails(self):
        for task_id in ("s01-basic", "f02-same-sku"):
            task = T.TASKS_BY_ID[task_id]
            with self.subTest(task=task_id):
                demo = self.teach(task)
                recorded = []
                teacher_target = Target(demo)
                probe = Scripted(_NullHost(), teacher_target, "p")
                probe.correct(demo)
                recorded = [(tool, args) for tool, args, _r in probe.events]
                # Unguided: the target refuses the teacher's operation key and token.
                run = T.make_instance(task, "run", 0, avoid=demo)
                target = Target(run)
                Scripted(_NullHost(), target, "u").replay_literal(run, recorded)
                self.assertFalse(T.check(task, run, target.snapshot())[0])
                self.assertEqual(target.snapshot()["reservations"], {})
                # Guided: the gate blocks it before it reaches the target.
                run2, session = self.start_run(task, demo, 1)
                target2 = Target(run2)
                agent = Scripted(self.host, target2, session)
                agent.replay_literal(run2, recorded)
                self.assertTrue(agent.blocked)
                self.assertFalse(T.check(task, run2, target2.snapshot())[0])
                self.host.proof("cancel", session)

    def test_unguided_wrong_orders_fail_at_the_target_with_the_expected_codes(self):
        cases = (
            ("s01-basic", "reserve_first", "lookup_required"),
            ("o01-stock-first", "read_before_lookup", "read_before_lookup"),
            ("o04-batch-reads", "batch_reads_first", "stale_version"),
        )
        for task_id, policy, code in cases:
            with self.subTest(policy=policy):
                task = T.TASKS_BY_ID[task_id]
                run = T.make_instance(task, "run", 0)
                target = Target(run)
                getattr(Scripted(_NullHost(), target, "u"), policy)(run)
                self.assertFalse(T.check(task, run, target.snapshot())[0])
                self.assertIn(code, [item["code"] for item in target.rejections()])

    def test_the_distractor_run_fails_the_checker(self):
        task = T.TASKS_BY_ID["o03-set-aside"]
        run = T.make_instance(task, "run", 0)
        target = Target(run)
        Scripted(_NullHost(), target, "u").hold_instead(run)
        passed, reason = T.check(task, run, target.snapshot())
        self.assertFalse(passed)
        self.assertIn("place_hold", reason)

    def test_a_guided_follower_that_reads_first_passes_the_gate_and_fails_at_the_target(self):
        task = T.TASKS_BY_ID["o01-stock-first"]
        demo = self.teach(task)
        run, session = self.start_run(task, demo)
        target = Target(run)
        agent = Scripted(self.host, target, session)
        agent.read_before_lookup(run)
        self.assertEqual(agent.blocked, [], "the gate allowed the call; the target refused it")
        self.assertEqual(target.rejections()[-1]["code"], "read_before_lookup")
        self.assertEqual((self.host.run_state(session) or {})["state"], "stopped")

    def test_coincident_numbers_bind_to_the_right_field(self):
        """A demonstration where quantity equals a stock level still binds live.

        The plugin keeps candidates per (tool, field), so the action's quantity
        binds to the request's quantity and the stock version to the inventory
        read, even when both numbers are equal in the demonstration. This was a
        known limitation; the regression is covered in
        `proof-run/tests/test_guided_fidelity.py`.
        """
        task = T.TASKS_BY_ID["s02-exact-stock"]
        demo = T.make_instance(task, "teacher", 0)
        qty = demo["requests"][0]["quantity"]
        demo["stock"][demo["requests"][0]["sku"]] = qty  # coincident on purpose
        name, session = "bench-coincident", "teach-coincident"
        self.host.proof(f"start {name} x", session)
        Scripted(self.host, Target(demo), session).correct(demo)
        pairs = " ".join(f"{n}={v}" for n, v in T.input_pairs(task, demo))
        self.assertIn("saved", self.host.proof("save " + pairs, session))
        run = T.make_instance(task, "run", 0, avoid=demo)
        pairs = " ".join(f"{n}={v}" for n, v in T.input_pairs(task, run))
        self.host.proof(f"run {name} {pairs}", "run-coincident")
        target = Target(run)
        agent = Scripted(self.host, target, "run-coincident")
        agent.correct(run)
        self.assertEqual(agent.blocked, [], "the coincident value must not bake in")
        self.assertEqual((self.host.run_state("run-coincident") or {})["state"], "completed")


class Stats(unittest.TestCase):
    def test_wilson_known_values(self):
        low, high = ab_stats.wilson(0, 10)
        self.assertAlmostEqual(low, 0.0, places=6)
        self.assertAlmostEqual(high, 0.2775, places=3)
        low, high = ab_stats.wilson(10, 10)
        self.assertAlmostEqual(low, 0.7225, places=3)
        self.assertAlmostEqual(high, 1.0, places=6)
        low, high = ab_stats.wilson(5, 10)
        self.assertAlmostEqual(low, 0.2366, places=3)
        self.assertAlmostEqual(high, 0.7634, places=3)
        self.assertTrue(math.isnan(ab_stats.wilson(0, 0)[0]))

    def test_newcombe_matches_the_published_example(self):
        # Newcombe (1998), Table II, example: 56/70 vs 48/80 -> 0.2000 (0.0524, 0.3339)
        diff, low, high = ab_stats.newcombe_diff(56, 70, 48, 80)
        self.assertAlmostEqual(diff, 0.2, places=4)
        self.assertAlmostEqual(low, 0.0524, places=3)
        self.assertAlmostEqual(high, 0.3339, places=3)

    def test_newcombe_is_antisymmetric_and_bounded(self):
        a = ab_stats.newcombe_diff(9, 10, 3, 10)
        b = ab_stats.newcombe_diff(3, 10, 9, 10)
        self.assertAlmostEqual(a[0], -b[0])
        self.assertAlmostEqual(a[1], -b[2])
        self.assertAlmostEqual(a[2], -b[1])
        low, high = ab_stats.newcombe_diff(10, 10, 0, 10)[1:]
        self.assertTrue(-1 <= low <= high <= 1)

    def test_fisher_matches_brute_force(self):
        def brute(a, b, c, d):
            n, row1, col1 = a + b + c + d, a + b, a + c
            probs = {}
            for x in range(max(0, col1 - (c + d)), min(row1, col1) + 1):
                probs[x] = math.comb(row1, x) * math.comb(n - row1, col1 - x) / math.comb(n, col1)
            return sum(p for p in probs.values() if p <= probs[a] + 1e-12)

        for a, b, c, d in itertools.product(range(0, 6), repeat=4):
            if a + b and c + d:
                self.assertAlmostEqual(ab_stats.fisher_two_sided(a, b, c, d), min(1.0, brute(a, b, c, d)), places=9)
        self.assertAlmostEqual(ab_stats.fisher_two_sided(3, 0, 0, 3), 0.1, places=9)

    def test_reading_never_claims_superiority(self):
        all_pass = [{"passed": True}] * 12
        self.assertIn("does not discriminate", ab_stats.reading(all_pass, all_pass))
        self.assertIn("does not discriminate", ab_stats.reading([{"passed": False}] * 5, [{"passed": False}] * 5))
        worse = ab_stats.reading([{"passed": False}] * 10, [{"passed": True}] * 10)
        self.assertIn("lower", worse)
        better = ab_stats.reading([{"passed": True}] * 10, [{"passed": False}] * 10)
        self.assertNotIn("superior", better.lower())
        close = ab_stats.reading([{"passed": True}] * 3 + [{"passed": False}], [{"passed": True}] * 2 + [{"passed": False}] * 2)
        self.assertIn("no detectable difference", close)

    def test_arm_summary_counts_provider_errors_as_failures_and_reports_them(self):
        runs = [
            {"passed": True, "api_calls": 5, "total_tokens": 100, "wall_s": 1.0, "target_rejections": 0},
            {"passed": False, "api_calls": 0, "total_tokens": 0, "wall_s": 0.2, "target_rejections": 0, "provider_error": "HTTP 500"},
        ]
        summary = ab_stats.arm_summary(runs)
        self.assertEqual((summary["runs"], summary["passed"], summary["provider_error_runs"]), (2, 1, 1))


@unittest.skipUnless(os.environ.get("HERMES_PY"), "set HERMES_PY to the Python that has Hermes' dependencies")
class DriverSelftest(unittest.TestCase):
    def test_selftest_runs_end_to_end_on_a_real_aiagent(self):
        env = dict(os.environ, HERMES_DISABLE_LAZY_INSTALLS="1")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "selftest.json"
            done = subprocess.run(
                [os.environ["HERMES_PY"], str(HERE / "run_ab.py"), "--selftest", "--runs-per-arm", "1", "--out", str(out)],
                capture_output=True, text=True, timeout=240, env=env,
            )
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            printed = json.loads(done.stdout.strip().splitlines()[-1])
            record = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(printed["complete"] and record["complete"] and record["selftest"])
        self.assertIn("SELFTEST", record["claim_ceiling"])
        self.assertEqual(len(record["teaching"]), 3)
        self.assertTrue(all(t["saved"] for t in record["teaching"]))
        self.assertEqual({r["arm"] for r in record["runs"]}, {"guided", "unguided"})
        self.assertEqual(len(record["runs"]), 6)
        guided = [r for r in record["runs"] if r["arm"] == "guided"]
        self.assertTrue(all(r["guide_turns"] >= 1 for r in guided))
        self.assertTrue(all(r["guide_turns"] == 0 for r in record["runs"] if r["arm"] == "unguided"))
        for run in record["runs"]:
            self.assertIn("trace", run)
            self.assertGreater(run["api_calls"], 0)
            self.assertEqual(run["budget"], T.TASKS_BY_ID[run["task"]].call_budget())
        text = json.dumps(record)
        self.assertNotIn("selftest-not-a-credential", text)
        self.assertNotIn("http://", text)

    def run_selftest(self, *extra):
        env = dict(os.environ, HERMES_DISABLE_LAZY_INSTALLS="1")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "selftest.json"
            done = subprocess.run(
                [os.environ["HERMES_PY"], str(HERE / "run_ab.py"), "--selftest", "--runs-per-arm", "1",
                 "--tasks", "s01-basic", "--out", str(out), *extra],
                capture_output=True, text=True, timeout=240, env=env,
            )
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            return json.loads(out.read_text(encoding="utf-8"))

    def test_the_call_budget_is_exact_and_an_exhausted_run_is_a_recorded_failure(self):
        record = self.run_selftest("--model", "mock-loop", "--call-budget", "6")
        self.assertEqual(len(record["runs"]), 2)
        for run in record["runs"]:
            self.assertFalse(run["passed"])
            self.assertEqual(run["api_calls"], 6, "no extra summary call after the budget")
            self.assertTrue(run["budget_exhausted"])
            self.assertIn("call budget exhausted", run["failure_reason"])
        self.assertEqual(record["summary"]["arms"]["guided"]["passed"], 0)

    def test_a_provider_error_is_recorded_not_retried_into_a_pass(self):
        record = self.run_selftest("--model", "mock-error")
        for run in record["runs"]:
            self.assertFalse(run["passed"])
            self.assertTrue(run["provider_error"])
            self.assertTrue(run["failure_reason"].startswith("provider error"))
        self.assertEqual(record["summary"]["arms"]["unguided"]["provider_error_runs"], 1)

    def test_a_follower_that_reads_first_fails_at_the_target_in_both_arms(self):
        record = self.run_selftest("--model", "mock-naive")
        for run in record["runs"]:
            self.assertFalse(run["passed"])
            self.assertIn("read_before_lookup", run["failure_reason"])
            self.assertEqual(run["gate_blocks"], 0)
        guided = [r for r in record["runs"] if r["arm"] == "guided"][0]
        self.assertEqual(guided["guided_state"]["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
