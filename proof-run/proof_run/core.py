"""Strict observed-step contracts and durable local storage for Proof Run.

Storage uses Linux/POSIX flock and descriptor-relative, no-follow operations.
The host adapter owns session isolation and consumption of read observations.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class ContractError(Exception):
    """A recording, contract, or its storage cannot be used safely."""


_CONTRACT_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ"),
    re.compile(
        r"\b(?:api[_-]?key|token|password|secret|authorization)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}", re.IGNORECASE),
    re.compile(r"\bxox[bp]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN\s+[A-Z\s]*PRIVATE KEY-----"),
]
_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|passphrase|(?:api|access|refresh|auth|id)[_-]?token|"
    r"api[_-]?key|authorization|private[_-]?key|client[_-]?secret|^secret$|^token$)",
    re.IGNORECASE,
)
READ_ONLY_TOOLS = {
    "read_file",
    "list_dir",
    "search_files",
    "web_search",
    "web_extract",
    "vision_analyze",
    "skill_view",
    "session_search",
    "stock_level",
    "git_status",
    "git_log",
    "git_diff",
    "check_service_health",
    "read_config",
    "get_status",
    "list_services",
}


def _validate_contract_name(name: str) -> str:
    if not isinstance(name, str) or not _CONTRACT_NAME_RE.fullmatch(name):
        raise ContractError(
            "invalid contract name: use letters, digits, hyphens, underscores (max 64)"
        )
    return name


def _reject_secrets(value: Any) -> None:
    """Refuse known credential patterns/names; never include their values in errors."""
    if isinstance(value, str):
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise ContractError("recording or contract contains a known secret pattern")
        # Host results commonly arrive as JSON-encoded strings.
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                return
            if isinstance(parsed, (dict, list)):
                _reject_secrets(parsed)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and _SENSITIVE_KEY.search(key):
                raise ContractError(
                    "recording or contract contains a sensitive named field"
                )
            _reject_secrets(key)
            _reject_secrets(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secrets(item)


def _validate_json(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError("JSON object keys must be strings")
            _validate_json(item)
        return
    raise ContractError("value is not finite JSON")


def _canonical(value: Any) -> str:
    _validate_json(value)
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )


def fact_key(tool: str, args: dict) -> str:
    if not isinstance(tool, str) or not tool or not isinstance(args, dict):
        raise ContractError("invalid fact identity")
    return _canonical({"tool": tool, "args": args})


def _parse_result(result: Any) -> Any:
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, (dict, list)):
                return parsed
        except (ValueError, RecursionError):
            pass
    return result


_FAILURE_STATUSES = {
    "error",
    "tool_error",
    "failed",
    "failure",
    "cancelled",
    "canceled",
    "blocked",
    "denied",
    "interrupted",
    "aborted",
    "timeout",
    "timed_out",
}


def _is_error_result(result: Any, **metadata: Any) -> bool:
    if isinstance(result, BaseException):
        return True
    parsed = _parse_result(result)
    for data in (parsed, metadata):
        if not isinstance(data, dict):
            continue
        status = data.get("status")
        if isinstance(status, str) and status.lower().strip() in _FAILURE_STATUSES:
            return True
        if data.get("success") is False or data.get("is_error") is True:
            return True
        if any(
            data.get(key)
            for key in (
                "error",
                "error_type",
                "error_message",
                "cancelled",
                "canceled",
                "blocked",
            )
        ):
            return True
        if "exit_code" in data:
            code = data["exit_code"]
            if type(code) is not int or code != 0:
                return True
    if isinstance(parsed, str):
        return "traceback" in parsed.lower() or "exception" in parsed.lower()
    return False


def _compare(current: Any, op: str, expected: Any) -> bool:
    try:
        if op == "==":
            return _canonical(current) == _canonical(expected)
        if op == ">=":
            return (
                type(current) in (int, float)
                and type(expected) in (int, float)
                and math.isfinite(current)
                and math.isfinite(expected)
                and current >= expected
            )
        if op == "sha256==":
            return isinstance(expected, str) and _digest(current) == expected
    except (ContractError, TypeError, ValueError, OverflowError):
        return False
    return False


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _contains_long_text(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) > 128
    if isinstance(value, list):
        return any(_contains_long_text(x) for x in value)
    if isinstance(value, dict):
        return any(
            _is_blob_field(k) or _contains_long_text(v) for k, v in value.items()
        )
    return False


def _is_blob_field(field: str) -> bool:
    return any(
        part in field.lower()
        for part in ("text", "output", "content", "log", "html", "body")
    )


def _predicate(
    kind: str, tool: str, field: str, value: Any, args: dict | None = None
) -> dict:
    _validate_json(value)
    digest = _is_blob_field(field) or _contains_long_text(value)
    p = {
        "kind": kind,
        "tool": tool,
        "field": field,
        "op": "sha256==" if digest else "==",
        "value": _digest(value) if digest else copy.deepcopy(value),
    }
    if args is not None:
        p["args_match"] = copy.deepcopy(args)
    return p


def _validate_predicate(p: Any, kind: str, tool: str | None = None) -> None:
    if (
        not isinstance(p, dict)
        or not {"kind", "tool", "field", "op", "value"} <= p.keys()
    ):
        raise ContractError("invalid predicate")
    if p["kind"] != kind or not isinstance(p["tool"], str) or not p["tool"]:
        raise ContractError("invalid predicate kind or tool")
    if tool is not None and p["tool"] != tool:
        raise ContractError("evidence tool does not match step")
    if (
        not isinstance(p["field"], str)
        or not p["field"]
        or p["op"] not in ("==", "sha256==")
    ):
        raise ContractError("invalid predicate field or operator")
    if p["op"] == "sha256==" and (
        not isinstance(p["value"], str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", p["value"])
    ):
        raise ContractError("invalid digest")
    _validate_json(p["value"])
    if kind == "tool_fact":
        if p["tool"] not in READ_ONLY_TOOLS or not isinstance(
            p.get("args_match"), dict
        ):
            raise ContractError("invalid fact predicate")
        _validate_json(p["args_match"])


def _validate_v2(contract: Any) -> None:
    required = {
        "schema_version",
        "contract_id",
        "version",
        "bound",
        "action_window",
        "preconditions",
        "success_evidence",
        "provenance",
        "steps",
    }
    if not isinstance(contract, dict) or not required <= contract.keys():
        raise ContractError("missing contract safety fields")
    if type(contract["schema_version"]) is not int or contract["schema_version"] != 2:
        raise ContractError("unsupported contract schema")
    _validate_contract_name(contract["contract_id"])
    if (
        type(contract["version"]) is not int
        or contract["version"] < 1
        or type(contract["bound"]) is not bool
    ):
        raise ContractError("invalid contract version or bound state")
    if contract.get("evidence_status", "proven") not in ("proven", "unproven"):
        raise ContractError("invalid evidence status")
    window = contract["action_window"]
    if not isinstance(window, dict) or not isinstance(
        window.get("mutating_tools"), list
    ):
        raise ContractError("invalid action window")
    tools = window["mutating_tools"]
    if (
        not tools
        or any(not isinstance(t, str) or not t or t in READ_ONLY_TOOLS for t in tools)
        or len(set(tools)) != len(tools)
    ):
        raise ContractError("invalid guarded tools")
    steps = contract["steps"]
    if not isinstance(steps, list) or not steps:
        raise ContractError("missing observed steps")
    all_pre, all_evidence, step_tools = [], [], set()
    for step in steps:
        if (
            not isinstance(step, dict)
            or not {"tool", "args", "preconditions", "success_evidence"} <= step.keys()
        ):
            raise ContractError("invalid observed step")
        tool = step["tool"]
        if (
            not isinstance(tool, str)
            or tool not in tools
            or not isinstance(step["args"], dict)
        ):
            raise ContractError("invalid step tool or arguments")
        _validate_json(step["args"])
        pre, evidence = step["preconditions"], step["success_evidence"]
        if not isinstance(pre, list) or not isinstance(evidence, list) or not evidence:
            raise ContractError("invalid step predicates")
        for pred in pre:
            _validate_predicate(pred, "tool_fact")
        for pred in evidence:
            _validate_predicate(pred, "tool_result_field", tool)
        all_pre.extend(pre)
        all_evidence.extend(evidence)
        step_tools.add(tool)
    if step_tools != set(tools):
        raise ContractError("guarded tools differ from observed steps")
    if not isinstance(contract["preconditions"], list) or _canonical(
        contract["preconditions"]
    ) != _canonical(all_pre):
        raise ContractError("inconsistent preconditions")
    if not isinstance(contract["success_evidence"], list) or _canonical(
        contract["success_evidence"]
    ) != _canonical(all_evidence):
        raise ContractError("inconsistent success evidence")
    provenance = contract["provenance"]
    if (
        not isinstance(provenance, dict)
        or type(provenance.get("recorded_calls")) is not int
        or provenance["recorded_calls"] < len(steps)
    ):
        raise ContractError("invalid provenance")
    _validate_json(contract)
    _reject_secrets(contract)


def _validate_persisted(contract: Any, name: str) -> None:
    if isinstance(contract, dict) and "schema_version" not in contract:
        # Legacy files may be read/replaced but the host must refuse to enforce them.
        if "steps" in contract or "evidence_status" in contract:
            raise ContractError("malformed contract schema")
        required = {"contract_id", "version", "bound"}
        if (
            not required <= contract.keys()
            or type(contract["version"]) is not int
            or contract["version"] < 1
            or type(contract["bound"]) is not bool
        ):
            raise ContractError("malformed legacy contract")
        _validate_json(contract)
        _reject_secrets(contract)
    else:
        _validate_v2(contract)
    if contract["contract_id"] != name:
        raise ContractError("contract filename and identity differ")


def check_evidence(step: dict, result: Any, **metadata: Any) -> bool:
    if _is_error_result(result, **metadata):
        return False
    parsed = _parse_result(result)
    try:
        if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
            return False
        evidence = step.get("success_evidence")
        if (
            not isinstance(parsed, dict)
            or not isinstance(evidence, list)
            or not evidence
        ):
            return False
        for pred in evidence:
            _validate_predicate(pred, "tool_result_field", step["tool"])
            field = pred["field"]
            if field not in parsed or not _compare(
                parsed[field], pred["op"], pred["value"]
            ):
                return False
        return True
    except (ContractError, TypeError, ValueError, KeyError):
        return False


def _check_legacy(contract: dict, runtime: dict, proposed: dict) -> dict | None:
    """Only for manually authored compatibility fixtures; not for host disk loading."""
    tool = proposed["tool"]
    args = proposed.get("args") or {}
    if tool not in contract["action_window"]["mutating_tools"]:
        return None
    for key, pattern in (
        contract["action_window"].get("allowed_arg_patterns", {}).get(tool, {}).items()
    ):
        if key not in args:
            return {"action": "block", "message": f"argument {key} is missing"}
        actual = args[key]
        if isinstance(pattern, dict) and "any" in pattern:
            matched = any(_compare(actual, "==", p) for p in pattern["any"])
        elif isinstance(pattern, dict) and "min" in pattern:
            matched = _compare(actual, ">=", pattern["min"]) and _compare(
                pattern["max"], ">=", actual
            )
        else:
            matched = _compare(actual, "==", pattern)
        if not matched:
            return {"action": "block", "message": f"argument {key} does not match"}
    for pre in contract.get("preconditions") or []:
        observed = runtime.get(pre["tool"])
        if not isinstance(observed, dict) or pre["field"] not in observed:
            return {
                "action": "block",
                "message": f"precondition {pre['tool']}.{pre['field']} is unknown",
            }
        current = observed[pre["field"]]
        if not _compare(current, pre["op"], pre["value"]):
            return {
                "action": "block",
                "message": f"precondition {pre['tool']}.{pre['field']} {pre['op']} {pre['value']}, current {current}",
            }
    return {"action": "allow"}


def _display_fact(value: Any, *, digest: bool = False) -> str:
    if digest:
        return _digest(value)
    try:
        _reject_secrets(value)
    except ContractError:
        return "[sensitive value]"
    text = value if isinstance(value, str) else _canonical(value)
    return text.replace("\n", "\\n")[:160]


def check_contract(
    contract: dict, *, runtime: dict, proposed_call: dict
) -> dict | None:
    """Match a complete observed step and all of that step's fresh facts."""
    try:
        if (
            not isinstance(contract, dict)
            or not isinstance(runtime, dict)
            or not isinstance(proposed_call, dict)
        ):
            raise ContractError("invalid gate input")
        if "schema_version" not in contract:
            if any(key in contract for key in ("steps", "evidence_status", "bound")):
                raise ContractError("persisted or malformed contract needs schema v2")
            return _check_legacy(contract, runtime, proposed_call)
        _validate_v2(contract)
        tool, args = proposed_call["tool"], proposed_call["args"]
        if not isinstance(tool, str) or not isinstance(args, dict):
            raise ContractError("invalid proposed call")
        if tool not in contract["action_window"]["mutating_tools"]:
            return None
        if contract.get("evidence_status") == "unproven":
            return {"action": "block", "message": "contract outcome is unproven"}
        wanted = _canonical(args)
        reason = None
        for index, step in enumerate(contract["steps"]):
            if step["tool"] != tool or _canonical(step["args"]) != wanted:
                continue
            passed = True
            for pred in step["preconditions"]:
                observed = runtime.get(fact_key(pred["tool"], pred["args_match"]))
                fact = f"Contract {contract['contract_id']}: precondition {pred['tool']}.{pred['field']}"
                if not isinstance(observed, dict) or pred["field"] not in observed:
                    reason = {"action": "block", "message": f"{fact} is unknown"}
                    passed = False
                    break
                current = observed[pred["field"]]
                if not _compare(current, pred["op"], pred["value"]):
                    expected = _display_fact(pred["value"])
                    actual = _display_fact(current, digest=pred["op"] == "sha256==")
                    reason = {
                        "action": "block",
                        "message": f"{fact} {pred['op']} {expected}, current {actual}",
                    }
                    passed = False
                    break
            if passed:
                return {"action": "allow", "step_index": index}
        return reason or {
            "action": "block",
            "message": f"Contract {contract['contract_id']}: arguments do not match a demonstrated {tool} call",
        }
    except (
        ContractError,
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ):
        return {"action": "block", "message": "invalid contract or gate input"}


