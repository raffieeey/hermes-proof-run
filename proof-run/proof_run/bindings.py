"""Live-result bindings for guided procedures.

A read result value can be substituted into a later action without guessing it.
The substitution is stored as structure, and a run resolves it only from a
successful matching read in that run.
"""
from __future__ import annotations

import copy
import re

from .core import READ_ONLY_TOOLS, fact_key

_DYNAMIC = "live_result"
MIN_STRING = 4
_VOLATILE = re.compile(
    r"(?:token|version|revision|(?:^|_)id$|(?:^|_)key$|nonce|cursor|etag|checksum|digest|hash|sequence|serial)",
    re.IGNORECASE,
)


def _walk(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, path + (index,))
    else:
        yield list(path), value


def _candidates(step: dict) -> list[dict]:
    """Scalar read-result values recorded before this action, latest read wins."""
    seen: dict[tuple, dict] = {}
    for predicate in step.get("preconditions") or []:
        if predicate.get("tool") not in READ_ONLY_TOOLS or predicate.get("op") == "sha256==":
            continue
        value = predicate.get("value")
        if isinstance(value, (dict, list)) or value is None:
            continue
        if isinstance(value, str) and len(value) < MIN_STRING:
            continue
        key = (type(value).__name__, repr(value))
        seen[key] = {
            "tool": predicate["tool"],
            "args": copy.deepcopy(predicate.get("args_match") or {}),
            "field": predicate["field"],
            "recorded": copy.deepcopy(value),
        }
    return list(seen.values())


def _slot(candidate: dict) -> dict:
    return {
        "$proof": _DYNAMIC,
        "tool": candidate["tool"],
        "args": copy.deepcopy(candidate["args"]),
        "field": candidate["field"],
    }


def _eligible(candidate: dict, key: str | None) -> bool:
    return key == candidate.get("field") or bool(_VOLATILE.search(str(candidate.get("field", ""))))


def _bind_value(value, candidates: list[dict], used: list[dict], key: str | None = None):
    if isinstance(value, str):
        strings = [
            c for c in candidates
            if isinstance(c["recorded"], str) and c["recorded"] in value and _eligible(c, key)
        ]
        if not strings:
            return value
        # Longest first; one candidate per exact recorded value.
        seen = set()
        chosen = []
        for candidate in sorted(strings, key=lambda c: len(c["recorded"]), reverse=True):
            if candidate["recorded"] in seen:
                continue
            seen.add(candidate["recorded"])
            chosen.append(candidate)
        chosen.sort(key=lambda c: value.find(c["recorded"]))
        parts: list = []
        position = 0
        for candidate in chosen:
            start = value.find(candidate["recorded"], position)
            if start < 0:
                continue
            if start > position:
                parts.append(value[position:start])
            parts.append(_slot(candidate))
            used.append(candidate)
            position = start + len(candidate["recorded"])
        if not parts:
            return value
        if position < len(value):
            parts.append(value[position:])
        return {"$proof": "concat", "parts": parts}
    if isinstance(value, list):
        return [_bind_value(item, candidates, used, key) for item in value]
    if isinstance(value, dict):
        return {item_key: _bind_value(item, candidates, used, item_key) for item_key, item in value.items()}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for candidate in candidates:
            recorded = candidate["recorded"]
            if (
                type(recorded) in (int, float) and not isinstance(recorded, bool)
                and recorded == value and _eligible(candidate, key)
            ):
                used.append(candidate)
                return _slot(candidate)
        return value
    if isinstance(value, bool):
        for candidate in candidates:
            if type(candidate["recorded"]) is bool and candidate["recorded"] is value and _eligible(candidate, key):
                used.append(candidate)
                return _slot(candidate)
        return value
    return value


def bind_step(step: dict) -> tuple[dict, list[dict]]:
    """Return an action-args template with live slots, plus the bindings used."""
    used: list[dict] = []
    template = _bind_value(copy.deepcopy(step.get("args") or {}), _candidates(step), used)
    unique = []
    seen = set()
    for candidate in used:
        key = (candidate["tool"], candidate["field"], repr(candidate["recorded"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return template, unique


def _lookup_marker(marker: dict, reads: dict):
    tool, args, field = marker.get("tool"), marker.get("args"), marker.get("field")
    if not isinstance(tool, str) or not isinstance(args, dict) or not isinstance(field, str):
        return None, False, "the live slot is invalid"
    try:
        key = fact_key(tool, args)
    except Exception:
        return None, False, "the live slot has invalid read arguments"
    read = reads.get(key)
    if not isinstance(read, dict):
        return None, False, f"no successful {tool} read with arguments {args!r}"
    result = read.get("result")
    if not isinstance(result, dict) or field not in result:
        return None, False, f"the {tool} read has no field {field!r}"
    value = result[field]
    if isinstance(value, (dict, list)):
        return None, False, f"the live field {tool}.{field} is not a scalar"
    return copy.deepcopy(value), True, ""


def resolve_dynamic(value, reads: dict):
    """Resolve live slots from this run's reads; returns (value, missing list)."""
    missing: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            marker = node.get("$proof")
            if marker == _DYNAMIC:
                resolved, ok, _reason = _lookup_marker(node, reads)
                if not ok:
                    missing.append(f"{node.get('tool')}.{node.get('field')}")
                    return node
                return resolved
            if marker == "concat":
                out = []
                for part in node.get("parts") or []:
                    resolved = walk(part)
                    if isinstance(resolved, dict) and resolved.get("$proof"):
                        out.append(str(resolved))
                    else:
                        out.append(resolved if isinstance(resolved, str) else str(resolved))
                return "".join(out)
            return {key: walk(item) for key, item in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(value), missing


def marker_reads(template, inputs: dict | None = None) -> list[dict]:
    """The read calls a template's live slots depend on, de-duplicated."""
    found: list[dict] = []
    seen = set()

    def walk(node):
        if isinstance(node, dict):
            if node.get("$proof") == _DYNAMIC and isinstance(node.get("tool"), str):
                key = (node.get("tool"), repr(node.get("args")), node.get("field"))
                if key not in seen:
                    seen.add(key)
                    found.append({
                        "tool": node["tool"],
                        "args": copy.deepcopy(node.get("args") or {}),
                        "field": node.get("field"),
                    })
                return
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(template)
    return found
