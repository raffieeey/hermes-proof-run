"""Guided runs: procedure templates, run state, and step verdicts.

A saved procedure turns one demonstrated action sequence into a template with
named inputs. A run binds new values, tells a follower the next step, and checks
each action call at the tool boundary.

This module is pure Python over the contract dictionaries: no host imports, no
file access, and no model or network calls. The plugin passes storage in.
"""

from __future__ import annotations

import copy
import json
import re
import time

from .core import (
    READ_ONLY_TOOLS,
    ContractError,
    _canonical,
    _compare,
    _display_fact,
    _is_error_result,
    _parse_result,
    fact_key,
)
from . import bindings as live_bindings_mod

MAX_BLOCKS = 5
FACT_MAX_AGE = 300.0
PROCEDURE_SCHEMA = 1

_INPUT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
_INT_TEXT = re.compile(r"[+-]?[0-9]+")
_FLOAT_TEXT = re.compile(r"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?")
_MARKER = "$proof"


class GuidedError(Exception):
    """A procedure, an input list, or a run request cannot be used."""


# --------------------------------------------------------------------------
# input parsing


def parse_inputs(tokens) -> list[tuple[str, str]]:
    """Split ``name=value`` tokens; values keep their shell-quoted text."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for token in tokens:
        name, sep, value = str(token).partition("=")
        if not sep:
            raise GuidedError(f"argument {token!r} is not name=value")
        name = name.strip()
        if not _INPUT_NAME.fullmatch(name):
            raise GuidedError(
                f"invalid input name {name!r}: start with a letter, then letters, "
                "digits or underscores (max 32)"
            )
        if name in seen:
            raise GuidedError(f"input {name!r} was given twice")
        seen.add(name)
        pairs.append((name, value))
    return pairs


# --------------------------------------------------------------------------
# templates


def _number_text(value) -> str | None:
    if type(value) is int:
        return str(value)
    if type(value) is float:
        return json.dumps(value)
    return None


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _numbers(value):
    if type(value) is bool:
        return
    if type(value) in (int, float):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _numbers(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _numbers(item)


def _reject_reserved(value) -> None:
    if isinstance(value, dict):
        if _MARKER in value:
            raise GuidedError(
                f"a recorded argument already contains the {_MARKER} key; rename it and record again"
            )
        for item in value.values():
            _reject_reserved(item)
    elif isinstance(value, list):
        for item in value:
            _reject_reserved(item)


def _appears(value, demo: str, kind: str) -> bool:
    if kind in ("int", "float"):
        return any(_number_text(number) == demo for number in _numbers(value))
    return any(demo in text for text in _strings(value))


def _walk(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, path + (index,))
    else:
        yield list(path), value


def _slot_tables(pairs, kinds):
    strings = [(demo, name) for name, demo in pairs if demo]
    numbers = {
        demo: (name, kinds[name])
        for name, demo in pairs
        if kinds[name] in ("int", "float")
    }
    mapping: dict[str, str] = {}
    for demo, name in sorted(strings, key=lambda item: len(item[0]), reverse=True):
        mapping.setdefault(demo, name)
    pattern = None
    if mapping:
        ordered = sorted(mapping, key=len, reverse=True)
        pattern = re.compile("|".join(re.escape(demo) for demo in ordered))
    return pattern, mapping, numbers


def _template(value, slots):
    """Replace demonstrated values with input slots; text stays literal."""
    pattern, mapping, numbers = slots
    if isinstance(value, str):
        if pattern is None:
            return value
        parts: list = []
        position = 0
        matched = False
        for match in pattern.finditer(value):
            matched = True
            if match.start() > position:
                parts.append(value[position:match.start()])
            parts.append({_MARKER: "input", "name": mapping[match.group(0)]})
            position = match.end()
        if not matched:
            return value
        if position < len(value):
            parts.append(value[position:])
        return {_MARKER: "concat", "parts": parts}
    if isinstance(value, list):
        return [_template(item, slots) for item in value]
    if isinstance(value, dict):
        return {key: _template(item, slots) for key, item in value.items()}
    if type(value) is bool:
        return value
    if type(value) in (int, float):
        hit = numbers.get(_number_text(value) or "")
        if hit is not None:
            return {_MARKER: "input", "name": hit[0], "as": hit[1]}
        return value
    return value


def render(template, values):
    """Render a stored template with bound input values, keeping JSON types."""
    if isinstance(template, dict):
        marker = template.get(_MARKER)
        if marker == "live_result":
            out = copy.deepcopy(template)
            out["args"] = render(template.get("args") or {}, values)
            return out
        if marker == "input":
            return copy.deepcopy(values[template["name"]])
        if marker == "concat":
            out: list = []
            has_live = False
            for part in template.get("parts") or []:
                rendered = render(part, values) if isinstance(part, dict) else str(part)
                if isinstance(rendered, dict) and rendered.get("$proof"):
                    has_live = True
                    out.append(rendered)
                else:
                    out.append(rendered if isinstance(rendered, str) else _json(rendered))
            if has_live:
                return {"$proof": "concat", "parts": out}
            return "".join(out)
        return {key: render(item, values) for key, item in template.items()}
    if isinstance(template, list):
        return [render(item, values) for item in template]
    return template


def display(template):
    """A readable copy of a template: input slots become ``{{name}}``."""
    if isinstance(template, dict):
        marker = template.get(_MARKER)
        if marker == "live_result":
            return "{{fresh " + str(template.get("tool")) + "." + str(template.get("field")) + "}}"
        if marker == "input":
            return "{{" + str(template["name"]) + "}}"
        if marker == "concat":
            out = []
            for part in template.get("parts") or []:
                out.append(display(part) if isinstance(part, dict) else str(part))
            return "".join(out)
        return {key: display(item) for key, item in template.items()}
    if isinstance(template, list):
        return [display(item) for item in template]
    return template


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _show(value) -> str:
    if isinstance(value, str):
        return value
    return _json(display(value))


# --------------------------------------------------------------------------
# saving


def build_procedure(snapshot: dict, pairs) -> dict:
    """Turn a recorder snapshot into a procedure block; validates first."""
    steps = snapshot.get("steps") or []
    if not steps:
        raise GuidedError("the recording has no successful action step")
    if snapshot.get("rejected"):
        raise GuidedError(
            "the recording contains a failed or unsafe call; record the task again"
        )
    for step in steps:
        _reject_reserved(step.get("args") or {})

    # A read argument can be a task-specific identifier. Requiring an explicit
    # binding prevents silently replaying the teacher's request ID.
    read_values = []
    for step in steps:
        for pred in step.get("preconditions") or []:
            if pred.get("op") == "sha256==":
                continue
            for path, value in _walk(pred.get("args_match") or {}):
                if isinstance(value, (str, int, float, bool)) and value is not None:
                    read_values.append((path, value, pred.get("tool"), path[-1] if path else ""))
    step_templates = [live_bindings_mod.bind_step(step) for step in steps]

    specs: list[tuple[str, str]] = []
    for name, value in pairs:
        if not value:
            raise GuidedError(f"input {name!r} has an empty value")
        specs.append((name, value))
    declared = {value for _, value in specs}
    for path, value, tool, field in read_values:
        appears_in_action = any(
            value == candidate
            for step in steps
            for _, candidate in _walk(step.get("args") or {})
        )
        if appears_in_action and value not in declared:
            raise GuidedError(
                f"read argument {tool}.{field} value {value!r} is not declared as an input; "
                "add an input name=value or record a safer procedure"
            )

    kinds: dict[str, str] = {}
    for name, value in specs:
        seen = set()
        for step in steps:
            for number in _numbers(step.get("args") or {}):
                if _number_text(number) == value:
                    seen.add("int" if type(number) is int else "float")
        kinds[name] = "float" if seen == {"float"} else ("int" if seen else "str")

    owners: dict[str, str] = {}
    for name, value in specs:
        if value in owners:
            raise GuidedError(
                f"the demonstrated value {value!r} is claimed by more than one input "
                f"({owners[value]!r} and {name!r}); record distinct values or use one input"
            )
        owners[value] = name
    for name, value in specs:
        if not any(_appears(step.get("args") or {}, value, kinds[name]) for step in steps):
            raise GuidedError(
                f"input {name!r}: the value {value!r} appears in no recorded action argument"
            )

    slots = _slot_tables(specs, kinds)
    procedure_steps = []
    for step_index, step in enumerate(steps):
        reads: list[dict] = []
        groups: dict[str, dict] = {}
        for predicate in step.get("preconditions") or []:
            tool = predicate["tool"]
            args = predicate.get("args_match") or {}
            key = tool + "\0" + _canonical(args)
            group = groups.get(key)
            if group is None:
                group = {
                    "tool": tool,
                    "args": _template(args, slots),
                    "facts": [],
                }
                groups[key] = group
                reads.append(group)
            value = predicate.get("value")
            if predicate.get("op") != "sha256==":
                value = _template(value, slots)
            group["facts"].append(
                {
                    "field": predicate["field"],
                    "op": predicate["op"],
                    "value": value,
                }
            )
        args_template, used = step_templates[step_index]
        procedure_steps.append(
            {
                "tool": step["tool"],
                "args": _template(args_template, slots),
                "reads": reads,
                "live_bindings": [
                    {
                        "tool": b["tool"],
                        "args": _template(b["args"], slots),
                        "field": b["field"],
                    }
                    for b in used
                ],
            }
        )

    return {
        "schema": PROCEDURE_SCHEMA,
        "inputs": [{"name": name, "demo": value, "as": kinds[name]} for name, value in specs],
        "steps": procedure_steps,
        "pins": [],
    }


def _validate_template_json(value, names: frozenset[str], depth: int = 0) -> None:
    """A damaged template must fail before it can reach a tool boundary."""
    if depth > 32:
        raise GuidedError("the procedure template is too deeply nested")
    if value is None or isinstance(value, (str, bool)):
        return
    if type(value) is int:
        return
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise GuidedError("the procedure contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_template_json(item, names, depth + 1)
        return
    if not isinstance(value, dict):
        raise GuidedError("the procedure contains a non-JSON value")
    marker = value.get(_MARKER)
    if marker is not None:
        if marker == "live_result":
            if not isinstance(value.get("field"), str) or not isinstance(value.get("tool"), str) or not isinstance(value.get("args"), dict):
                raise GuidedError("the procedure live-result slot is invalid")
            return
        if marker == "input":
            if value.get("name") not in names:
                raise GuidedError("the procedure references an unknown input")
            if value.get("as", "str") not in ("str", "int", "float"):
                raise GuidedError("the procedure input slot has an invalid type")
            return
        if marker == "concat":
            if not isinstance(value.get("parts"), list):
                raise GuidedError("the procedure concat slot has no parts list")
            for part in value["parts"]:
                if isinstance(part, dict):
                    _validate_template_json(part, names, depth + 1)
                elif not isinstance(part, str):
                    raise GuidedError("the procedure concat slot has a non-string part")
            return
        raise GuidedError("the procedure has an invalid input slot")
    for key, item in value.items():
        if not isinstance(key, str):
            raise GuidedError("the procedure has a non-string JSON key")
        _validate_template_json(item, names, depth + 1)


def validate_procedure(procedure) -> dict:
    """Fail closed on a damaged or hand-edited procedure before dispatch."""
    if not isinstance(procedure, dict):
        raise GuidedError("the procedure block is not a JSON object")
    inputs = procedure.get("inputs")
    steps = procedure.get("steps")
    if not isinstance(inputs, list) or not isinstance(steps, list) or not steps:
        raise GuidedError("the procedure has an invalid inputs or steps list")
    names: set[str] = set()
    for item in inputs:
        if not isinstance(item, dict) or not _INPUT_NAME.fullmatch(str(item.get("name", ""))):
            raise GuidedError("the procedure contains an invalid input")
        name = item["name"]
        if name in names or not isinstance(item.get("demo"), str):
            raise GuidedError(f"the procedure input {name!r} is duplicated or has no demo value")
        if item.get("as") not in (None, "str", "int", "float"):
            raise GuidedError(f"the procedure input {name!r} has an invalid type")
        names.add(name)
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("tool"), str) or not step["tool"]:
            raise GuidedError("the procedure contains a step without a tool name")
        if step["tool"] in READ_ONLY_TOOLS:
            raise GuidedError(f"the procedure step {step['tool']!r} has invalid action arguments")
        try:
            _validate_template_json(step.get("args"), frozenset(names))
        except GuidedError:
            raise
        except (TypeError, ValueError, RecursionError):
            raise GuidedError(f"the procedure step {step['tool']!r} has invalid action arguments") from None
        if not isinstance(step.get("reads"), list):
            raise GuidedError(f"the procedure step {step['tool']!r} has invalid reads")
        for read in step["reads"]:
            if not isinstance(read, dict) or not isinstance(read.get("tool"), str) or not isinstance(read.get("args"), dict):
                raise GuidedError("the procedure contains an invalid read reference")
            if not isinstance(read.get("facts"), list):
                raise GuidedError("the procedure contains an invalid read-fact list")
            for fact in read["facts"]:
                if not isinstance(fact, dict) or not isinstance(fact.get("field"), str) or fact.get("op") not in ("==", "sha256=="):
                    raise GuidedError("the procedure contains an invalid read fact")
    if not isinstance(procedure.get("pins", []), list):
        raise GuidedError("the procedure pins are not a list")
    return procedure


def procedure_fields(procedure: dict) -> list[str]:
    """Every recorded read fact name, ordered and de-duplicated."""
    names: list[str] = []
    for step in procedure.get("steps") or []:
        for read in step.get("reads") or []:
            for fact in read.get("facts") or []:
                name = f"{read['tool']}.{fact['field']}"
                if name not in names:
                    names.append(name)
    return names


def _pinned_pairs(procedure: dict) -> list[str]:
    return [str(item) for item in procedure.get("pins") or []]


# --------------------------------------------------------------------------
# running


def _coerce(name: str, text: str, kind: str):
    if kind == "str":
        return text
    if kind == "int":
        if not _INT_TEXT.fullmatch(text):
            raise GuidedError(f"input {name!r} must be an int, got {text!r}")
        return int(text)
    if not _FLOAT_TEXT.fullmatch(text):
        raise GuidedError(f"input {name!r} must be a float, got {text!r}")
    value = float(text)
    if value != value or value in (float("inf"), float("-inf")):
        raise GuidedError(f"input {name!r} must be a finite float")
    return value


def compile_plan(procedure: dict, values: dict) -> dict:
    """Bind inputs and render every step; raises GuidedError before anything runs."""
    inputs = procedure.get("inputs") or []
    names = [item["name"] for item in inputs]
    for name in values:
        if name not in names:
            raise GuidedError(
                f"unknown input {name!r}; this procedure takes "
                f"{', '.join(names) or 'no inputs'}"
            )
    for item in inputs:
        if item["name"] not in values:
            kind = item.get("as", "str")
            raise GuidedError(
                f"missing input {item['name']!r}; provide it as "
                f"{item['name']}=<{kind}>"
            )
    bound = {
        item["name"]: _coerce(item["name"], values[item["name"]], item.get("as", "str"))
        for item in inputs
    }

    pins = set(_pinned_pairs(procedure))
    steps = []
    for step in procedure.get("steps") or []:
        args = render(step.get("args") or {}, bound)
        entry = {"tool": step["tool"], "args": args, "reads": [], "pinned": [], "live_bindings": []}
        for binding in step.get("live_bindings") or []:
            entry["live_bindings"].append({
                "tool": binding["tool"],
                "args": render(binding.get("args") or {}, bound),
                "field": binding["field"],
                "arg_path": binding.get("arg_path") or [],
            })
        for read in step.get("reads") or []:
            read_args = render(read.get("args") or {}, bound)
            entry["reads"].append({"tool": read["tool"], "args": read_args})
            for fact in read.get("facts") or []:
                if f"{read['tool']}.{fact['field']}" not in pins:
                    continue
                entry["pinned"].append(
                    {
                        "tool": read["tool"],
                        "field": fact["field"],
                        "op": fact.get("op", "=="),
                        "value": render(fact.get("value"), bound),
                        "args": read_args,
                    }
                )
        steps.append(entry)
    if not steps:
        raise GuidedError("the procedure has no recorded action step")
    return {
        "inputs": bound,
        "steps": steps,
        "tools": sorted({step["tool"] for step in steps}),
    }


def new_run(name: str, version: int, procedure: dict, values: dict) -> dict:
    validate_procedure(procedure)
    return {
        "name": name,
        "version": version,
        "plan": compile_plan(procedure, values),
        "index": 0,
        "state": "running",
        "blocks": 0,
        "pending": None,
        "reads": {},
    }


def step_count(run: dict) -> int:
    return len(run["plan"]["steps"])


def status_text(run: dict) -> str:
    total = step_count(run)
    index = min(run.get("index", 0), total - 1) + 1
    line = f"run {run['name']} step {index} of {total}"
    if run.get("state") not in (None, "running"):
        line += f" ({run['state']})"
    return line


def _blocked(message: str) -> dict:
    return {"action": "block", "message": message}


def _remembered(run: dict, tool: str, args: dict):
    try:
        entry = run["reads"].get(fact_key(tool, args))
    except (ContractError, TypeError, ValueError):
        return None, False
    if not entry:
        return None, False
    if time.monotonic() - entry["observed_at"] > FACT_MAX_AGE:
        return None, False
    return entry["result"], True


def _after_block(run: dict, message: str) -> dict:
    if run.get("blocks", 0) >= MAX_BLOCKS:
        run["state"] = "stopped"
        return _blocked(
            f"{message} The run stopped after {MAX_BLOCKS} blocked attempts: report "
            "the problem and wait, or use /proof cancel to release this procedure's tools."
        )
    return _blocked(message)


def evaluate_call(
    run, *, tool_name: str, args: dict, tool_call_id=None
) -> dict | None:
    """Gate one proposed call for this session's run, or return None."""
    if not run:
        return None
    if run.get("state") == "completed":
        return None
    if tool_name in READ_ONLY_TOOLS:
        return None
    plan = run.get("plan") or {}
    if tool_name not in plan.get("tools", []):
        return None
    total = step_count(run)
    if run.get("state") == "stopped":
        return _blocked(
            f"guided run '{run['name']}' is stopped at step "
            f"{min(run.get('index', 0), total - 1) + 1} of {total}. "
            "Nothing was executed. Report the problem and wait; "
            "/proof cancel releases this procedure's tools."
        )
    index = run.get("index", 0)
    if run.get("pending") is not None:
        return _blocked(
            f"guided run '{run['name']}' is still waiting for step {index + 1} of "
            f"{total} to finish. Do not start another action."
        )
    step = plan["steps"][index]
    head = f"guided run '{run['name']}', step {index + 1} of {total}"
    expected_args, missing = live_bindings_mod.resolve_dynamic(step["args"], run.get("reads") or {})
    if missing:
        run["blocks"] = run.get("blocks", 0) + 1
        return _after_block(
            run,
            f"{head}: fresh read data is missing ({', '.join(sorted(set(missing)))}); "
            "perform the required read before this action.",
        )
    if step["tool"] != tool_name or _canonical(args) != _canonical(expected_args):
        run["blocks"] = run.get("blocks", 0) + 1
        return _after_block(
            run,
            f"{head}: expected {step['tool']} with arguments "
            f"{_json(expected_args)}. Nothing was executed.",
        )
    for pin in step.get("pinned") or []:
        current, fresh = _remembered(run, pin["tool"], pin["args"])
        name = f"{pin['tool']}.{pin['field']}"
        if not fresh:
            run["blocks"] = run.get("blocks", 0) + 1
            return _after_block(
                run,
                f"{head}: pinned fact {name} has no fresh read. Call {pin['tool']} "
                f"with arguments {_json(pin['args'])} and check that "
                f"{pin['field']} is {_display_fact(pin['value'])}.",
            )
        if not isinstance(current, dict) or pin["field"] not in current:
            run["blocks"] = run.get("blocks", 0) + 1
            return _after_block(
                run,
                f"{head}: pinned fact {name} is missing from the last read. "
                f"Call {pin['tool']} with arguments {_json(pin['args'])} first.",
            )
        if not _compare(current[pin["field"]], pin["op"], pin["value"]):
            run["blocks"] = run.get("blocks", 0) + 1
            return _after_block(
                run,
                f"{head}: pinned fact {name} is "
                f"{_display_fact(current[pin['field']])} but the procedure requires "
                f"{_display_fact(pin['value'])}. Fix the cause, or /proof cancel.",
            )
    run["pending"] = {
        "call_id": tool_call_id or None,
        "index": index,
        "tool": tool_name,
        "args": copy.deepcopy(args),
    }
    return None