class Recorder:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.Lock()
        self._active: dict | None = None

    def start(self, contract_id: str, intent: str) -> None:
        _validate_contract_name(contract_id)
        if not isinstance(intent, str):
            raise ContractError("invalid intent")
        _reject_secrets(intent)
        with self._lock:
            self._active = {
                "contract_id": contract_id,
                "intent": intent,
                "reads": {},
                "steps": [],
                "count": 0,
                "rejected": False,
            }

    def note_tool(self, tool: str, args: dict, result: Any, **metadata: Any) -> None:
        with self._lock:
            if self._active is None:
                raise ContractError("no active recording")
            active = self._active
            try:
                if not isinstance(tool, str) or not tool or not isinstance(args, dict):
                    raise ContractError("invalid tool call")
                _validate_json(args)
                _reject_secrets(
                    {"tool": tool, "args": args, "result": result, "metadata": metadata}
                )
                active["count"] += 1
                if _is_error_result(result, **metadata):
                    active["rejected"] = True
                    return
                parsed = _parse_result(result)
                if tool in READ_ONLY_TOOLS:
                    predicates = []
                    if isinstance(parsed, dict):
                        for field, value in parsed.items():
                            if not isinstance(field, str) or not field:
                                raise ContractError("invalid result field")
                            predicates.append(
                                _predicate("tool_fact", tool, field, value, args)
                            )
                    active["reads"][fact_key(tool, args)] = predicates
                    return
                if (
                    not isinstance(parsed, dict)
                    or not parsed
                    or not any(
                        value is not None
                        and not (isinstance(value, (str, list, dict)) and not value)
                        for value in parsed.values()
                    )
                ):
                    active["rejected"] = True
                    return
                evidence = []
                for field, value in parsed.items():
                    if not isinstance(field, str) or not field:
                        raise ContractError("invalid result field")
                    evidence.append(_predicate("tool_result_field", tool, field, value))
                active["steps"].append(
                    {
                        "tool": tool,
                        "args": copy.deepcopy(args),
                        "preconditions": [
                            p for group in active["reads"].values() for p in group
                        ],
                        "success_evidence": evidence,
                    }
                )
                active["reads"] = {}
            except (ContractError, TypeError, ValueError, RecursionError):
                active["rejected"] = True
                raise ContractError(
                    "recording contains unsafe or invalid data"
                ) from None

    def stop(self, success: bool = True, bound: bool = False) -> dict:
        with self._lock:
            if self._active is None:
                raise ContractError("no active recording")
            active, self._active = self._active, None
            if success is not True or active["rejected"]:
                raise ContractError("recording contained a failed or unproven call")
            if type(bound) is not bool or not active["steps"]:
                raise ContractError(
                    "recording has no successful mutation or invalid binding"
                )
            steps = active["steps"]
            contract = {
                "schema_version": 2,
                "contract_id": active["contract_id"],
                "version": 1,
                "intent": active["intent"],
                "bound": bound,
                "evidence_status": "proven",
                "action_window": {"mutating_tools": sorted({s["tool"] for s in steps})},
                "preconditions": [p for s in steps for p in s["preconditions"]],
                "success_evidence": [p for s in steps for p in s["success_evidence"]],
                "provenance": {"recorded_calls": active["count"]},
                "steps": steps,
            }
            return save_contract(self.root, contract, new_version=True)


