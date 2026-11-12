"""Trace-only teaching capture: host receipts, no incidental schema-2 gate.

Credentials are refused before any interested trace or receipt is persisted.
Capture never writes ``contracts/`` and never activates a legacy exact gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import core
from .workflow import WorkflowError

_SESSION_FILE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
_SUCCESS = {"completed"}


class CaptureError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runner(root: Path) -> Path:
    path = Path(root) / "runner"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _session_key(session_id: str) -> str:
    raw = session_id or "default"
    if _SESSION_FILE_RE.fullmatch(raw):
        return raw
    return "s-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _session_path(root: Path, session_id: str) -> Path:
    directory = _runner(root) / "capture-sessions"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / (_session_key(session_id) + ".json")


def _trace_path(root: Path, name: str) -> Path:
    directory = _runner(root) / "traces"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / f"{name}.json"


def _open_dir_fd(path: Path, create: bool) -> tuple[int, list[int]]:
    abs_path = os.path.abspath(os.fspath(path))
    fds: list[int] = []
    fd = os.open(os.path.sep, core._DIR_FLAGS)
    fds.append(fd)
    for part in Path(abs_path).parts[1:]:
        created = False
        try:
            nxt = os.open(part, core._DIR_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            nxt = None
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    created = True
                except FileExistsError:
                    created = False
                nxt = os.open(part, core._DIR_FLAGS, dir_fd=fd)
        if nxt is None:
            for item in reversed(fds):
                os.close(item)
            raise CaptureError("STORAGE_UNSAFE", "capture directory is missing")
        fds.append(nxt)
        fd = nxt
        if created:
            try:
                os.fchmod(fd, 0o700)
            except OSError:
                pass
    return fd, fds


def _write_private(path: Path, data: dict) -> None:
    payload = (json.dumps(data, sort_keys=True, indent=2) + "\n").encode("utf-8")
    directory = Path(path).parent
    filename = Path(path).name
    dir_fd, fds = _open_dir_fd(directory, True)
    try:
        with core._name_lock(dir_fd, filename):
            core._atomic_write(dir_fd, filename, payload, rollback=False)
    except core.ContractError as exc:
        raise CaptureError("STORAGE_UNSAFE", "capture storage write failed") from exc
    finally:
        for item in reversed(fds):
            os.close(item)


def _read_json(path: Path) -> dict | None:
    if not path.is_file() or path.is_symlink():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _refuse_secrets(value: Any) -> None:
    try:
        core._reject_secrets(value)
    except core.ContractError as exc:
        raise CaptureError(
            "SECRET_REFUSED", "recognized credential refused before persistence"
        ) from exc


def start_trace(
    root: Path,
    *,
    name: str,
    session_id: str,
    profile_id: str,
    intent: str = "",
) -> dict:
    try:
        name = core._validate_contract_name(name)
    except core.ContractError as exc:
        raise CaptureError("INPUT_INVALID", str(exc)) from exc
    if not isinstance(session_id, str) or not session_id:
        raise CaptureError("INPUT_INVALID", "session_id required")
    if not isinstance(profile_id, str) or not profile_id:
        raise CaptureError("INPUT_INVALID", "profile_id required")
    _refuse_secrets(name)
    _refuse_secrets(session_id)
    _refuse_secrets(profile_id)
    _refuse_secrets(intent or name)
    existing = active_trace(root, session_id=session_id)
    if existing:
        raise CaptureError(
            "CAPTURE_ACTIVE",
            f"already capturing {existing['name']!r} in this session",
        )
    record = {
        "name": name,
        "capture_id": "C-" + secrets.token_hex(8),
        "session_id": session_id,
        "profile_id": profile_id,
        "intent": intent or name,
        "started_at": _now(),
        "call_ids": [],
    }
    _refuse_secrets(record)
    _write_private(_session_path(root, session_id), record)
    return record


def active_trace(root: Path, *, session_id: str) -> dict | None:
    return _read_json(_session_path(root, session_id))


def cancel_trace(root: Path, *, session_id: str) -> dict:
    path = _session_path(root, session_id)
    if path.is_file() and not path.is_symlink():
        path.unlink()
    return {"cancelled": True, "session_id": session_id}


def load_trace(root: Path, name: str) -> dict | None:
    try:
        name = core._validate_contract_name(name)
    except core.ContractError:
        return None
    return _read_json(_trace_path(root, name))


def interested_scope(
    root: Path,
    *,
    session_id: str,
    profile_id: str,
    run_id: str = "",
    store=None,
) -> bool:
    active = active_trace(root, session_id=session_id)
    if (
        isinstance(active, dict)
        and active.get("session_id") == session_id
        and active.get("profile_id") == profile_id
    ):
        return True
    if run_id and store is not None:
        try:
            run = store.get_run(run_id)
        except (WorkflowError, OSError, KeyError, TypeError, ValueError):
            return False
        actor = run.get("actor") or {}
        if actor.get("session_id") == session_id and actor.get("profile_id") == profile_id:
            return True
        return False
    return False


def ingest_receipt(
    root: Path,
    receipt: dict,
    *,
    session_id: str,
    profile_id: str,
    store=None,
    run_id: str = "",
) -> Any:
    if not isinstance(receipt, dict):
        return None
    rec_session = receipt.get("session_id") or session_id
    rec_profile = receipt.get("profile_id") or profile_id
    rec_run = receipt.get("run_id") or run_id or ""
    if rec_session != session_id or rec_profile != profile_id:
        return None
    if not interested_scope(
        root,
        session_id=session_id,
        profile_id=profile_id,
        run_id=rec_run,
        store=store,
    ):
        return None
    _refuse_secrets(receipt)
    active = active_trace(root, session_id=session_id)
    trace_id = ""
    if active:
        trace_id = active.get("capture_id") or active["name"]
        call_id = receipt.get("call_id")
        if isinstance(call_id, str) and call_id and call_id not in active.get("call_ids", []):
            active = dict(active)
            active["call_ids"] = list(active.get("call_ids") or []) + [call_id]
            _write_private(_session_path(root, session_id), active)
    elif rec_run:
        trace_id = rec_run
    if store is None:
        return True
    store.record_receipt(receipt, trace_id=trace_id)
    return True


def _terminal_ok(receipt: dict) -> bool:
    return (
        receipt.get("phase") == "terminal"
        and bool(receipt.get("dispatched"))
        and receipt.get("disposition") in _SUCCESS
    )


def expected_call_ids(trace: dict) -> list[str]:
    ids: list[str] = []
    for item in list(trace.get("call_ids") or []):
        if isinstance(item, str) and item and item not in ids:
            ids.append(item)
    for rec in trace.get("receipts") or []:
        cid = rec.get("call_id")
        if isinstance(cid, str) and cid and cid not in ids:
            ids.append(cid)
    return ids


def incomplete_call_ids(trace: dict) -> list[str]:
    receipts = list(trace.get("receipts") or [])
    by_id: dict[str, list[dict]] = {}
    for rec in receipts:
        cid = rec.get("call_id")
        if isinstance(cid, str) and cid:
            by_id.setdefault(cid, []).append(rec)
    incomplete = []
    for cid in expected_call_ids(trace):
        recs = by_id.get(cid) or []
        if not any(_terminal_ok(item) for item in recs):
            incomplete.append(cid)
    return incomplete


def trace_is_complete(trace: dict) -> bool:
    if not isinstance(trace, dict):
        return False
    expected = expected_call_ids(trace)
    if not expected:
        return False
    return not incomplete_call_ids(trace)


def finish_trace(root: Path, *, session_id: str, store=None) -> dict:
    active = active_trace(root, session_id=session_id)
    if not active:
        raise CaptureError("CAPTURE_INACTIVE", "no active capture in this session")
    name = active["name"]
    capture_id = active.get("capture_id") or name
    expected = list(active.get("call_ids") or [])
    receipts = []
    if store is not None:
        receipts = store.get_receipts(trace_id=capture_id, session_id=session_id)
        if expected:
            allowed = set(expected)
            receipts = [item for item in receipts if item.get("call_id") in allowed]
        else:
            receipts = []
    trace = {
        "name": name,
        "capture_id": capture_id,
        "intent": active.get("intent") or name,
        "session_id": session_id,
        "profile_id": active.get("profile_id"),
        "started_at": active.get("started_at"),
        "finished_at": _now(),
        "call_ids": expected,
        "receipts": receipts,
        "complete": False,
        "runnable": False,
        "teacher_outcome_assurance": "not_measured",
        "source": "captured-teaching-trace",
        "source_kind": "scripted-or-live-teaching",
    }
    complete = trace_is_complete(trace)
    trace["complete"] = complete
    trace["runnable"] = complete
    trace["teacher_outcome_assurance"] = "effect_observed" if complete else "not_measured"
    _write_private(_trace_path(root, name), trace)
    path = _session_path(root, session_id)
    if path.is_file() and not path.is_symlink():
        path.unlink()
    return trace
