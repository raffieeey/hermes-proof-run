"""Counted file-backed HostPort/Follower fakes for runtime tests.

These stand in for operator-lane hostport.py/follower.py. Handler and
status counts live in files so a crashed subprocess and a reopened
parent share independent receiver tallies. No paid inference.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
from pathlib import Path
from typing import Any

EFFECT_TOOLS = frozenset({"deploy_service", "example_ticket_route_if_version"})
READ_TOOLS = frozenset({"check_service_health", "example_ticket_get"})
STATUS_TOOLS = frozenset({"example_ticket_operation_status", "deploy_operation_status"})


def load_fixture(name: str) -> dict:
    path = Path(__file__).resolve().parent / name
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def new_state_dir(root: Path) -> Path:
    state = Path(root) / "host-state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "counters.json").write_text(
        json.dumps({"read": 0, "effect": 0, "status": 0, "progress": 0}),
        encoding="utf-8",
    )
    (state / "world.json").write_text(
        json.dumps(
            {
                "deploy": {
                    "service": "fixture-api",
                    "status": "healthy",
                    "version": "v0",
                },
                "tickets": {
                    "T-21": {
                        "tenant_id": "tenant-A",
                        "ticket_id": "T-21",
                        "source_revision": 4,
                        "routing_revision": 7,
                        "body": "I was charged twice this month for the same invoice.",
                        "queue": "unassigned",
                        "last_routing_operation": None,
                    }
                },
                "operations": {},
            }
        ),
        encoding="utf-8",
    )
    (state / "context.json").write_text(
        json.dumps(
            {
                "profile_id": "test-profile",
                "session_id": "sess-1",
                "tenant_id": "tenant-A",
                "routing_intent_epoch": "initial",
            }
        ),
        encoding="utf-8",
    )
    (state / "capabilities.json").write_text(
        json.dumps(
            {
                "version": "checked-execution/v1",
                "final_authorization": True,
                "mandatory_receipts": True,
                "bound_context": True,
                "plugin_host_supported": True,
            }
        ),
        encoding="utf-8",
    )
    (state / "host.json").write_text(
        json.dumps(
            {
                "crash": "none",
                "rewrite": None,
                "lie_success": False,
                "call_seq": 0,
            }
        ),
        encoding="utf-8",
    )
    return state


class FileFollower:
    """Controlled follower: reads a JSON decision file. Not inference."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def identity(self) -> dict:
        return {
            "provider": "controlled-test",
            "model": "fake-follower",
            "inference": False,
        }

    def decide(self, packet: dict) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))