def note_result(
    run, *, tool_name: str, args: dict, result, metadata: dict, tool_call_id=None
) -> str | None:
    """Record one finished call for this session's run: 'completed'/'advanced'/'failed'."""
    if not run:
        return None
    if run.get("state") == "completed":
        return None
    if tool_name in READ_ONLY_TOOLS:
        try:
            key = fact_key(tool_name, args)
        except (ContractError, TypeError, ValueError):
            return None
        if _is_error_result(result, **metadata):
            run["reads"].pop(key, None)
            return None
        parsed = _parse_result(result)
        if isinstance(parsed, dict):
            run["reads"][key] = {"result": parsed, "observed_at": time.monotonic()}
        return None
    plan = run.get("plan") or {}
    if tool_name not in plan.get("tools", []):
        return None
    pending = run.get("pending")
    if not pending:
        return None
    if pending.get("call_id") is not None and pending["call_id"] != tool_call_id:
        return None
    if pending.get("tool") != tool_name:
        return None
    if _canonical(args) != _canonical(pending.get("args") or {}):
        return None
    run["pending"] = None
    run["reads"].clear()
    if _is_error_result(result, **metadata):
        run["state"] = "stopped"
        return "failed"
    run["index"] = pending["index"] + 1
    run["blocks"] = 0
    if run["index"] >= step_count(run):
        run["state"] = "completed"
        return "completed"
    return "advanced"


