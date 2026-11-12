"""Opt-in synthetic ticket adapter: private SQLite target, dual-revision CAS.

This is a teaching/follower example, not a production business integration.
Caller-owned tool registration happens only with explicit opt-in.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TicketError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
  ticket_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  source_revision INTEGER NOT NULL,
  routing_revision INTEGER NOT NULL,
  body TEXT NOT NULL,
  queue TEXT NOT NULL,
  last_routing_operation TEXT,
  document TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
  operation_key TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class TicketStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def put_ticket(self, ticket: dict) -> dict:
        required = (
            "tenant_id",
            "ticket_id",
            "source_revision",
            "routing_revision",
            "body",
            "queue",
        )
        for field in required:
            if field not in ticket:
                raise TicketError("INPUT_INVALID", f"missing {field}")
        doc = copy.deepcopy(ticket)
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO tickets(
                  ticket_id, tenant_id, source_revision, routing_revision,
                  body, queue, last_routing_operation, document
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET
                  tenant_id=excluded.tenant_id,
                  source_revision=excluded.source_revision,
                  routing_revision=excluded.routing_revision,
                  body=excluded.body,
                  queue=excluded.queue,
                  last_routing_operation=excluded.last_routing_operation,
                  document=excluded.document
                """,
                (
                    doc["ticket_id"],
                    doc["tenant_id"],
                    int(doc["source_revision"]),
                    int(doc["routing_revision"]),
                    doc["body"],
                    doc["queue"],
                    doc.get("last_routing_operation"),
                    _canonical(doc),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return doc

    def get(self, ticket_id: str, *, tenant_id: str) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise TicketError("NOT_FOUND", f"ticket {ticket_id} not found")
        if row["tenant_id"] != tenant_id:
            raise TicketError("TARGET_IDENTITY_MISMATCH", "tenant does not match")
        return json.loads(row["document"])

    def route_if_version(self, args: dict, *, tenant_id: str) -> dict:
        if args.get("tenant_id") != tenant_id:
            raise TicketError("TARGET_IDENTITY_MISMATCH", "tenant does not match")
        ticket_id = args.get("ticket_id")
        key = args.get("operation_key")
        queue = args.get("queue")
        if not isinstance(ticket_id, str) or not isinstance(key, str) or not isinstance(queue, str):
            raise TicketError("INPUT_INVALID", "ticket_id, operation_key and queue required")
        expected_source = args.get("expected_source_revision")
        expected_routing = args.get("expected_routing_revision")
        payload = copy.deepcopy(args)
        dumped = _canonical(payload)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM operations WHERE operation_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                if existing["payload"] != dumped:
                    conn.execute("ROLLBACK")
                    raise TicketError(
                        "INTENT_CONFLICT", "same operation key with different payload"
                    )
                conn.execute("COMMIT")
                return {
                    "ok": True,
                    "deduped": True,
                    "queue": json.loads(existing["payload"]).get("queue"),
                    "operation_key": key,
                    "state": existing["state"],
                }
            row = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise TicketError("NOT_FOUND", f"ticket {ticket_id} not found")
            if row["tenant_id"] != tenant_id:
                conn.execute("ROLLBACK")
                raise TicketError("TARGET_IDENTITY_MISMATCH", "tenant does not match")
            if int(row["source_revision"]) != int(expected_source):
                conn.execute("ROLLBACK")
                raise TicketError("STATE_DRIFT", "source revision cas failed")
            if int(row["routing_revision"]) != int(expected_routing):
                conn.execute("ROLLBACK")
                raise TicketError("STATE_DRIFT", "routing revision cas failed")
            new_rev = int(row["routing_revision"]) + 1
            doc = json.loads(row["document"])
            doc["queue"] = queue
            doc["last_routing_operation"] = key
            doc["routing_revision"] = new_rev
            conn.execute(
                """
                UPDATE tickets SET
                  queue = ?, routing_revision = ?, last_routing_operation = ?, document = ?
                WHERE ticket_id = ?
                """,
                (queue, new_rev, key, _canonical(doc), ticket_id),
            )
            conn.execute(
                "INSERT INTO operations(operation_key, payload, state, created_at) VALUES (?, ?, 'committed', ?)",
                (key, dumped, _now()),
            )
            conn.execute("COMMIT")
            return {"ok": True, "queue": queue, "operation_key": key, "state": "committed"}
        except TicketError:
            raise
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def operation_status(self, operation_key: str) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_key = ?", (operation_key,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return {"state": "not_found", "operation_key": operation_key}
        return {
            "state": row["state"],
            "operation_key": operation_key,
            "payload": json.loads(row["payload"]),
        }

    def mark_operation_unknown(self, operation_key: str, payload: dict | None = None) -> None:
        dumped = _canonical(payload or {"operation_key": operation_key})
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO operations(operation_key, payload, state, created_at)
                VALUES (?, ?, 'unknown', ?)
                ON CONFLICT(operation_key) DO UPDATE SET state = 'unknown'
                """,
                (operation_key, dumped, _now()),
            )
            conn.commit()
        finally:
            conn.close()


def example_opt_in_path(root: Path) -> Path:
    return Path(root) / "example-ticket.opt-in"


def enable_example(root: Path) -> None:
    path = example_opt_in_path(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text("opt-in\n", encoding="utf-8")
    os.chmod(path, 0o600)


def example_opted_in(root: Path) -> bool:
    path = example_opt_in_path(root)
    return path.is_file() and not path.is_symlink()


def register_example_ticket_tools(ctx, store: TicketStore, *, opt_in: bool, tenant_id: str):
    if not opt_in:
        return False
    if not hasattr(ctx, "register_tool"):
        return False

    def _get(args, **kwargs):
        ticket_id = (args or {}).get("ticket_id")
        return json.dumps(store.get(ticket_id, tenant_id=tenant_id))

    def _route(args, **kwargs):
        return json.dumps(store.route_if_version(args or {}, tenant_id=tenant_id))

    def _status(args, **kwargs):
        key = (args or {}).get("operation_key")
        return json.dumps(store.operation_status(key))

    for name, handler, description in (
        ("example_ticket_get", _get, "Synthetic ticket get; private profile data"),
        (
            "example_ticket_route_if_version",
            _route,
            "Synthetic ticket CAS route; private profile data",
        ),
        (
            "example_ticket_operation_status",
            _status,
            "Synthetic ticket operation status; private profile data",
        ),
    ):
        ctx.register_tool(
            name=name,
            toolset="example-ticket",
            handler=handler,
            description=description,
            schema={
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": {"ticket_id": {"type": "string"}}},
            },
        )
    return True
