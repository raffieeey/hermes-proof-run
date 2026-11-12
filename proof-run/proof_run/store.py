"""SQLite-backed workflow store: candidates, runs, receipts, intents, hazards."""

from __future__ import annotations

import copy
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .workflow import (
    WorkflowError,
    canonical_dumps,
    validate_inputs,
    validate_workflow,
    workflow_digest,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflows (
  name TEXT NOT NULL,
  revision INTEGER NOT NULL,
  content_digest TEXT NOT NULL,
  document TEXT NOT NULL,
  unresolved TEXT NOT NULL,
  teacher_outcome_assurance TEXT NOT NULL,
  provenance TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (name, revision)
);
CREATE TABLE IF NOT EXISTS promotions (
  name TEXT PRIMARY KEY,
  revision INTEGER NOT NULL,
  actor TEXT NOT NULL,
  promoted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  revision INTEGER NOT NULL,
  content_digest TEXT NOT NULL,
  inputs TEXT NOT NULL,
  actor TEXT NOT NULL,
  state TEXT NOT NULL,
  cursor INTEGER NOT NULL DEFAULT 0,
  fence INTEGER NOT NULL DEFAULT 0,
  worker_id TEXT,
  lease_until REAL,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  node_outputs TEXT NOT NULL DEFAULT '{}',
  dispatch_started INTEGER NOT NULL DEFAULT 0,
  dispatch_node TEXT,
  error_code TEXT,
  result TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
  call_id TEXT NOT NULL,
  phase TEXT NOT NULL,
  run_id TEXT,
  session_id TEXT,
  trace_id TEXT,
  document TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (call_id, phase)
);
CREATE TABLE IF NOT EXISTS effect_intents (
  operation_key TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  run_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  resource_key TEXT NOT NULL,
  expectations TEXT NOT NULL,
  decision_provenance TEXT,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hazards (
  resource_key TEXT PRIMARY KEY,
  operation_key TEXT NOT NULL,
  run_id TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
"""

_TERMINAL = {
    "completed",
    "verified",
    "blocked",
    "failed",
    "needs_review",
    "unknown_outcome",
    "canceled",
}
_ACTOR_KEYS = ("id", "profile_id", "session_id")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(text: str | None, default: Any = None) -> Any:
    if text is None:
        return default
    return json.loads(text)


def _trusted_actor(actor: Any) -> dict:
    if not isinstance(actor, dict):
        raise WorkflowError("WORKFLOW_INVALID", "actor must be an object")
    trusted = copy.deepcopy(actor)
    for key in _ACTOR_KEYS:
        value = trusted.get(key)
        if not isinstance(value, str) or not value.strip():
            raise WorkflowError("WORKFLOW_INVALID", f"actor.{key} required")
        trusted[key] = value
    return trusted


def _successful_terminal_receipt(document: Any) -> bool:
    if not isinstance(document, dict):
        return False
    if document.get("schema") != "ExecutionReceipt.v1":
        return False
    if document.get("phase") != "terminal":
        return False
    if document.get("dispatched") is not True:
        return False
    if document.get("disposition") != "completed":
        return False
    tool = document.get("tool_name")
    if not isinstance(tool, str) or not tool.strip():
        return False
    profile_id = document.get("profile_id")
    session_id = document.get("session_id")
    if profile_id is not None and (not isinstance(profile_id, str) or not profile_id):
        return False
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        return False
    return True


class WorkflowStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._runner = self.root / "runner"
        self._db = self._runner / "runner.sqlite"
        self._ensure()

    def _ensure(self) -> None:
        self._runner.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._runner, 0o700)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', '1')"
            )
            conn.commit()
        finally:
            conn.close()
        os.chmod(self._db, 0o600)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _txn(self, fn):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn)
            conn.execute("COMMIT")
            return result
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def put_candidate(self, name: str, workflow: dict) -> dict:
        wf = validate_workflow(workflow)
        if wf["workflow_id"] != name:
            raise WorkflowError("WORKFLOW_INVALID", "name must match workflow_id")

        def body(conn: sqlite3.Connection) -> dict:
            row = conn.execute(
                "SELECT MAX(revision) AS m FROM workflows WHERE name = ?", (name,)
            ).fetchone()
            revision = int(row["m"] or 0) + 1
            stored = copy.deepcopy(wf)
            stored["revision"] = revision
            stored["status"] = "candidate"
            stored["content_digest"] = workflow_digest(stored)
            conn.execute(
                """
                INSERT INTO workflows(
                  name, revision, content_digest, document, unresolved,
                  teacher_outcome_assurance, provenance, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    revision,
                    stored["content_digest"],
                    canonical_dumps(stored),
                    canonical_dumps(stored.get("unresolved") or []),
                    stored["provenance"]["teacher_outcome_assurance"],
                    canonical_dumps(stored["provenance"]),
                    _now(),
                ),
            )
            return stored

        return self._txn(body)

    def inspect(self, name: str, revision: int | None = None) -> dict:
        def body(conn: sqlite3.Connection) -> dict:
            if revision is None:
                row = conn.execute(
                    "SELECT * FROM workflows WHERE name = ? ORDER BY revision DESC LIMIT 1",
                    (name,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM workflows WHERE name = ? AND revision = ?",
                    (name, revision),
                ).fetchone()
            if row is None:
                raise WorkflowError("WORKFLOW_NOT_FOUND", f"workflow {name!r} not found")
            doc = json.loads(row["document"])
            promo = conn.execute(
                "SELECT revision FROM promotions WHERE name = ?", (name,)
            ).fetchone()
            promoted_revision = int(promo["revision"]) if promo else None
            doc["promoted"] = promoted_revision == int(row["revision"])
            doc["promoted_revision"] = promoted_revision
            if doc["promoted"]:
                doc["status"] = "promoted"
            doc["unresolved"] = json.loads(row["unresolved"])
            return doc

        return self._txn(body)

    def promote(self, name: str, revision: int, actor: dict) -> dict:
        actor = _trusted_actor(actor)

        def body(conn: sqlite3.Connection) -> dict:
            row = conn.execute(
                "SELECT * FROM workflows WHERE name = ? AND revision = ?",
                (name, revision),
            ).fetchone()
            if row is None:
                raise WorkflowError("WORKFLOW_NOT_FOUND", f"workflow {name!r} r{revision} not found")
            doc = json.loads(row["document"])
            unresolved = json.loads(row["unresolved"])
            if unresolved:
                raise WorkflowError("WORKFLOW_NOT_PROMOTED", "unresolved items remain")
            provenance = json.loads(row["provenance"])
            if provenance.get("teacher_outcome_assurance") == "domain_goal_failed":
                raise WorkflowError("TEACHER_GOAL_FAILED", "known-failed teacher blocks promotion")
            refs = provenance.get("source_receipt_refs") or []
            claimed = bool(provenance.get("actual_demonstration_receipt"))
            if claimed and not refs:
                raise WorkflowError(
                    "DEMO_EVIDENCE_INCOMPLETE",
                    "actual_demonstration_receipt requires referenced terminal receipts",
                )
            for ref in refs:
                call_id = ref.get("call_id") if isinstance(ref, dict) else ref
                if not isinstance(call_id, str) or not call_id:
                    raise WorkflowError("DEMO_EVIDENCE_INCOMPLETE", "invalid source receipt ref")
                rec = conn.execute(
                    "SELECT document FROM receipts WHERE call_id = ? AND phase = 'terminal'",
                    (call_id,),
                ).fetchone()
                if rec is None:
                    raise WorkflowError(
                        "DEMO_EVIDENCE_INCOMPLETE",
                        "referenced terminal receipt is missing",
                    )
                document = json.loads(rec["document"])
                if not _successful_terminal_receipt(document):
                    raise WorkflowError(
                        "DEMO_EVIDENCE_INCOMPLETE",
                        "referenced receipt is not a successful demonstration",
                    )
            conn.execute(
                """
                INSERT INTO promotions(name, revision, actor, promoted_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  revision=excluded.revision,
                  actor=excluded.actor,
                  promoted_at=excluded.promoted_at
                """,
                (name, revision, canonical_dumps(actor), _now()),
            )

        self._txn(body)
        return self.inspect(name, revision)

    def create_run(self, name: str, inputs: dict, actor: dict) -> dict:
        actor = _trusted_actor(actor)

        def body(conn: sqlite3.Connection) -> dict:
            promo = conn.execute(
                "SELECT revision FROM promotions WHERE name = ?", (name,)
            ).fetchone()
            if promo is None:
                raise WorkflowError("WORKFLOW_NOT_PROMOTED", f"{name} has no promoted revision")
            revision = int(promo["revision"])
            row = conn.execute(
                "SELECT * FROM workflows WHERE name = ? AND revision = ?",
                (name, revision),
            ).fetchone()
            doc = json.loads(row["document"])
            try:
                validated = validate_inputs(doc, inputs)
            except WorkflowError as exc:
                if exc.code != "INPUT_INVALID":
                    raise WorkflowError("INPUT_INVALID", exc.message) from exc
                raise
            run_id = "R-" + secrets.token_hex(8)
            now = _now()
            conn.execute(
                """
                INSERT INTO runs(
                  run_id, name, revision, content_digest, inputs, actor, state,
                  cursor, fence, cancel_requested, node_outputs, dispatch_started,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'created', 0, 0, 0, '{}', 0, ?, ?)
                """,
                (
                    run_id,
                    name,
                    revision,
                    row["content_digest"],
                    canonical_dumps(validated),
                    canonical_dumps(actor),
                    now,
                    now,
                ),
            )
            return self._run_from_row(
                conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            )

        return self._txn(body)

    def get_run(self, run_id: str) -> dict:
        def body(conn: sqlite3.Connection) -> dict:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise WorkflowError("RUN_NOT_FOUND", f"run {run_id} not found")
            return self._run_from_row(row)

        return self._txn(body)

    def list_runs(self, limit: int = 20) -> list:
        if type(limit) is not int or limit < 0:
            raise WorkflowError("INPUT_INVALID", "invalid list_runs limit")

        def body(conn: sqlite3.Connection) -> list:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._run_from_row(row) for row in rows]

        return self._txn(body)

    def request_cancel(self, run_id: str) -> dict:
        def body(conn: sqlite3.Connection) -> dict:
            row = self._require_run(conn, run_id)
            conn.execute(
                "UPDATE runs SET cancel_requested = 1, updated_at = ? WHERE run_id = ?",
                (_now(), run_id),
            )
            if not int(row["dispatch_started"]) and row["state"] not in _TERMINAL:
                conn.execute(
                    "UPDATE runs SET state = 'canceled', updated_at = ? WHERE run_id = ?",
                    (_now(), run_id),
                )
            return self._run_from_row(
                conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            )

        return self._txn(body)

    def record_receipt(self, receipt: dict, *, trace_id: str = "") -> None:
        if not isinstance(receipt, dict) or receipt.get("schema") != "ExecutionReceipt.v1":
            raise WorkflowError("RECEIPT_INVALID", "schema must be ExecutionReceipt.v1")
        phase = receipt.get("phase")
        call_id = receipt.get("call_id")
        if phase not in {"before_dispatch", "terminal"} or not isinstance(call_id, str) or not call_id:
            raise WorkflowError("RECEIPT_INVALID", "invalid receipt identity")
        document = copy.deepcopy(receipt)

        def body(conn: sqlite3.Connection) -> None:
            existing = conn.execute(
                "SELECT document FROM receipts WHERE call_id = ? AND phase = ?",
                (call_id, phase),
            ).fetchone()
            dumped = canonical_dumps(document)
            if existing is not None:
                if existing["document"] != dumped:
                    raise WorkflowError("RECEIPT_CONFLICT", "conflicting receipt reuse")
                return
            conn.execute(
                """
                INSERT INTO receipts(
                  call_id, phase, run_id, session_id, trace_id, document, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    phase,
                    document.get("run_id") or "",
                    document.get("session_id") or "",
                    trace_id or "",
                    dumped,
                    _now(),
                ),
            )

        self._txn(body)

    def get_receipts(
        self,
        *,
        trace_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> list:
        def body(conn: sqlite3.Connection) -> list:
            sql = "SELECT document FROM receipts WHERE 1=1"
            params: list[Any] = []
            if trace_id is not None:
                sql += " AND trace_id = ?"
                params.append(trace_id)
            if run_id is not None:
                sql += " AND run_id = ?"
                params.append(run_id)
            if session_id is not None:
                sql += " AND session_id = ?"
                params.append(session_id)
            sql += " ORDER BY created_at ASC, call_id ASC, phase ASC"
            return [json.loads(row["document"]) for row in conn.execute(sql, params)]

        return self._txn(body)

    def admit_dispatch(
        self, run_context: dict, tool_name: str, final_args: dict
    ) -> None:
        if not isinstance(run_context, dict) or not isinstance(final_args, dict):
            raise WorkflowError("PAYLOAD_MISMATCH", "invalid admit_dispatch arguments")
        if not isinstance(tool_name, str) or not tool_name:
            raise WorkflowError("CONTEXT_MISMATCH", "invalid tool name")
        run_id = run_context.get("run_id")
        fence = run_context.get("fence")
        intent_ref = run_context.get("intent_ref") or {}
        if not isinstance(intent_ref, dict):
            intent_ref = {}
        operation_key = intent_ref.get("operation_key")
        if not isinstance(run_id, str) or not run_id:
            raise WorkflowError("INTENT_MISSING", "run_context intent_ref required")

        def body(conn: sqlite3.Connection) -> None:
            run = self._require_run(conn, run_id)
            actor = json.loads(run["actor"])
            if run_context.get("profile_id") != actor.get("profile_id"):
                raise WorkflowError("CONTEXT_MISMATCH", "profile_id does not match pinned actor")
            if run_context.get("session_id") != actor.get("session_id"):
                raise WorkflowError("CONTEXT_MISMATCH", "session_id does not match pinned actor")
            if run_context.get("workflow_digest") != run["content_digest"]:
                raise WorkflowError(
                    "CONTEXT_MISMATCH", "workflow digest does not match pinned run"
                )
            if int(run["cancel_requested"]):
                raise WorkflowError("CANCELLED", "run is cancelled")
            if int(run["fence"]) != fence:
                raise WorkflowError("FENCE_STALE", "stale worker fence")
            if run["state"] in _TERMINAL:
                raise WorkflowError("FENCE_STALE", "run is terminal")
            wf_row = conn.execute(
                "SELECT document FROM workflows WHERE name = ? AND revision = ?",
                (run["name"], run["revision"]),
            ).fetchone()
            if wf_row is None:
                raise WorkflowError("WORKFLOW_NOT_FOUND", "pinned workflow missing")
            wf = json.loads(wf_row["document"])
            nodes = [n for n in (wf.get("nodes") or []) if isinstance(n, dict)]
            matching = [n for n in nodes if n.get("tool") == tool_name]
            if not matching:
                raise WorkflowError("CONTEXT_MISMATCH", "tool is not in the pinned workflow")
            effect_nodes = [n for n in matching if n.get("kind") == "effect"]
            read_nodes = [n for n in matching if n.get("kind") == "read"]
            if isinstance(operation_key, str) and operation_key:
                intent = conn.execute(
                    "SELECT * FROM effect_intents WHERE operation_key = ?",
                    (operation_key,),
                ).fetchone()
                if intent is None:
                    raise WorkflowError("INTENT_MISSING", "durable intent is missing")
                if intent["run_id"] != run_id:
                    raise WorkflowError("INTENT_CONFLICT", "intent belongs to another run")
                if intent["payload"] != canonical_dumps(final_args):
                    raise WorkflowError("PAYLOAD_MISMATCH", "final args do not match intent")
                intent_node = next(
                    (n for n in nodes if n.get("id") == intent["node_id"]), None
                )
                if intent_node is None or intent_node.get("tool") != tool_name:
                    raise WorkflowError(
                        "CONTEXT_MISMATCH", "tool does not match pinned intent node"
                    )
                return
            if read_nodes and not effect_nodes:
                return
            raise WorkflowError("INTENT_MISSING", "run_context intent_ref required")

        self._txn(body)

    def admit_status_lookup(
        self, run_context: dict, tool_name: str, final_args: dict
    ) -> None:
        if not isinstance(run_context, dict) or not isinstance(final_args, dict):
            raise WorkflowError("PAYLOAD_MISMATCH", "invalid admit_status_lookup arguments")
        if not isinstance(tool_name, str) or not tool_name:
            raise WorkflowError("CONTEXT_MISMATCH", "invalid tool name")
        run_id = run_context.get("run_id")
        fence = run_context.get("fence")
        intent_ref = run_context.get("intent_ref") or {}
        if not isinstance(intent_ref, dict):
            intent_ref = {}
        operation_key = intent_ref.get("operation_key")
        if not isinstance(run_id, str) or not run_id:
            raise WorkflowError("INTENT_MISSING", "run_context intent_ref required")

        def body(conn: sqlite3.Connection) -> None:
            run = self._require_run(conn, run_id)
            actor = json.loads(run["actor"])
            if run_context.get("profile_id") != actor.get("profile_id"):
                raise WorkflowError("CONTEXT_MISMATCH", "profile_id does not match pinned actor")
            if run_context.get("session_id") != actor.get("session_id"):
                raise WorkflowError("CONTEXT_MISMATCH", "session_id does not match pinned actor")
            if run_context.get("workflow_digest") != run["content_digest"]:
                raise WorkflowError(
                    "CONTEXT_MISMATCH", "workflow digest does not match pinned run"
                )
            if int(run["fence"]) != fence:
                raise WorkflowError("FENCE_STALE", "stale worker fence")
            if run["state"] not in {"unknown_outcome", "running"}:
                raise WorkflowError("FENCE_STALE", "run is terminal")
            wf_row = conn.execute(
                "SELECT document FROM workflows WHERE name = ? AND revision = ?",
                (run["name"], run["revision"]),
            ).fetchone()
            if wf_row is None:
                raise WorkflowError("WORKFLOW_NOT_FOUND", "pinned workflow missing")
            wf = json.loads(wf_row["document"])
            nodes = [n for n in (wf.get("nodes") or []) if isinstance(n, dict)]
            effect_tools = {n.get("tool") for n in nodes if n.get("kind") == "effect"}
            if tool_name in effect_tools:
                raise WorkflowError(
                    "CONTEXT_MISMATCH", "status lookup cannot use an effect tool"
                )
            recovery = wf.get("recovery") if isinstance(wf.get("recovery"), dict) else {}
            expected_tool = recovery.get("tool")
            if isinstance(expected_tool, str) and expected_tool:
                if tool_name != expected_tool:
                    raise WorkflowError(
                        "CONTEXT_MISMATCH", "tool is not the audited recovery tool"
                    )
            elif any(n.get("tool") == tool_name for n in nodes):
                raise WorkflowError(
                    "CONTEXT_MISMATCH", "tool is not a recovery status tool"
                )
            if set(final_args.keys()) != {"operation_key"}:
                raise WorkflowError(
                    "CONTEXT_MISMATCH", "status args are not a read-only operation lookup"
                )
            args_key = final_args.get("operation_key")
            if not isinstance(operation_key, str) or not operation_key:
                raise WorkflowError("INTENT_MISSING", "run_context intent_ref required")
            if not isinstance(args_key, str) or not args_key:
                raise WorkflowError("INTENT_MISSING", "operation_key required")
            if args_key != operation_key:
                raise WorkflowError(
                    "CONTEXT_MISMATCH", "status key does not match original intent"
                )
            intent = conn.execute(
                "SELECT * FROM effect_intents WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if intent is None:
                raise WorkflowError("INTENT_MISSING", "durable intent is missing")
            if intent["run_id"] != run_id:
                raise WorkflowError("INTENT_CONFLICT", "intent belongs to another run")

        self._txn(body)

    def persist_effect_intent(
        self,
        *,
        run_id: str,
        node_id: str,
        operation_key: str,
        resource_key: str,
        payload: dict,
        expectations: dict | None = None,
        decision_provenance: dict | None = None,
        fence: int,
    ) -> dict:
        dumped = canonical_dumps(payload)
        expect = canonical_dumps(expectations or {})
        provenance = (
            canonical_dumps(decision_provenance) if decision_provenance is not None else None
        )

        def body(conn: sqlite3.Connection) -> dict:
            run = self._require_run(conn, run_id)
            if int(run["cancel_requested"]):
                raise WorkflowError("CANCELLED", "run is cancelled")
            if int(run["fence"]) != fence:
                raise WorkflowError("FENCE_STALE", "stale worker fence")
            existing = conn.execute(
                "SELECT * FROM effect_intents WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if existing is not None:
                if existing["payload"] != dumped:
                    raise WorkflowError(
                        "INTENT_CONFLICT", "same intent key with different payload"
                    )
                return self._intent_from_row(existing)
            hazard = conn.execute(
                "SELECT * FROM hazards WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            if hazard is not None and hazard["state"] in {"reserved", "unknown"}:
                if hazard["operation_key"] != operation_key:
                    raise WorkflowError(
                        "HAZARD_CONFLICT", "resource hazard is held by another intent"
                    )
            now = _now()
            conn.execute(
                """
                INSERT INTO effect_intents(
                  operation_key, payload, run_id, node_id, resource_key,
                  expectations, decision_provenance, state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (
                    operation_key,
                    dumped,
                    run_id,
                    node_id,
                    resource_key,
                    expect,
                    provenance,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO hazards(resource_key, operation_key, run_id, state, created_at)
                VALUES (?, ?, ?, 'reserved', ?)
                ON CONFLICT(resource_key) DO UPDATE SET
                  operation_key=excluded.operation_key,
                  run_id=excluded.run_id,
                  state='reserved'
                """,
                (resource_key, operation_key, run_id, now),
            )
            return self._intent_from_row(
                conn.execute(
                    "SELECT * FROM effect_intents WHERE operation_key = ?",
                    (operation_key,),
                ).fetchone()
            )

        return self._txn(body)

    def acquire_lease(self, run_id: str, worker_id: str) -> dict:
        def body(conn: sqlite3.Connection) -> dict:
            run = self._require_run(conn, run_id)
            fence = int(run["fence"]) + 1
            conn.execute(
                """
                UPDATE runs SET fence = ?, worker_id = ?, lease_until = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (fence, worker_id, time.time() + 3600.0, _now(), run_id),
            )
            return {"fence": fence, "worker_id": worker_id, "run_id": run_id}

        return self._txn(body)

    def mark_dispatch_started(self, run_id: str, node_id: str, fence: int) -> None:
        def body(conn: sqlite3.Connection) -> None:
            run = self._require_run(conn, run_id)
            if int(run["fence"]) != fence:
                raise WorkflowError("FENCE_STALE", "stale worker fence")
            if int(run["cancel_requested"]):
                raise WorkflowError("CANCELLED", "run is cancelled")
            conn.execute(
                """
                UPDATE runs SET dispatch_started = 1, dispatch_node = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (node_id, _now(), run_id),
            )
            conn.execute(
                "UPDATE effect_intents SET state = 'dispatched' WHERE run_id = ? AND node_id = ?",
                (run_id, node_id),
            )
            self._append_event(conn, run_id, "dispatch_started", {"node_id": node_id})

        self._txn(body)

    def update_run(self, run_id: str, **fields: Any) -> dict:
        allowed = {
            "state",
            "cursor",
            "node_outputs",
            "error_code",
            "result",
            "dispatch_started",
            "dispatch_node",
            "cancel_requested",
        }

        def body(conn: sqlite3.Connection) -> dict:
            self._require_run(conn, run_id)
            assignments = ["updated_at = ?"]
            params: list[Any] = [_now()]
            for key, value in fields.items():
                if key not in allowed:
                    raise WorkflowError("WORKFLOW_INVALID", f"cannot update {key}")
                if key in {"node_outputs", "result"} and not isinstance(value, str):
                    value = canonical_dumps(value) if value is not None else None
                if key == "cancel_requested":
                    value = 1 if value else 0
                if key == "dispatch_started":
                    value = 1 if value else 0
                assignments.append(f"{key} = ?")
                params.append(value)
            params.append(run_id)
            conn.execute(
                f"UPDATE runs SET {', '.join(assignments)} WHERE run_id = ?",
                params,
            )
            return self._run_from_row(
                conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            )

        return self._txn(body)

    def list_effect_intents(self, run_id: str) -> dict:
        def body(conn: sqlite3.Connection) -> dict:
            rows = conn.execute(
                "SELECT * FROM effect_intents WHERE run_id = ?", (run_id,)
            ).fetchall()
            return {row["node_id"]: self._intent_from_row(row) for row in rows}

        return self._txn(body)

    def get_effect_intent(self, operation_key: str) -> dict | None:
        def body(conn: sqlite3.Connection) -> dict | None:
            row = conn.execute(
                "SELECT * FROM effect_intents WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            return None if row is None else self._intent_from_row(row)

        return self._txn(body)

    def release_hazard(self, resource_key: str, run_id: str) -> None:
        def body(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM hazards WHERE resource_key = ? AND run_id = ?",
                (resource_key, run_id),
            )

        self._txn(body)

    def mark_hazard_unknown(self, resource_key: str, run_id: str) -> None:
        def body(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE hazards SET state = 'unknown'
                WHERE resource_key = ? AND run_id = ?
                """,
                (resource_key, run_id),
            )

        self._txn(body)

    def load_workflow_for_run(self, run: dict) -> dict:
        return self.inspect(run["name"], run["revision"])

    def _require_run(self, conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise WorkflowError("RUN_NOT_FOUND", f"run {run_id} not found")
        return row

    def _append_event(
        self, conn: sqlite3.Connection, run_id: str, kind: str, payload: dict
    ) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        seq = int(row["m"]) + 1
        conn.execute(
            "INSERT INTO events(run_id, seq, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, seq, kind, canonical_dumps(payload), _now()),
        )

    def _intent_from_row(self, row: sqlite3.Row) -> dict:
        return {
            "operation_key": row["operation_key"],
            "payload": json.loads(row["payload"]),
            "run_id": row["run_id"],
            "node_id": row["node_id"],
            "resource_key": row["resource_key"],
            "expectations": json.loads(row["expectations"]),
            "decision_provenance": _loads(row["decision_provenance"]),
            "state": row["state"],
        }

    def _run_from_row(self, row: sqlite3.Row) -> dict:
        result = _loads(row["result"], default={}) or {}
        data = {
            "run_id": row["run_id"],
            "name": row["name"],
            "revision": int(row["revision"]),
            "content_digest": row["content_digest"],
            "inputs": json.loads(row["inputs"]),
            "actor": json.loads(row["actor"]),
            "state": row["state"],
            "cursor": int(row["cursor"]),
            "fence": int(row["fence"]),
            "worker_id": row["worker_id"],
            "cancel_requested": bool(int(row["cancel_requested"])),
            "node_outputs": json.loads(row["node_outputs"] or "{}"),
            "dispatch_started": bool(int(row["dispatch_started"])),
            "dispatch_node": row["dispatch_node"],
            "error_code": row["error_code"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if isinstance(result, dict):
            data.update(result)
            data["state"] = row["state"]
            if row["error_code"]:
                data["error_code"] = row["error_code"]
        return data
