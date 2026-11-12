"""Workflow IR tests. Fixtures show how a compiler emits exact and semantic programs."""

from __future__ import annotations

import copy
import json
import math
import sys
import unittest
from pathlib import Path

_FIX = Path(__file__).resolve().parent / "fixtures"
if str(_FIX) not in sys.path:
    sys.path.insert(0, str(_FIX))

from runtime_support import load_fixture  # noqa: E402

from proof_run.workflow import (  # noqa: E402
    WorkflowError,
    eval_predicate,
    resolve_binding,
    validate_decision,
    validate_inputs,
    validate_workflow,
    workflow_digest,
)


def _exact():
    return load_fixture("exact_ensure_fixture_release.json")


def _semantic():
    return load_fixture("semantic_route_ticket.json")


class WorkflowErrorTests(unittest.TestCase):
    def test_code_attribute(self):
        err = WorkflowError("INPUT_INVALID", "bad")
        self.assertEqual(err.code, "INPUT_INVALID")
        self.assertIn("bad", str(err))


class ValidateWorkflowTests(unittest.TestCase):
    def test_exact_and_semantic_fixtures_validate(self):
        exact = validate_workflow(_exact())
        self.assertEqual(exact["format"], "proof.workflow/v1")
        self.assertEqual(exact["workflow_id"], "ensure-fixture-release")
        self.assertEqual(exact["limits"]["max_effects"], 1)
        self.assertEqual(exact["limits"]["max_model_calls"], 0)
        self.assertIsNone(exact["model_route"])
        kinds = [n["kind"] for n in exact["nodes"]]
        self.assertEqual(kinds.count("effect"), 1)
        self.assertNotIn("decide", kinds)
        semantic = validate_workflow(_semantic())
        self.assertEqual(semantic["workflow_id"], "route-support-ticket")
        self.assertEqual(semantic["limits"]["max_model_calls"], 1)
        self.assertIsInstance(semantic["model_route"], dict)
        self.assertEqual([n["kind"] for n in semantic["nodes"]].count("decide"), 1)
        self.assertEqual([n["kind"] for n in semantic["nodes"]].count("effect"), 1)

    def test_digest_is_stable_and_ignores_revision_assignment(self):
        a = validate_workflow(_exact())
        b = validate_workflow(_exact())
        self.assertEqual(workflow_digest(a), workflow_digest(b))
        self.assertTrue(workflow_digest(a).startswith("sha256:"))
        mutated = copy.deepcopy(a)
        mutated["nodes"][0]["id"] = "renamed"
        self.assertNotEqual(workflow_digest(a), workflow_digest(mutated))

    def test_rejects_second_effect_and_eval_bindings(self):
        doc = _exact()
        extra = copy.deepcopy(doc["nodes"][2])
        extra["id"] = "deploy_again"
        doc["nodes"].append(extra)
        with self.assertRaises(WorkflowError) as ctx:
            validate_workflow(doc)
        self.assertEqual(ctx.exception.code, "WORKFLOW_INVALID")
        doc = _exact()
        doc["nodes"][0]["args"]["service"] = {"eval": "1+1"}
        with self.assertRaises(WorkflowError) as ctx:
            validate_workflow(doc)
        self.assertEqual(ctx.exception.code, "WORKFLOW_INVALID")

    def test_rejects_forward_node_reference_and_unknown_kind(self):
        doc = _exact()
        doc["nodes"][0]["args"]["service"] = {"node": "goal", "pointer": "/status"}
        with self.assertRaises(WorkflowError) as ctx:
            validate_workflow(doc)
        self.assertEqual(ctx.exception.code, "WORKFLOW_INVALID")
        doc = _exact()
        doc["nodes"][1]["kind"] = "shell"
        with self.assertRaises(WorkflowError) as ctx:
            validate_workflow(doc)
        self.assertEqual(ctx.exception.code, "WORKFLOW_INVALID")

    def test_effect_may_name_own_intent_not_own_future_result_node(self):
        doc = _exact()
        validated = validate_workflow(doc)
        effect = [n for n in validated["nodes"] if n["kind"] == "effect"][0]
        self.assertEqual(
            effect["args"]["operation_key"]["effect_intent"], effect["id"]
        )
        doc = _exact()
        doc["nodes"][2]["args"]["version"] = {"node": "deploy", "pointer": "/result"}
        with self.assertRaises(WorkflowError) as ctx:
            validate_workflow(doc)
        self.assertEqual(ctx.exception.code, "WORKFLOW_INVALID")