# --------------------------------------------------------------------------
# guide text


def guide_text(run: dict) -> str:
    plan = run["plan"]
    total = step_count(run)
    index = min(run.get("index", 0), total - 1)
    step = plan["steps"][index]
    inputs = ", ".join(f"{name}={_show(value)}" for name, value in plan["inputs"].items())
    lines = [
        f"Proof Run guided run '{run['name']}' (contract v{run.get('version')}), "
        f"step {index + 1} of {total}.",
        f"Bound inputs: {inputs or 'none'}.",
    ]
    reads = step.get("reads") or []
    if reads:
        lines.append("Reads recorded before this step (advisory unless listed below):")
        for read in reads:
            lines.append(f"- {read['tool']} with arguments {_json(read['args'])}")
    required = live_bindings_mod.marker_reads(step.get("args") or {})
    if required:
        lines.append("Required before this action: perform these reads and use their fresh values.")
        for item in required:
            lines.append(f"- {item['tool']} with arguments {_json(item['args'])} -> {item['field']}")
    pinned = step.get("pinned") or []
    if pinned:
        lines.append("Pinned facts a fresh read must confirm:")
        for pin in pinned:
            lines.append(
                f"- {pin['tool']}.{pin['field']} must be "
                f"{_display_fact(pin['value'])}: call {pin['tool']} with arguments "
                f"{_json(pin['args'])}"
            )
    resolved, missing = live_bindings_mod.resolve_dynamic(step.get("args") or {}, run.get("reads") or {})
    shown = resolved if not missing else display(step.get("args") or {})
    lines.append(f"Next: call {step['tool']} with arguments {_json(shown)}.")
    later = total - index - 1
    if later:
        names = ", ".join(item["tool"] for item in plan["steps"][index + 1:])
        lines.append(f"Later steps ({later}): {names}.")
    else:
        lines.append("This is the last step.")
    lines.append(
        "Follow these steps exactly. Do not improvise with this procedure's tools "
        f"({', '.join(plan['tools'])}). If a step is blocked or fails, stop and report it."
    )
    if run.get("pending"):
        lines.append(
            f"Step {index + 1} is still running; wait for its result before "
            "proposing another action."
        )
    return "\n".join(lines)


