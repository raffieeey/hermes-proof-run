"""Proof Run core: recorder, contract persistence, and enforcement gate.

Pure logic only; host adapters live in the plugin entry point.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


class ContractError(Exception):
    pass


@dataclass
class _RecordedCall:
    tool: str
    args: dict[str, Any]
    result: Any


# Tools that are safe to run without gating (reads, searches, listings).
READ_ONLY_TOOLS = {
    "read_file", "list_dir", "search_files", "web_search", "web_extract",
    "vision_analyze", "skill_view", "session_search", "memory",
    "stock_level", "terminal", "git_status", "git_log", "git_diff",
}


def _is_read_only(tool: str) -> bool:
    return tool in READ_ONLY_TOOLS


def _is_error_result(result: Any) -> bool:
    """True when the result indicates a failed tool call."""
    if isinstance(result, dict):
        return "error" in result or result.get("status") == "error" or result.get("success") is False
    if isinstance(result, str):
        return "error" in result.lower() or "failed" in result.lower()
    return result is None


class Recorder:
    """Capture one run and draft a reviewable contract. Persistence is atomic."""

    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()
        self._active: Optional[dict[str, Any]] = None

    def start(self, contract_id: str, intent: str) -> None:
        with self._lock:
            self._active = {"contract_id": contract_id, "intent": intent, "calls": []}

    def note_tool(self, tool: str, args: dict[str, Any], result: Any) -> None:
        with self._lock:
            if self._active is None:
                raise ContractError("no active recording")
            self._active["calls"].append(_RecordedCall(tool, dict(args), result))

    def stop(self, success: bool = True) -> dict[str, Any]:
        with self._lock:
            if self._active is None:
                raise ContractError("no active recording")

            contract_id = self._active["contract_id"]
            intent = self._active["intent"]
            calls = self._active["calls"]
            self._active = None

            # B6: detect failed runs from tool results, not just the flag.
            if not success:
                raise ContractError("run was marked failed by the caller")
            failed_calls = [c for c in calls if _is_error_result(c.result)]
            if failed_calls:
                names = ", ".join(c.tool for c in failed_calls)
                raise ContractError(f"run contained failed tool calls: {names}")

            mutating = [c for c in calls if not _is_read_only(c.tool)]
            if not mutating:
                raise ContractError("no mutating tool observed")

            reads = [c for c in calls if _is_read_only(c.tool)]

            # B3: derive preconditions from ALL read tools, not just 'stock_level'.
            preconditions = []
            for call in reads:
                if isinstance(call.result, dict):
                    for key, value in call.result.items():
                        if isinstance(value, (int, float, str, bool)):
                            preconditions.append({
                                "kind": "tool_fact", "tool": call.tool,
                                "args_match": dict(call.args), "field": key,
                                "op": ">=", "value": value,
                            })

            success_evidence = []
            for call in mutating:
                if isinstance(call.result, dict):
                    for key, value in call.result.items():
                        if isinstance(value, (int, float, str, bool)):
                            success_evidence.append({
                                "kind": "tool_result_field", "tool": call.tool,
                                "field": key, "op": "==", "value": value,
                            })

            contract = {
                "version": 1,
                "contract_id": contract_id,
                "intent": intent,
                "bound": False,
                "action_window": {
                    "mutating_tools": sorted({c.tool for c in mutating}),
                    "allowed_arg_patterns": {
                        tool: _arg_patterns([c.args for c in mutating if c.tool == tool])
                        for tool in sorted({c.tool for c in mutating})
                    },
                },
                "preconditions": preconditions,
                "success_evidence": success_evidence,
                "provenance": {"recorded_calls": len(calls)},
            }
        self._persist(contract)
        return contract

    def _persist(self, contract: dict[str, Any]) -> None:
        target = self.root / "contracts" / f"{contract['contract_id']}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            prior = json.loads(target.read_text(encoding="utf-8"))
            contract["version"] = int(prior.get("version", 0)) + 1
            contract["supersedes"] = prior.get("version")
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(contract, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(target)


def _arg_patterns(arg_sets: list[dict[str, Any]]) -> dict[str, Any]:
    patterns: dict[str, Any] = {}
    for args in arg_sets:
        for key, value in args.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cur = patterns.get(key)
                if cur is None:
                    patterns[key] = {"min": value, "max": value}
                else:
                    cur["min"] = min(cur["min"], value)
                    cur["max"] = max(cur["max"], value)
            else:
                patterns[key] = value
    return patterns


def check_contract(contract: dict[str, Any], *, runtime: dict[str, Any],
                   proposed_call: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return None for unrelated tools, else an allow/block verdict.

    B4: checks allowed_arg_patterns in addition to preconditions.
    """
    tool = proposed_call["tool"]
    args = proposed_call.get("args") or {}
    if tool not in contract["action_window"]["mutating_tools"]:
        return None

    # B4: check that the proposed args match the recorded arg patterns.
    patterns = contract["action_window"].get("allowed_arg_patterns", {}).get(tool, {})
    for key, pattern in patterns.items():
        actual = args.get(key)
        if actual is None:
            return {"action": "block", "message": f"Contract {contract['contract_id']}: argument {key} is missing"}
        if isinstance(pattern, dict) and "min" in pattern:
            if not isinstance(actual, (int, float)) or actual < pattern["min"] or actual > pattern["max"]:
                return {"action": "block", "message": f"Contract {contract['contract_id']}: argument {key}={actual} outside recorded range {pattern['min']}-{pattern['max']}"}
        elif actual != pattern:
            return {"action": "block", "message": f"Contract {contract['contract_id']}: argument {key}={actual} does not match recorded value {pattern}"}

    # Check preconditions.
    for precondition in contract.get("preconditions") or []:
        if precondition["kind"] != "tool_fact":
            continue
        runtime_tool = runtime.get(precondition["tool"])
        if runtime_tool is None:
            return {"action": "block", "message": f"Contract {contract['contract_id']}: precondition {precondition['tool']}.{precondition['field']} is unknown"}
        current = runtime_tool.get(precondition["field"])
        if current is None:
            return {"action": "block", "message": f"Contract {contract['contract_id']}: precondition {precondition['tool']}.{precondition['field']} is unknown"}
        if not _compare(current, precondition["op"], precondition["value"]):
            return {"action": "block", "message": f"Contract {contract['contract_id']}: precondition {precondition['tool']}.{precondition['field']} {precondition['op']} {precondition['value']}, current {current}"}
    return {"action": "allow"}


def _compare(current: Any, op: str, expected: Any) -> bool:
    if op == ">=":
        return current >= expected
    if op == "==":
        return current == expected
    return False
