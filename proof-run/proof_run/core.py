"""Proof Run core: recorder, contract persistence, and enforcement gate.

Pure logic only; host adapters live in the plugin entry point.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


# Security: contract names must be safe filesystem identifiers.
_CONTRACT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _validate_contract_name(name: str) -> str:
    """Reject contract names that could traverse paths or inject shell characters."""
    if not _CONTRACT_NAME_RE.match(name):
        raise ContractError(f"invalid contract name '{name}': use letters, digits, hyphens, underscores (max 64 chars)")
    return name


# Security: patterns that look like secrets, redacted before persisting.
_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ"),  # JWT prefix
    re.compile(r"\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S{8,}", re.IGNORECASE),
]


def _redact_secrets(value: Any) -> Any:
    """Recursively redact secret-looking strings from a value before persisting."""
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub("[REDACTED]", value)
        return value
    if isinstance(value, dict):
        return {k: _redact_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(v) for v in value]
    return value


class ContractError(Exception):
    pass


@dataclass
class _RecordedCall:
    tool: str
    args: dict[str, Any]
    result: Any


# Tools that are safe to run without gating (reads, searches, listings).
# Note: "terminal" is NOT here because terminal commands can be mutating.
READ_ONLY_TOOLS = {
    "read_file", "list_dir", "search_files", "web_search", "web_extract",
    "vision_analyze", "skill_view", "session_search", "memory",
    "stock_level", "git_status", "git_log", "git_diff",
}


def _is_read_only(tool: str) -> bool:
    return tool in READ_ONLY_TOOLS


def _parse_result(result: Any) -> Any:
    """Parse string results that contain JSON into dicts for extraction."""
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return result


def _is_error_result(result: Any) -> bool:
    """True only when the tool call itself failed."""
    parsed = _parse_result(result)
    if isinstance(parsed, dict):
        return (parsed.get("success") is False
                or parsed.get("status") in ("tool_error", "error")
                or ("error" in parsed and parsed.get("status") == "error"))
    if isinstance(parsed, str):
        return "traceback" in parsed.lower() or "exception" in parsed.lower()
    return False


class Recorder:
    """Capture one run and draft a reviewable contract. Persistence is atomic."""

    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()
        self._active: Optional[dict[str, Any]] = None

    def start(self, contract_id: str, intent: str) -> None:
        _validate_contract_name(contract_id)
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

            # B3/F2: derive preconditions from ALL read tools, parsing JSON strings.
            preconditions = []
            for call in reads:
                parsed = _parse_result(call.result)
                if isinstance(parsed, dict):
                    for key, value in parsed.items():
                        if isinstance(value, (int, float, str, bool)):
                            preconditions.append({
                                "kind": "tool_fact", "tool": call.tool,
                                "args_match": dict(call.args), "field": key,
                                "op": ">=", "value": value,
                            })

            success_evidence = []
            for call in mutating:
                parsed = _parse_result(call.result)
                if isinstance(parsed, dict):
                    for key, value in parsed.items():
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
        _validate_contract_name(contract["contract_id"])
        target = self.root / "contracts" / f"{contract['contract_id']}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            prior = json.loads(target.read_text(encoding="utf-8"))
            contract["version"] = int(prior.get("version", 0)) + 1
            contract["supersedes"] = prior.get("version")
        # Security: redact secrets before writing to disk.
        contract = _redact_secrets(contract)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(contract, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        # Security: restrict file permissions to owner only.
        os.chmod(tmp, 0o600)
        tmp.replace(target)
        os.chmod(target, 0o600)


def _arg_patterns(arg_sets: list[dict[str, Any]]) -> dict[str, Any]:
    """F5: preserve all observed values — lists for multiple, scalar for single."""
    all_values: dict[str, list] = {}
    for args in arg_sets:
        for key, value in args.items():
            all_values.setdefault(key, []).append(value)
    patterns: dict[str, Any] = {}
    for key, values in all_values.items():
        numeric = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
        non_numeric = [v for v in values if not (isinstance(v, (int, float)) and not isinstance(v, bool))]
        if numeric and not non_numeric:
            if len(set(numeric)) == 1:
                patterns[key] = numeric[0]
            else:
                patterns[key] = {"min": min(numeric), "max": max(numeric)}
        elif len(set(non_numeric)) == 1:
            patterns[key] = non_numeric[0]
        else:
            # Multiple distinct non-numeric values: allow any of them.
            patterns[key] = {"any": non_numeric}
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

    # B4/F5: check that the proposed args match the recorded arg patterns.
    patterns = contract["action_window"].get("allowed_arg_patterns", {}).get(tool, {})
    for key, pattern in patterns.items():
        actual = args.get(key)
        if actual is None:
            return {"action": "block", "message": f"Contract {contract['contract_id']}: argument {key} is missing"}
        if isinstance(pattern, dict) and "any" in pattern:
            if actual not in pattern["any"]:
                return {"action": "block", "message": f"Contract {contract['contract_id']}: argument {key}={actual} not in recorded values {pattern['any']}"}
        elif isinstance(pattern, dict) and "min" in pattern:
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
        # F6: only use >= for comparable types (numbers), not strings.
        if isinstance(current, (int, float)) and isinstance(expected, (int, float)):
            return current >= expected
        return current == expected
    if op == "==":
        return current == expected
    return False