def notice(run: dict) -> tuple[str, bool]:
    """Text for pre_llm_call, and whether the run should stay in state."""
    state = run.get("state")
    total = step_count(run)
    index = min(run.get("index", 0), total - 1) + 1
    if state == "completed":
        return (
            f"Proof Run guided run '{run['name']}' completed: all {total} step(s) "
            "succeeded and this procedure's tools are released in this session.",
            False,
        )
    if state == "stopped":
        return (
            f"Proof Run guided run '{run['name']}' stopped at step {index} of {total}. "
            "A step failed or the run was blocked too many times. Report this and wait "
            "for the operator; /proof cancel releases this procedure's tools.",
            True,
        )
    return guide_text(run), True


# --------------------------------------------------------------------------
# inspect and pinning


def inspect_text(contract: dict) -> str:
    procedure = validate_procedure(contract["procedure"])
    name = contract["contract_id"]
    lines = [
        f"Procedure '{name}' (contract v{contract['version']}, "
        f"bound={bool(contract.get('bound'))})"
    ]
    inputs = procedure.get("inputs") or []
    if inputs:
        lines.append(
            "Inputs: " + ", ".join(f"{item['name']}={item['demo']}" for item in inputs)
        )
    else:
        lines.append("Inputs: none")
    pins = set(_pinned_pairs(procedure))
    steps = procedure.get("steps") or []
    for index, step in enumerate(steps, 1):
        lines.append(
            f"Step {index} of {len(steps)}: {step['tool']} "
            f"{_json(display(step.get('args') or {}))}"
        )
        for read in step.get("reads") or []:
            lines.append(
                f"  read {read['tool']} {_json(display(read.get('args') or {}))}"
            )
            for fact in read.get("facts") or []:
                field = f"{read['tool']}.{fact['field']}"
                marker = " [pinned]" if field in pins else ""
                lines.append(f"    {field} = {_fact_text(fact)}{marker}")
    if inputs:
        example = " ".join(f"{item['name']}=<{item.get('as', 'str')}>" for item in inputs)
        lines.append(f"Run it with: /proof run {name} {example}")
    else:
        lines.append(f"Run it with: /proof run {name}")
    lines.append("Read facts are advisory unless pinned with /proof pin.")
    return "\n".join(lines)


def _fact_text(fact: dict) -> str:
    value = fact.get("value")
    if fact.get("op") == "sha256==":
        return f"{value} (unchanged since recording)"
    return _display_fact(display(value))


def pin_field(contract: dict, target: str) -> str:
    procedure = contract.get("procedure")
    if not isinstance(procedure, dict):
        raise GuidedError(f"{contract.get('contract_id')} is not a guided procedure")
    if not isinstance(target, str) or "." not in target:
        raise GuidedError("use <tool>.<field>, for example read_file.content")
    tool, _, field = target.partition(".")
    if target not in procedure_fields(procedure):
        raise GuidedError(f"no recorded read fact is named {tool}.{field}")
    pins = procedure.setdefault("pins", [])
    if target not in pins:
        pins.append(target)
    return target


def unpin_field(contract: dict, target: str) -> str:
    procedure = contract.get("procedure")
    if not isinstance(procedure, dict):
        raise GuidedError(f"{contract.get('contract_id')} is not a guided procedure")
    pins = procedure.get("pins") or []
    if target not in pins:
        raise GuidedError(f"{target} is not pinned")
    pins.remove(target)
    return target
