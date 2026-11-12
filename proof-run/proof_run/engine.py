"""Deterministic workflow scheduler. The follower never owns tools or the PC."""

from __future__ import annotations

import copy
import secrets
from typing import Any, Protocol

from .workflow import (
    WorkflowError,
    bind_args,
    compute_operation_key,
    compute_resource_key,
    eval_predicate,
    parse_json_result,
    resolve_binding,
    segment_evidence,
    validate_decision,
)


class HostPort(Protocol):
    def capabilities(self) -> dict: ...
    def authenticated_context(self) -> dict: ...
    def execute_checked(
        self, tool_name: str, args: dict, *, run_context: dict
    ) -> dict: ...
    def operation_status(self, intent: dict, *, run_context: dict) -> dict: ...
    def progress(self, message: str, *, run_context: dict) -> None: ...


class Follower(Protocol):
    def identity(self) -> dict: ...
    def decide(self, packet: dict) -> dict: ...


_TERMINAL = {
    "completed",
    "verified",
    "blocked",
    "failed",
    "needs_review",
    "unknown_outcome",
    "canceled",
}
_REQUIRED_CAPS = (
    "final_authorization",
    "mandatory_receipts",
    "bound_context",
)


class WorkflowRunner:
    def __init__(self, store: Any, host: Any, follower: Any = None):
        self.store = store
        self.host = host
        self.follower = follower

    def drive(self, run_id: str) -> dict:
        return self._execute(run_id, reconcile_unknown=False)

    def resume(self, run_id: str) -> dict:
        return self._execute(run_id, reconcile_unknown=True)

    def cancel(self, run_id: str) -> dict:
        return self.store.request_cancel(run_id)

    def _execute(self, run_id: str, reconcile_unknown: bool = False) -> dict:
        run = self.store.get_run(run_id)
        if run["state"] in _TERMINAL:
            if not (reconcile_unknown and run["state"] == "unknown_outcome"):
                return run
        if run["cancel_requested"] and not run["dispatch_started"]:
            return self._finish(
                run_id,
                "canceled",
                effect_readback="not_applicable",
                semantic_judgment="not_applicable",
                whole_goal="unverified",
            )
        caps = self.host.capabilities()
        if not self._caps_ok(caps):
            return self._finish(
                run_id,
                "blocked",
                error_code="HOST_CAPABILITY_MISSING",
                effect_readback="not_applicable",
                semantic_judgment="not_applicable",
                whole_goal="unverified",
            )
        worker_id = "W-" + secrets.token_hex(8)
        lease = self.store.acquire_lease(run_id, worker_id)
        wf = self.store.load_workflow_for_run(run)
        auth = self.host.authenticated_context()
        actor = run.get("actor") or {}
        if not self._actor_matches_auth(actor, auth):
            return self._finish(
                run_id,
                "blocked",
                error_code="AUTH_CONTEXT_MISSING",
                effect_readback="not_applicable",
                semantic_judgment="not_applicable",
                whole_goal="unverified",
            )
        run_context = {
            "run_id": run_id,
            "profile_id": actor.get("profile_id"),
            "session_id": actor.get("session_id"),
            "workflow_digest": run["content_digest"],
            "fence": lease["fence"],
            "intent_ref": {},
        }
        self.host.progress(f"run {run_id}", run_context=run_context)
        ctx = {
            "inputs": copy.deepcopy(run["inputs"]),
            "constants": copy.deepcopy(wf.get("constants") or {}),
            "host_context": copy.deepcopy(auth),
            "nodes": copy.deepcopy(run.get("node_outputs") or {}),
            "effect_intents": self.store.list_effect_intents(run_id),
        }
        if run["dispatch_started"] and run.get("dispatch_node") not in ctx["nodes"]:
            outcome = self._reconcile(run, wf, ctx, run_context)
            if outcome is not None:
                return outcome
            run = self.store.get_run(run_id)
        if run["cancel_requested"] and not self._effect_committed(ctx):
            return self._finish(
                run_id,
                "canceled",
                effect_readback="not_applicable",
                semantic_judgment="not_applicable",
                whole_goal="unverified",
            )
        if run["state"] == "created":
            self.store.update_run(run_id, state="running")
        nodes = wf["nodes"]
        cursor = int(run["cursor"])
        had_decide = self._decide_recorded(wf, ctx)
        effect_done = self._effect_recorded(wf, ctx)
        post_effect_reads = 0
        post_effect_goals = 0
        try:
            while cursor < len(nodes):
                run = self.store.get_run(run_id)
                if run["cancel_requested"] and not run["dispatch_started"]:
                    return self._finish(
                        run_id,
                        "canceled",
                        effect_readback="not_applicable",
                        semantic_judgment="not_applicable",
                        whole_goal="unverified",
                    )
                node = nodes[cursor]
                self.host.progress(f"step {node['id']}", run_context=run_context)
                kind = node["kind"]
                if kind == "read":
                    args = bind_args(node["args"], ctx)
                    receipt = self.host.execute_checked(
                        node["tool"], args, run_context=run_context
                    )
                    decoded = self._decode_read_receipt(
                        receipt, post_effect=effect_done
                    )
                    for field in node.get("required_fields") or []:
                        if field not in decoded:
                            raise WorkflowError(
                                "STATE_DRIFT", f"missing required field {field}"
                            )
                    ctx["nodes"][node["id"]] = decoded
                    if effect_done:
                        post_effect_reads += 1
                elif kind == "assert":
                    for check in node["checks"]:
                        if not eval_predicate(check, ctx):
                            code = node.get("on_failure")
                            if not code:
                                code = (
                                    "VERIFICATION_FAILED"
                                    if ctx["effect_intents"]
                                    else "STATE_DRIFT"
                                )
                            raise WorkflowError(code, f"assertion {node['id']} failed")
                    ctx["nodes"][node["id"]] = {"ok": True}
                    if effect_done and post_effect_reads:
                        post_effect_goals += 1
                elif kind == "decide":
                    had_decide = True
                    if node["id"] not in ctx["nodes"]:
                        self._assert_follower_route(wf)
                        source = resolve_binding(node["source"], ctx)
                        segments = segment_evidence(source)
                        evidence_ids = [item["id"] for item in segments]
                        policy = (
                            copy.deepcopy(wf["policy"])
                            if isinstance(wf.get("policy"), dict)
                            else None
                        )
                        packet = {
                            "instruction": "Select one authorized queue or abstain.",
                            "policy": policy,
                            "policy_revision": (policy or {}).get("revision"),
                            "observation": {"untrusted": True, "body": source},
                            "candidates": list(node["candidates"]),
                            "evidence_ids": evidence_ids,
                            "evidence": segments,
                            "output_schema": node.get("output", "queue-decision/v1"),
                            "allow_abstain": bool(node.get("allow_abstain", True)),
                        }
                        raw = self.follower.decide(packet)
                        decision = validate_decision(
                            raw,
                            candidates=list(node["candidates"]),
                            evidence_ids=evidence_ids,
                            allow_abstain=bool(node.get("allow_abstain", True)),
                        )
                        if decision["kind"] == "abstain":
                            ctx["nodes"][node["id"]] = decision
                            self._save_progress(run_id, cursor, ctx)
                            return self._finish(
                                run_id,
                                "needs_review",
                                error_code="NEEDS_REVIEW",
                                effect_readback="not_applicable",
                                semantic_judgment="abstained",
                                whole_goal="unverified",
                                teacher_outcome_assurance=wf["provenance"][
                                    "teacher_outcome_assurance"
                                ],
                            )
                        ctx["nodes"][node["id"]] = decision
                elif kind == "effect":
                    outcome = self._run_effect(
                        run_id, node, wf, ctx, run_context, lease["fence"]
                    )
                    if outcome is not None:
                        return outcome
                    effect_done = True
                else:
                    raise WorkflowError("WORKFLOW_INVALID", f"unknown kind {kind}")
                cursor += 1
                self._save_progress(run_id, cursor, ctx)
        except WorkflowError as exc:
            return self._fail(run_id, wf, ctx, exc)
        if effect_done or ctx["effect_intents"]:
            if post_effect_reads >= 1 and post_effect_goals >= 1:
                effect_readback = "verified"
            else:
                effect_readback = "unverified"
        else:
            effect_readback = "not_applicable"
        had_decide = had_decide or self._decide_recorded(wf, ctx)
        if had_decide:
            state = "completed"
            semantic = "not_independently_verified"
            whole = "unverified"
        else:
            state = "verified" if effect_readback == "verified" else "completed"
            semantic = "not_applicable"
            whole = "verified" if effect_readback == "verified" else "unverified"
        for intent in ctx["effect_intents"].values():
            self.store.release_hazard(intent["resource_key"], run_id)
        return self._finish(
            run_id,
            state,
            effect_readback=effect_readback,
            semantic_judgment=semantic,
            whole_goal=whole,
            teacher_outcome_assurance=wf["provenance"]["teacher_outcome_assurance"],
        )

    def _run_effect(
        self,
        run_id: str,
        node: dict,
        wf: dict,
        ctx: dict,
        run_context: dict,
        fence: int,
    ) -> dict | None:
        if node["id"] in ctx["nodes"]:
            return None
        partial = bind_args(node["args"], ctx, skip_self_intent=node["id"])
        operation_key = compute_operation_key(node["operation_identity"], ctx)
        resource_key = compute_resource_key(node["resource"], ctx)
        payload = copy.deepcopy(partial)
        payload["operation_key"] = operation_key
        try:
            intent = self.store.persist_effect_intent(
                run_id=run_id,
                node_id=node["id"],
                operation_key=operation_key,
                resource_key=resource_key,
                payload=payload,
                expectations={"payload": copy.deepcopy(payload)},
                decision_provenance={"node_id": node["id"]},
                fence=fence,
            )
        except WorkflowError:
            raise
        ctx["effect_intents"][node["id"]] = intent
        remaining = {}
        for key, spec in node["args"].items():
            if isinstance(spec, dict) and spec.get("effect_intent") == node["id"]:
                remaining[key] = resolve_binding(spec, ctx)
        payload.update(remaining)
        if canonical_mismatch(intent["payload"], payload):
            raise WorkflowError(
                "PAYLOAD_MISMATCH", "effect payload drifted from persisted intent"
            )
        run_context["intent_ref"] = {"operation_key": operation_key}
        if intent.get("state") in {"reconciled"}:
            ctx["nodes"][node["id"]] = copy.deepcopy(intent["payload"])
            return None
        self.store.mark_dispatch_started(run_id, node["id"], fence)
        try:
            receipt = self.host.execute_checked(
                node["tool"], payload, run_context=run_context
            )
        except WorkflowError:
            raise
        disposition = receipt.get("disposition")
        dispatched = bool(receipt.get("dispatched"))
        if disposition in {"unknown", "handler_error"} or (
            dispatched and disposition not in {"completed", "blocked"}
        ):
            self.store.mark_hazard_unknown(resource_key, run_id)
            return self._finish(
                run_id,
                "unknown_outcome",
                error_code="UNKNOWN_OUTCOME",
                effect_readback="unknown",
                semantic_judgment=(
                    "not_independently_verified"
                    if any(n["kind"] == "decide" for n in wf["nodes"])
                    else "not_applicable"
                ),
                whole_goal="unverified",
                teacher_outcome_assurance=wf["provenance"]["teacher_outcome_assurance"],
            )
        if disposition == "blocked" or not dispatched:
            return self._finish(
                run_id,
                "blocked",
                error_code="APPROVAL_DENIED",
                effect_readback="not_applicable",
                semantic_judgment="not_applicable",
                whole_goal="unverified",
            )
        decoded = parse_json_result(receipt.get("result"))
        ctx["nodes"][node["id"]] = decoded if isinstance(decoded, dict) else {"result": decoded}
        return None

    def _reconcile(
        self, run: dict, wf: dict, ctx: dict, run_context: dict
    ) -> dict | None:
        node_id = run.get("dispatch_node")
        intents = ctx["effect_intents"]
        intent = intents.get(node_id) if node_id else None
        if intent is None and intents:
            intent = next(iter(intents.values()))
        if intent is None:
            return self._finish(
                run_id=run["run_id"],
                state="unknown_outcome",
                error_code="UNKNOWN_OUTCOME",
                effect_readback="unknown",
                semantic_judgment="not_applicable",
                whole_goal="unverified",
            )
        recovery_raw = wf.get("recovery")
        recovery = recovery_raw if isinstance(recovery_raw, dict) else {}
        status_intent = {
            "operation_key": intent["operation_key"],
            "payload": intent["payload"],
            "node_id": intent["node_id"],
        }
        status_tool = recovery.get("tool")
        if isinstance(status_tool, str) and status_tool:
            status_intent["status_tool"] = status_tool
        prior_intent = run_context.get("intent_ref")
        run_context["intent_ref"] = {"operation_key": intent["operation_key"]}
        try:
            status = self.host.operation_status(
                status_intent,
                run_context=run_context,
            )
        finally:
            run_context["intent_ref"] = prior_intent if prior_intent is not None else {}
        state = status.get("state")
        if state == "committed" and not canonical_mismatch(
            status.get("payload"), intent["payload"]
        ):
            ctx["nodes"][intent["node_id"]] = copy.deepcopy(intent["payload"])
            nodes = wf["nodes"]
            cursor = int(run["cursor"])
            while cursor < len(nodes) and nodes[cursor]["id"] != intent["node_id"]:
                cursor += 1
            if cursor < len(nodes) and nodes[cursor]["id"] == intent["node_id"]:
                cursor += 1
            self._save_progress(run["run_id"], cursor, ctx)
            self.store.update_run(run["run_id"], dispatch_started=0, dispatch_node=None)
            return None
        self.store.mark_hazard_unknown(intent["resource_key"], run["run_id"])
        return self._finish(
            run["run_id"],
            "unknown_outcome",
            error_code="UNKNOWN_OUTCOME",
            effect_readback="unknown",
            semantic_judgment=(
                "not_independently_verified"
                if any(n["kind"] == "decide" for n in wf["nodes"])
                else "not_applicable"
            ),
            whole_goal="unverified",
            teacher_outcome_assurance=wf["provenance"]["teacher_outcome_assurance"],
        )

    def _save_progress(self, run_id: str, cursor: int, ctx: dict) -> None:
        self.store.update_run(
            run_id,
            cursor=cursor,
            node_outputs=copy.deepcopy(ctx["nodes"]),
            state="running",
        )

    def _fail(self, run_id: str, wf: dict, ctx: dict, exc: WorkflowError) -> dict:
        code = exc.code
        if code in {
            "TARGET_IDENTITY_MISMATCH",
            "STATE_DRIFT",
            "HOST_CAPABILITY_MISSING",
            "MODEL_ROUTE_REQUIRED",
            "MODEL_ROUTE_MISMATCH",
            "AUTH_CONTEXT_MISSING",
            "MODEL_OUTPUT_INVALID",
            "INTENT_CONFLICT",
            "HAZARD_CONFLICT",
            "PAYLOAD_MISMATCH",
            "CANCELLED",
            "FENCE_STALE",
            "INPUT_INVALID",
            "WORKFLOW_NOT_PROMOTED",
        }:
            state = "blocked" if code != "CANCELLED" else "canceled"
            readback = "not_applicable"
        elif code == "VERIFICATION_FAILED":
            state = "failed"
            readback = "failed"
        elif code == "UNKNOWN_OUTCOME":
            state = "unknown_outcome"
            readback = "unknown"
        else:
            state = "blocked"
            readback = "not_applicable"
        had_decide = any(n["kind"] == "decide" for n in wf["nodes"])
        if state == "unknown_outcome":
            for intent in ctx["effect_intents"].values():
                self.store.mark_hazard_unknown(intent["resource_key"], run_id)
        return self._finish(
            run_id,
            state,
            error_code=code,
            effect_readback=readback,
            semantic_judgment=(
                "not_independently_verified" if had_decide else "not_applicable"
            ),
            whole_goal="unverified",
            teacher_outcome_assurance=wf["provenance"]["teacher_outcome_assurance"],
        )

    def _finish(
        self,
        run_id: str,
        state: str,
        *,
        error_code: str | None = None,
        effect_readback: str = "not_applicable",
        semantic_judgment: str = "not_applicable",
        whole_goal: str = "unverified",
        teacher_outcome_assurance: str | None = None,
    ) -> dict:
        result = {
            "effect_readback": effect_readback,
            "semantic_judgment": semantic_judgment,
            "whole_goal": whole_goal,
        }
        if teacher_outcome_assurance is not None:
            result["teacher_outcome_assurance"] = teacher_outcome_assurance
        return self.store.update_run(
            run_id,
            state=state,
            error_code=error_code,
            result=result,
        )

    def _caps_ok(self, caps: dict) -> bool:
        if not isinstance(caps, dict):
            return False
        version = caps.get("version")
        if not isinstance(version, str) or not version.startswith("checked-execution/"):
            return False
        return all(caps.get(key) is True for key in _REQUIRED_CAPS)

    def _effect_committed(self, ctx: dict) -> bool:
        return bool(ctx.get("effect_intents"))

    def _decide_recorded(self, wf: dict, ctx: dict) -> bool:
        nodes = ctx.get("nodes") or {}
        return any(n.get("kind") == "decide" and n.get("id") in nodes for n in wf["nodes"])

    def _effect_recorded(self, wf: dict, ctx: dict) -> bool:
        nodes = ctx.get("nodes") or {}
        return any(n.get("kind") == "effect" and n.get("id") in nodes for n in wf["nodes"])

    def _actor_matches_auth(self, actor: dict, auth: dict) -> bool:
        if not isinstance(actor, dict) or not isinstance(auth, dict):
            return False
        for key in ("profile_id", "session_id"):
            pinned = actor.get(key)
            if not isinstance(pinned, str) or not pinned:
                return False
            if auth.get(key) != pinned:
                return False
        return True

    def _assert_follower_route(self, wf: dict) -> None:
        route = wf.get("model_route")
        if self.follower is None or not isinstance(route, dict):
            raise WorkflowError(
                "MODEL_ROUTE_REQUIRED", "no approved follower for decide"
            )
        identity = self.follower.identity()
        if not isinstance(identity, dict):
            raise WorkflowError(
                "MODEL_ROUTE_MISMATCH", "follower identity is not approved"
            )
        for field in ("model", "provider", "endpoint", "digest"):
            if field in route and route[field] != identity.get(field):
                raise WorkflowError(
                    "MODEL_ROUTE_MISMATCH", "follower identity is not approved"
                )

    def _decode_read_receipt(self, receipt: dict, *, post_effect: bool) -> dict:
        if not isinstance(receipt, dict):
            raise WorkflowError("STATE_DRIFT", "read result is not an object")
        dispatched = bool(receipt.get("dispatched"))
        disposition = receipt.get("disposition")
        if not dispatched or disposition != "completed":
            code = "VERIFICATION_FAILED" if post_effect else "STATE_DRIFT"
            raise WorkflowError(code, "read receipt is not successful evidence")
        decoded = parse_json_result(receipt.get("result"))
        if not isinstance(decoded, dict):
            raise WorkflowError("STATE_DRIFT", "read result is not an object")
        return decoded


def canonical_mismatch(left: Any, right: Any) -> bool:
    from .workflow import canonical_dumps

    try:
        return canonical_dumps(left) != canonical_dumps(right)
    except WorkflowError:
        return True
