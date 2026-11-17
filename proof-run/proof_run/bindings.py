"""Live-result bindings for guided procedures.

A read result value can be substituted into a later action without guessing it.
The substitution is stored as structure, and a run resolves it only from a
successful matching read in that run.
"""
from __future__ import annotations

import copy
import re
import time

from .core import READ_ONLY_TOOLS, fact_key

_DYNAMIC = "live_result"
MIN_STRING = 4
# A read is fresh for the same window the gate documents for remembered reads.
MAX_READ_AGE = 300.0
_VOLATILE = re.compile(
    r"(?:token|version|revision|(?:^|_)id$|(?:^|_)key$|nonce|cursor|etag|checksum|digest|hash|sequence|serial)",
    re.IGNORECASE,
)


class BindingError(ValueError):
    """A recorded value cannot be bound to exactly one live read field."""


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
    """Scalar read-result values recorded before this action, latest read wins.

    Candidates stay separate per (tool, field): two fields can legitimately hold
    the same value in one demonstration (a request quantity and an available
    count), and collapsing them by value silently binds the wrong field.
    """
    seen: dict[tuple, dict] = {}
    order = 0
    for predicate in step.get("preconditions") or []:
        if predicate.get("tool") not in READ_ONLY_TOOLS or predicate.get("op") == "sha256==":
            continue
        value = predicate.get("value")
        if isinstance(value, (dict, list)) or value is None:
            continue
        if isinstance(value, str) and len(value) < MIN_STRING:
            continue
        key = (predicate["tool"], predicate["field"], type(value).__name__, repr(value))
        order += 1
        seen[key] = {
            "tool": predicate["tool"],
            "args": copy.deepcopy(predicate.get("args_match") or {}),
            "field": predicate["field"],
            "recorded": copy.deepcopy(value),
            "order": order,
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


def _unique(candidates: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for candidate in candidates:
        key = (
            candidate["tool"],
            candidate["field"],
            type(candidate["recorded"]).__name__,
            repr(candidate["recorded"]),
        )
        seen[key] = candidate
    return list(seen.values())


def _prefer_field(candidates: list[dict], key: str | None) -> list[dict]:
    """Keep candidates whose field matches the argument key, when any do."""
    if key is None:
        return candidates
    exact = [c for c in candidates if c.get("field") == key]
    return exact or candidates


def _bind_value(value, candidates: list[dict], used: list[dict], key: str | None = None):
    if isinstance(value, str):
        strings = [
            c for c in candidates
            if isinstance(c["recorded"], str) and c["recorded"] in value and _eligible(c, key)
        ]
        if not strings:
            return value
        # One candidate per recorded value; a field matching the argument key wins
        # over a coincident value from another read.
        by_value: dict[str, dict] = {}
        for candidate in strings:
            recorded = candidate["recorded"]
            previous = by_value.get(recorded)
            if previous is None or (
                key is not None and candidate["field"] == key and previous["field"] != key
            ):
                by_value[recorded] = candidate
        chosen = sorted(by_value.values(), key=lambda c: len(c["recorded"]), reverse=True)
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
    if isinstance(value, bool):
        matches = [
            c for c in candidates
            if type(c["recorded"]) is bool and c["recorded"] is value and _eligible(c, key)
        ]
        return _bind_scalar(value, matches, used, key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        matches = [
            c for c in candidates
            if type(c["recorded"]) in (int, float)
            and not isinstance(c["recorded"], bool)
            and c["recorded"] == value
            and _eligible(c, key)
        ]
        return _bind_scalar(value, matches, used, key)
    return value


def _bind_scalar(value, matches: list[dict], used: list[dict], key: str | None):
    """Bind one scalar to exactly one read field, or refuse an ambiguous map."""
    if not matches:
        return value
    unique = _unique(_prefer_field(matches, key))
    if len(unique) == 1:
        used.append(unique[0])
        return _slot(unique[0])
    fields = ", ".join(sorted({f"{c['tool']}.{c['field']}" for c in unique}))
    raise BindingError(
        f"the value {value!r} could come from more than one read field ({fields}); "
        "record distinct values or review the procedure"
    )



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
    observed = read.get("observed_at")
    if not isinstance(observed, (int, float)) or time.monotonic() - observed > MAX_READ_AGE:
        return None, False, f"the {tool} read is older than {MAX_READ_AGE:g} seconds"
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