class InputAndDecisionTests(unittest.TestCase):
    def test_inputs_reject_missing_extra_wrong_type_nonfinite(self):
        wf = validate_workflow(_semantic())
        with self.assertRaises(WorkflowError) as ctx:
            validate_inputs(wf, {})
        self.assertEqual(ctx.exception.code, "INPUT_INVALID")
        with self.assertRaises(WorkflowError) as ctx:
            validate_inputs(wf, {"ticket_id": "T-21", "extra": 1})
        self.assertEqual(ctx.exception.code, "INPUT_INVALID")
        with self.assertRaises(WorkflowError) as ctx:
            validate_inputs(wf, {"ticket_id": 21})
        self.assertEqual(ctx.exception.code, "INPUT_INVALID")
        with self.assertRaises(WorkflowError) as ctx:
            validate_inputs(wf, {"ticket_id": "nope"})
        self.assertEqual(ctx.exception.code, "INPUT_INVALID")
        ok = validate_inputs(wf, {"ticket_id": "T-21"})
        self.assertEqual(ok["ticket_id"], "T-21")
        exact = validate_workflow(_exact())
        self.assertEqual(validate_inputs(exact, {}), {})
        with self.assertRaises(WorkflowError) as ctx:
            validate_inputs(exact, {"x": 1})
        self.assertEqual(ctx.exception.code, "INPUT_INVALID")
        number_wf = copy.deepcopy(wf)
        number_wf["inputs"] = {"n": {"type": "number"}}
        with self.assertRaises(WorkflowError) as ctx:
            validate_inputs(number_wf, {"n": math.nan})
        self.assertEqual(ctx.exception.code, "INPUT_INVALID")

    def test_closed_decision_validation(self):
        candidates = ["billing", "access", "incident"]
        evidence = ["s1", "s2"]
        select = validate_decision(
            {"kind": "select", "queue": "billing", "evidence_ids": ["s1"]},
            candidates=candidates,
            evidence_ids=evidence,
            allow_abstain=True,
        )
        self.assertEqual(select["queue"], "billing")
        abstain = validate_decision(
            {"kind": "abstain", "reason": "ambiguous"},
            candidates=candidates,
            evidence_ids=evidence,
            allow_abstain=True,
        )
        self.assertEqual(abstain["kind"], "abstain")
        with self.assertRaises(WorkflowError) as ctx:
            validate_decision(
                {"kind": "select", "queue": "legal", "evidence_ids": ["s1"]},
                candidates=candidates,
                evidence_ids=evidence,
                allow_abstain=True,
            )
        self.assertEqual(ctx.exception.code, "MODEL_OUTPUT_INVALID")
        with self.assertRaises(WorkflowError) as ctx:
            validate_decision(
                {
                    "kind": "select",
                    "queue": "billing",
                    "evidence_ids": ["s1"],
                    "tools": ["shell"],
                },
                candidates=candidates,
                evidence_ids=evidence,
                allow_abstain=True,
            )
        self.assertEqual(ctx.exception.code, "MODEL_OUTPUT_INVALID")
        with self.assertRaises(WorkflowError) as ctx:
            validate_decision(
                {"kind": "abstain", "reason": "ambiguous"},
                candidates=candidates,
                evidence_ids=evidence,
                allow_abstain=False,
            )
        self.assertEqual(ctx.exception.code, "MODEL_OUTPUT_INVALID")


class BindingPredicateTests(unittest.TestCase):
    def test_tagged_bindings_and_no_coercion(self):
        ctx = {
            "inputs": {"ticket_id": "T-21"},
            "constants": {"service": "fixture-api"},
            "host_context": {"tenant_id": "tenant-A"},
            "nodes": {"ticket": {"tenant_id": "tenant-A", "n": 1}},
            "effect_intents": {
                "route": {"operation_key": "K1", "payload": {"queue": "billing"}}
            },
        }
        self.assertEqual(resolve_binding({"literal": "v1"}, ctx), "v1")
        self.assertEqual(resolve_binding({"input": "/ticket_id"}, ctx), "T-21")
        self.assertEqual(resolve_binding({"constant": "/service"}, ctx), "fixture-api")
        self.assertEqual(
            resolve_binding({"host_context": "/tenant_id"}, ctx), "tenant-A"
        )
        self.assertEqual(
            resolve_binding({"node": "ticket", "pointer": "/tenant_id"}, ctx),
            "tenant-A",
        )
        self.assertEqual(
            resolve_binding({"effect_intent": "route", "pointer": "/payload/queue"}, ctx),
            "billing",
        )
        self.assertTrue(
            eval_predicate(
                {
                    "op": "eq",
                    "left": {"literal": 1},
                    "right": {"node": "ticket", "pointer": "/n"},
                },
                ctx,
            )
        )
        self.assertFalse(
            eval_predicate(
                {"op": "eq", "left": {"literal": "1"}, "right": {"literal": 1}},
                ctx,
            )
        )
        self.assertTrue(
            eval_predicate(
                {"op": "in", "left": {"literal": "a"}, "right": {"literal": ["a", "b"]}},
                ctx,
            )
        )

    def test_compiler_fixture_comment_contract(self):
        # Operator compiler emits tagged bindings only; these fixtures are the
        # supported exact (no decide) and semantic (one decide) programs.
        exact_text = json.dumps(_exact())
        semantic_text = json.dumps(_semantic())
        self.assertNotIn('"eval"', exact_text)
        self.assertNotIn("shell", semantic_text)
        self.assertIn('"kind": "effect"', exact_text)
        self.assertIn('"kind": "decide"', semantic_text)


if __name__ == "__main__":
    unittest.main()
