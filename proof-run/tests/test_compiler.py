"""Compiler: trace + explicit review bundle, no invented branches."""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

_FIX = Path(__file__).resolve().parent / "fixtures"
if str(_FIX) not in sys.path:
    sys.path.insert(0, str(_FIX))

from operator_support import (  # noqa: E402
    ADAPTER_CATALOG,
    REVIEW_EXACT,
    REVIEW_TICKET,
    teaching_ticket_trace_receipts,
)

from proof_run import compiler  # noqa: E402
from proof_run.workflow import validate_workflow  # noqa: E402


def _trace(receipts=None, **extra):
    data = {
        "name": extra.get("name", "route-ticket"),
        "intent": "route a support ticket",
        "session_id": "sess-1",
        "profile_id": "test-profile",
        "receipts": receipts if receipts is not None else teaching_ticket_trace_receipts(),
        "complete": True,
        "teacher_outcome_assurance": extra.get("teacher_outcome_assurance", "effect_observed"),
        "source": extra.get("source", "scripted-operator-teacher"),
    }
    data.update({k: v for k, v in extra.items() if k not in data})
    return data


class CompilerTests(unittest.TestCase):
    def test_compile_from_teaching_receipts_not_prewritten_recipe(self):
        wf = compiler.compile_from_trace(_trace(), ADAPTER_CATALOG, REVIEW_TICKET)
        validated = validate_workflow(wf)
        self.assertEqual(validated["format"], "proof.workflow/v1")
        self.assertEqual(validated["workflow_id"], "route-ticket")
        tools_in_order = [n.get("tool") for n in validated["nodes"] if n["kind"] in {"read", "effect"}]
        self.assertEqual(tools_in_order[0], "example_ticket_get")
        self.assertIn("example_ticket_route_if_version", tools_in_order)
        self.assertEqual(tools_in_order[-1], "example_ticket_get")
        kinds = [n["kind"] for n in validated["nodes"]]
        self.assertIn("decide", kinds)
        self.assertEqual(kinds.count("effect"), 1)
        self.assertEqual(
            validated["provenance"]["teacher_outcome_assurance"], "effect_observed"
        )
        self.assertTrue(validated["provenance"]["actual_demonstration_receipt"])
        refs = validated["provenance"]["source_receipt_refs"]
        self.assertTrue(refs)

    def test_one_billing_example_does_not_invent_queues(self):
        slim = copy.deepcopy(REVIEW_TICKET)
        slim["authority"] = copy.deepcopy(slim["authority"])
        slim["authority"].pop("queue_candidates", None)
        slim.pop("decision", None)
        wf = compiler.compile_from_trace(_trace(), ADAPTER_CATALOG, slim)
        unresolved = wf.get("unresolved") or []
        self.assertTrue(unresolved)
        text = json_blob(unresolved)
        self.assertTrue(
            any("queue" in str(item).lower() or "decision" in str(item).lower() or "candidate" in str(item).lower() for item in unresolved)
            or "queue" in text.lower()
        )
        decide = [n for n in wf.get("nodes") or [] if n.get("kind") == "decide"]
        if decide:
            self.assertEqual(decide[0].get("candidates"), ["billing"])

    def test_review_bundle_supplies_authorized_queues(self):
        wf = compiler.compile_from_trace(_trace(), ADAPTER_CATALOG, REVIEW_TICKET)
        decide = [n for n in wf["nodes"] if n["kind"] == "decide"][0]
        self.assertEqual(decide["candidates"], ["billing", "access", "incident"])
        self.assertEqual(
            wf["authority"]["queue_candidates"], ["billing", "access", "incident"]
        )

    def test_compiler_cannot_upgrade_teacher_assurance(self):
        wf = compiler.compile_from_trace(
            _trace(teacher_outcome_assurance="not_measured"),
            ADAPTER_CATALOG,
            {**REVIEW_TICKET, "teacher_outcome_assurance": "domain_goal_verified"},
        )
        self.assertEqual(wf["provenance"]["teacher_outcome_assurance"], "not_measured")
        self.assertNotEqual(wf["provenance"]["teacher_outcome_assurance"], "domain_goal_verified")

    def test_known_failed_teacher_is_preserved(self):
        wf = compiler.compile_from_trace(
            _trace(teacher_outcome_assurance="domain_goal_failed"),
            ADAPTER_CATALOG,
            REVIEW_TICKET,
        )
        self.assertEqual(wf["provenance"]["teacher_outcome_assurance"], "domain_goal_failed")

    def test_schema2_import_without_receipts_is_not_runnable(self):
        contract = {
            "schema_version": 2,
            "contract_id": "deploy-api",
            "bound": True,
            "steps": [
                {"tool": "check_service_health", "args": {"service": "api"}},
                {"tool": "deploy_service", "args": {"service": "api", "version": "2.1"}},
            ],
        }
        wf = compiler.compile_schema2_import(contract, REVIEW_EXACT)
        self.assertFalse(wf["provenance"].get("actual_demonstration_receipt"))
        unresolved = wf.get("unresolved") or []
        self.assertTrue(unresolved)
        blob = json_blob(unresolved)
        self.assertTrue(
            "DEMO_EVIDENCE_INCOMPLETE" in blob
            or "receipt" in blob.lower()
            or "evidence" in blob.lower()
        )

    def test_missing_review_leaves_unresolved_not_guessed_ranges(self):
        wf = compiler.compile_from_trace(_trace(), ADAPTER_CATALOG, {})
        self.assertTrue(wf.get("unresolved"))
        text = json_blob(wf)
        self.assertNotIn("min", json_blob(wf.get("inputs") or {}))
        self.assertTrue("unresolved" in wf)

    def test_exact_review_has_no_decide_and_null_model_route(self):
        receipts = []
        for call_id, tool, args in [
            ("h1", "check_service_health", {"service": "fixture-api"}),
            ("d1", "deploy_service", {"service": "fixture-api", "version": "v1"}),
            ("h2", "check_service_health", {"service": "fixture-api"}),
        ]:
            from operator_support import receipt as mk

            receipts.append(mk(call_id, phase="before_dispatch", tool_name=tool, args=args))
            receipts.append(
                mk(
                    call_id,
                    phase="terminal",
                    tool_name=tool,
                    args=args,
                    result='{"service":"fixture-api","status":"healthy","version":"v1"}',
                )
            )
        wf = compiler.compile_from_trace(
            _trace(receipts, name="ensure-fixture-release"),
            ADAPTER_CATALOG,
            REVIEW_EXACT,
        )
        validated = validate_workflow(wf)
        self.assertIsNone(validated["model_route"])
        self.assertEqual(validated["limits"]["max_model_calls"], 0)
        self.assertNotIn("decide", [n["kind"] for n in validated["nodes"]])
        self.assertEqual(validated["inputs"], {})

    def test_live_revisions_are_not_teacher_literals(self):
        wf = compiler.compile_from_trace(_trace(), ADAPTER_CATALOG, REVIEW_TICKET)
        effect = [n for n in wf["nodes"] if n["kind"] == "effect"][0]
        self.assertEqual(
            effect["args"]["expected_source_revision"],
            {"node": "ticket", "pointer": "/source_revision"},
        )
        self.assertNotIn("literal", effect["args"]["expected_source_revision"])
        self.assertEqual(
            effect["args"]["ticket_id"],
            {"node": "ticket", "pointer": "/ticket_id"},
        )

    def test_pending_call_is_compiler_incomplete(self):
        from operator_support import receipt as mk

        receipts = teaching_ticket_trace_receipts()
        receipts.append(mk("pending", phase="before_dispatch"))
        wf = compiler.compile_from_trace(
            _trace(receipts, complete=False, call_ids=["c-get-1", "c-route-1", "c-get-2", "pending"]),
            ADAPTER_CATALOG,
            REVIEW_TICKET,
        )
        self.assertTrue(
            any(item.get("code") == "DEMO_EVIDENCE_INCOMPLETE" for item in wf.get("unresolved") or [])
        )


def json_blob(value):
    import json

    return json.dumps(value, default=str)


if __name__ == "__main__":
    unittest.main()