# Descriptor-relative operations keep symlinks out of every component. The
# per-name mutex is needed in addition to flock for threads in one process.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_mutexes: dict[str, threading.RLock] = {}
_mutexes_lock = threading.Lock()


def _mutex(root: Path, name: str) -> threading.RLock:
    key = os.path.abspath(os.fspath(root)) + "\0" + name
    with _mutexes_lock:
        return _mutexes.setdefault(key, threading.RLock())


def _open_dir(parent: int, name: str, create: bool) -> int | None:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        return os.open(name, _DIR_FLAGS, dir_fd=parent)


@contextmanager
def _contracts_fd(root: Path, create: bool):
    fds = []
    try:
        path = os.path.abspath(os.fspath(root))
        fd = os.open(os.path.sep, _DIR_FLAGS)
        fds.append(fd)
        for part in Path(path).parts[1:]:
            fd = _open_dir(fd, part, create)
            if fd is None:
                yield None
                return
            fds.append(fd)
        if create:
            os.fchmod(fd, 0o700)
        fd = _open_dir(fd, "contracts", create)
        if fd is not None:
            fds.append(fd)
            if create:
                os.fchmod(fd, 0o700)
        yield fd
    except OSError as exc:
        raise ContractError(
            f"unsafe or inaccessible contract storage: {exc.strerror or 'I/O failure'}"
        ) from exc
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _check_regular(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ContractError("storage target must be an unlinked-safe regular file")


def _read_bytes(directory: int, filename: str) -> bytes | None:
    try:
        fd = os.open(filename, _FILE_FLAGS, dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        _check_regular(fd)
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_contract(directory: int, name: str) -> tuple[dict | None, bytes | None]:
    data = _read_bytes(directory, name + ".json")
    if data is None:
        return None, None
    try:
        contract = json.loads(data.decode("utf-8"))
        _validate_persisted(contract, name)
    except (UnicodeError, ValueError, TypeError, RecursionError, KeyError) as exc:
        raise ContractError("malformed contract file") from exc
    return contract, data


def _atomic_write(
    directory: int, filename: str, data: bytes, *, rollback: bool = False
) -> None:
    # Inspect an existing target without following it; temp names are exclusive.
    existing = _read_bytes(directory, filename)
    temporary = ".proof-" + secrets.token_hex(16) + ".tmp"
    fd = None
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short contract write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
        try:
            os.fsync(directory)
        except OSError:
            if not rollback:
                try:
                    if existing is None:
                        os.unlink(filename, dir_fd=directory)
                        os.fsync(directory)
                    else:
                        _atomic_write(directory, filename, existing, rollback=True)
                except OSError as rollback_error:
                    raise ContractError(
                        "directory sync failed and rollback could not be confirmed"
                    ) from rollback_error
            raise
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


@contextmanager
def _name_lock(directory: int, name: str):
    fd = os.open(
        "." + name + ".lock",
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        _check_regular(fd)
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def load_contract(root: Path, name: str) -> dict | None:
    _validate_contract_name(name)
    with _contracts_fd(root, False) as directory:
        if directory is None:
            return None
        return _read_contract(directory, name)[0]


def list_contracts(root: Path) -> list[dict]:
    with _contracts_fd(root, False) as directory:
        if directory is None:
            return []
        contracts = []
        for filename in sorted(os.listdir(directory)):
            if not filename.endswith(".json"):
                continue
            name = _validate_contract_name(filename[:-5])
            contract, _ = _read_contract(directory, name)
            if contract is None:
                raise ContractError("contract disappeared during listing")
            contracts.append(contract)
        return contracts


def _save_locked(directory: int, contract: dict, new_version: bool) -> dict:
    value = copy.deepcopy(contract)
    _validate_v2(value)
    name = value["contract_id"]
    prior, old_bytes = _read_contract(directory, name)
    if new_version:
        if prior is not None:
            value["version"] = prior["version"] + 1
            value["supersedes"] = prior["version"]
            _validate_v2(value)
            history = _open_dir(directory, "history", True)
            if history is None or old_bytes is None:
                raise ContractError("cannot open contract history")
            try:
                os.fchmod(history, 0o700)
                old_name = f"{name}-v{prior['version']}.json"
                archived = _read_bytes(history, old_name)
                if archived is not None and archived != old_bytes:
                    raise ContractError("history version already has different content")
                if archived is None:
                    _atomic_write(history, old_name, old_bytes)
            finally:
                os.close(history)
        else:
            value["version"] = 1
            value.pop("supersedes", None)
    elif prior is not None and value["version"] != prior["version"]:
        raise ContractError("contract version changed")
    _validate_v2(value)
    payload = (
        json.dumps(value, sort_keys=True, indent=1, ensure_ascii=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    _atomic_write(directory, name + ".json", payload)
    return value


def save_contract(root: Path, contract: dict, *, new_version: bool = False) -> dict:
    _validate_v2(contract)
    name = contract["contract_id"]
    if type(new_version) is not bool:
        raise ContractError("invalid versioning flag")
    with _mutex(root, name), _contracts_fd(root, True) as directory:
        if directory is None:
            raise ContractError("cannot open contract directory")
        with _name_lock(directory, name):
            return _save_locked(directory, contract, new_version)


def update_contract_state(
    root: Path, name: str, *, expected_version: int | None = None, **updates: Any
) -> dict | None:
    _validate_contract_name(name)
    if set(updates) - {"bound", "evidence_status", "last_check"}:
        raise ContractError("unsupported contract state field")
    if expected_version is not None and (
        type(expected_version) is not int or expected_version < 1
    ):
        raise ContractError("invalid expected version")
    _validate_json(updates)
    _reject_secrets(updates)
    with _mutex(root, name), _contracts_fd(root, False) as directory:
        if directory is None:
            return None
        with _name_lock(directory, name):
            current, _ = _read_contract(directory, name)
            if current is None:
                return None
            _validate_v2(current)
            if expected_version is not None and current["version"] != expected_version:
                return None
            current.update(copy.deepcopy(updates))
            return _save_locked(directory, current, False)