class FileHost:
    """External-boundary fake with file-backed counts and target ledger."""

    def __init__(self, state_dir: Path, store):
        self.state_dir = Path(state_dir)
        self.store = store
        self._lock_path = self.state_dir / "lock"
        self._lock_path.touch(exist_ok=True)

    def _locked(self):
        return open(self._lock_path, "a+", encoding="utf-8")

    def _load_json(self, name: str) -> dict:
        return json.loads((self.state_dir / name).read_text(encoding="utf-8"))

    def _save_json(self, name: str, data: dict) -> None:
        path = self.state_dir / name
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)

    def configure(self, **fields: Any) -> None:
        with self._locked() as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            host = self._load_json("host.json")
            host.update(fields)
            self._save_json("host.json", host)

    def set_world(self, **fields: Any) -> None:
        with self._locked() as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            world = self._load_json("world.json")
            world.update(fields)
            self._save_json("world.json", world)

    def set_context(self, **fields: Any) -> None:
        with self._locked() as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            ctx = self._load_json("context.json")
            ctx.update(fields)
            self._save_json("context.json", ctx)

    def set_capabilities(self, **fields: Any) -> None:
        with self._locked() as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            caps = self._load_json("capabilities.json")
            caps.update(fields)
            self._save_json("capabilities.json", caps)

    def counters(self) -> dict:
        with self._locked() as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            return self._load_json("counters.json")

    def world(self) -> dict:
        with self._locked() as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            return self._load_json("world.json")

    def _inc(self, key: str) -> None:
        counters = self._load_json("counters.json")
        counters[key] = int(counters.get(key, 0)) + 1
        self._save_json("counters.json", counters)

    def capabilities(self) -> dict:
        return self._load_json("capabilities.json")

    def authenticated_context(self) -> dict:
        return self._load_json("context.json")

    def progress(self, message: str, *, run_context: dict) -> None:
        with self._locked() as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            self._inc("progress")

    def operation_status(self, intent: dict, *, run_context: dict) -> dict:
        with self._locked() as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            self._inc("status")
            key = intent["operation_key"]
            op = self._load_json("world.json").get("operations", {}).get(key)
            if op is None:
                return {"state": "not_found", "operation_key": key}
            if _canonical(op.get("payload")) != _canonical(intent.get("payload")):
                return {"state": "ambiguous", "operation_key": key}
            return {
                "state": op["state"],
                "operation_key": key,
                "payload": copy.deepcopy(op["payload"]),
            }

    def execute_checked(self, tool_name: str, args: dict, *, run_context: dict) -> dict:
        with self._locked() as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            host = self._load_json("host.json")
            host["call_seq"] = int(host.get("call_seq", 0)) + 1
            call_id = f"call-{host['call_seq']:04d}"
            self._save_json("host.json", host)
            proposed = copy.deepcopy(args)
            final = copy.deepcopy(args)
            rewrite = host.get("rewrite")
            if isinstance(rewrite, dict) and tool_name in EFFECT_TOOLS:
                final.update(copy.deepcopy(rewrite))
            ctx = self._load_json("context.json")
            if tool_name in EFFECT_TOOLS:
                if host.get("crash") == "before_handler":
                    os._exit(92)
                self.store.admit_dispatch(run_context, tool_name, final)
                self._emit_receipt(
                    call_id,
                    "before_dispatch",
                    tool_name,
                    proposed,
                    final,
                    run_context,
                    ctx,
                    dispatched=False,
                    disposition="prepared",
                    result=None,
                )
                if host.get("crash") == "after_admit":
                    os._exit(94)
                self._inc("effect")
                if not host.get("lie_success"):
                    self._apply_effect(tool_name, final)
                if host.get("crash") == "after_handler":
                    os._exit(93)
                result = {"ok": True, "tool": tool_name, "args": final}
                rec = self._emit_receipt(
                    call_id,
                    "terminal",
                    tool_name,
                    proposed,
                    final,
                    run_context,
                    ctx,
                    dispatched=True,
                    disposition="completed",
                    result=json.dumps(result),
                )
                return rec
            if tool_name in READ_TOOLS:
                self._inc("read")
                result = self._read(tool_name, proposed)
                rec = self._emit_receipt(
                    call_id,
                    "terminal",
                    tool_name,
                    proposed,
                    final,
                    run_context,
                    ctx,
                    dispatched=True,
                    disposition="completed",
                    result=json.dumps(result),
                )
                return rec
            raise KeyError(f"unknown tool {tool_name}")

    def _apply_effect(self, tool_name: str, args: dict) -> None:
        world = self._load_json("world.json")
        operations = world.setdefault("operations", {})
        key = args.get("operation_key")
        if tool_name == "deploy_service":
            existing = operations.get(key)
            if existing and _canonical(existing.get("payload")) != _canonical(args):
                raise ValueError("same-key different payload")
            if existing and existing.get("state") == "committed":
                return
            world["deploy"] = {
                "service": args["service"],
                "status": "healthy",
                "version": args["version"],
            }
            operations[key] = {"payload": copy.deepcopy(args), "state": "committed"}
        elif tool_name == "example_ticket_route_if_version":
            ticket = world["tickets"][args["ticket_id"]]
            if ticket["source_revision"] != args["expected_source_revision"]:
                raise ValueError("source revision cas failed")
            if ticket["routing_revision"] != args["expected_routing_revision"]:
                raise ValueError("routing revision cas failed")
            existing = operations.get(key)
            if existing and _canonical(existing.get("payload")) != _canonical(args):
                raise ValueError("same-key different payload")
            if existing and existing.get("state") == "committed":
                return
            ticket["queue"] = args["queue"]
            ticket["last_routing_operation"] = key
            ticket["routing_revision"] = int(ticket["routing_revision"]) + 1
            operations[key] = {"payload": copy.deepcopy(args), "state": "committed"}
        self._save_json("world.json", world)

    def _read(self, tool_name: str, args: dict) -> dict:
        world = self._load_json("world.json")
        if tool_name == "check_service_health":
            deploy = copy.deepcopy(world["deploy"])
            return deploy
        if tool_name == "example_ticket_get":
            return copy.deepcopy(world["tickets"][args["ticket_id"]])
        raise KeyError(tool_name)

    def _emit_receipt(
        self,
        call_id: str,
        phase: str,
        tool_name: str,
        proposed: dict,
        final: dict,
        run_context: dict,
        ctx: dict,
        *,
        dispatched: bool,
        disposition: str,
        result: Any,
    ) -> dict:
        rec = {
            "schema": "ExecutionReceipt.v1",
            "phase": phase,
            "call_id": call_id,
            "profile_id": ctx.get("profile_id"),
            "session_id": ctx.get("session_id"),
            "run_id": run_context.get("run_id", ""),
            "tool_name": tool_name,
            "proposed_args": copy.deepcopy(proposed),
            "final_args": copy.deepcopy(final),
            "dispatched": dispatched,
            "disposition": disposition,
            "result": result,
            "operation_identity": (run_context.get("intent_ref") or {}).get(
                "operation_key", ""
            ),
            "policy_ref": "",
            "receipt_persisted": True,
        }
        self.store.record_receipt(rec, trace_id=run_context.get("run_id", ""))
        rec["receipt_persisted"] = True
        return rec
