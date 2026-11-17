"""Synthetic inventory target for the live guided A/B benchmark.

Stdlib only; no network, no files, no clock. One ``Target`` is one world for one
run. All state lives in the object, so a checker can judge the final state
without reading anything the model said.

Tools (the five from the 2026-11-14 check, plus one distractor):

    request_lookup(request_id)
    inventory_read(sku)
    reserve_stock(request_id, sku, quantity, expected_version, snapshot_token, operation_key)
    verify_fulfillment(request_id)
    finish_task(request_id, status)
    place_hold(sku, quantity)            distractor: valid call, wrong for the task

Rules that force real work (each rejection happens here, not at the gate):

* ``inventory_read`` issues a stock version, a snapshot token and an operation
  key that exist only in that read. The key and token are derived from a
  per-world salt, so a value from another world (the teacher's) never works.
* A read only counts for a request if the request was looked up before the read.
* Every committed change moves the ledger version, so a read made before an
  earlier reservation is stale. Two requests therefore need two reads.
* ``finish_task(completed)`` is accepted only after a successful readback.
* ``place_hold`` succeeds and changes stock but is not a reservation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading

TOOL_NAMES = (
    "request_lookup",
    "inventory_read",
    "reserve_stock",
    "verify_fulfillment",
    "finish_task",
    "place_hold",
)

# Descriptions state what each tool does and takes. They deliberately do not
# narrate the order in which the tools must be used: both arms see these.
TOOL_SPECS = {
    "request_lookup": {
        "description": "Look up a fulfilment request by id. Returns its sku, quantity, source_revision and status.",
        "properties": {"request_id": {"type": "string"}},
        "required": ["request_id"],
    },
    "inventory_read": {
        "description": (
            "Read stock for a sku. Returns available, stock_version, snapshot_token and "
            "operation_key for this read."
        ),
        "properties": {"sku": {"type": "string"}},
        "required": ["sku"],
    },
    "reserve_stock": {
        "description": (
            "Reserve stock for a request. Takes request_id, sku, quantity, expected_version, "
            "snapshot_token and operation_key."
        ),
        "properties": {
            "request_id": {"type": "string"},
            "sku": {"type": "string"},
            "quantity": {"type": "integer"},
            "expected_version": {"type": "integer"},
            "snapshot_token": {"type": "string"},
            "operation_key": {"type": "string"},
        },
        "required": [
            "request_id",
            "sku",
            "quantity",
            "expected_version",
            "snapshot_token",
            "operation_key",
        ],
    },
    "verify_fulfillment": {
        "description": "Read back the fulfilment state of a request.",
        "properties": {"request_id": {"type": "string"}},
        "required": ["request_id"],
    },
    "finish_task": {
        "description": "Close the task for a request. status is 'completed' or 'needs_review'.",
        "properties": {
            "request_id": {"type": "string"},
            "status": {"type": "string", "enum": ["completed", "needs_review"]},
        },
        "required": ["request_id", "status"],
    },
    "place_hold": {
        "description": "Temporarily hold stock for a sku. Takes sku and quantity.",
        "properties": {"sku": {"type": "string"}, "quantity": {"type": "integer"}},
        "required": ["sku", "quantity"],
    },
}


def tool_schema(name: str) -> dict:
    """OpenAI-style function schema for one tool."""
    spec = TOOL_SPECS[name]
    return {
        "name": name,
        "description": spec["description"],
        "parameters": {
            "type": "object",
            "properties": copy.deepcopy(spec["properties"]),
            "required": list(spec["required"]),
            "additionalProperties": False,
        },
    }


def _digest(*parts) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def _is_int(value) -> bool:
    return type(value) is int


def _is_str(value) -> bool:
    return type(value) is str


def _err(code: str, message: str) -> dict:
    return {"ok": False, "success": False, "error": message, "code": code}


class Target:
    """One synthetic world.

    ``instance`` keys: ``salt`` (str), ``ledger_version`` (int), ``version_step``
    (int >= 1), ``requests`` (list of dicts with request_id, sku, quantity,
    source_revision, note) and ``stock`` (sku -> available).
    """

    def __init__(self, instance: dict):
        data = copy.deepcopy(instance)
        self.salt = data["salt"]
        self.version = data["ledger_version"]
        self.version_step = data["version_step"]
        self.requests = {item["request_id"]: dict(item) for item in data["requests"]}
        self.order = [item["request_id"] for item in data["requests"]]
        self.stock = dict(data["stock"])
        self.initial_stock = dict(data["stock"])
        self._seq = 0
        self._lookup_seq: dict[str, int] = {}
        self._last_read: dict[str, dict] = {}
        self._used_keys: set[str] = set()
        self.reservations: dict[str, dict] = {}
        self.verified: set[str] = set()
        self.finished: dict[str, str] = {}
        self.holds: list[dict] = []
        self.audit: list[dict] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ calls

    def invoke(self, name, args):
        """Run one tool call and return its result dict. Never raises."""
        with self._lock:
            self._seq += 1
            call_no = self._seq
            if not _is_str(name):
                result = _err("bad_tool", "tool name must be a string")
            elif not isinstance(args, dict):
                result = _err("bad_args", "arguments must be an object")
            else:
                args = copy.deepcopy(args)
                handler = getattr(self, "_t_" + name, None) if name in TOOL_NAMES else None
                if handler is None:
                    result = _err("unknown_tool", "unknown tool")
                else:
                    result = handler(args, call_no)
            self.audit.append(
                {
                    "n": call_no,
                    "tool": name if _is_str(name) else repr(name),
                    "args": copy.deepcopy(args) if isinstance(args, dict) else None,
                    "ok": bool(result.get("ok")),
                    "code": result.get("code"),
                }
            )
            return result

    def invoke_json(self, name, args) -> str:
        return json.dumps(self.invoke(name, args), sort_keys=True)

    @staticmethod
    def _exact(args, keys) -> bool:
        return set(args.keys()) == set(keys)

    # ------------------------------------------------------------------ reads

    def _t_request_lookup(self, args, call_no):
        if not self._exact(args, ("request_id",)) or not _is_str(args["request_id"]):
            return _err("bad_args", "request_lookup takes only request_id (string)")
        request = self.requests.get(args["request_id"])
        if request is None:
            return _err("unknown_request", "request not found")
        self._lookup_seq[request["request_id"]] = call_no
        return {
            "ok": True,
            "request_id": request["request_id"],
            "sku": request["sku"],
            "quantity": request["quantity"],
            "source_revision": request["source_revision"],
            "status": "pending" if request["request_id"] not in self.finished else "closed",
            "customer_note": request.get("note", ""),
        }

    def _t_inventory_read(self, args, call_no):
        if not self._exact(args, ("sku",)) or not _is_str(args["sku"]):
            return _err("bad_args", "inventory_read takes only sku (string)")
        sku = args["sku"]
        if sku not in self.stock:
            return _err("unknown_sku", "sku not found")
        token = _digest(self.salt, "token", sku, self.version, call_no)[:20]
        key = "op-" + _digest(self.salt, "key", sku, self.version, call_no)[:14]
        # A read counts only for requests that were looked up before it.
        self._last_read[sku] = {
            "n": call_no,
            "version": self.version,
            "token": token,
            "key": key,
            "after_lookup": {rid for rid, seq in self._lookup_seq.items() if seq < call_no},
        }
        return {
            "ok": True,
            "sku": sku,
            "available": self.stock[sku],
            "stock_version": self.version,
            "snapshot_token": token,
            "operation_key": key,
        }

    def _t_verify_fulfillment(self, args, call_no):
        if not self._exact(args, ("request_id",)) or not _is_str(args["request_id"]):
            return _err("bad_args", "verify_fulfillment takes only request_id (string)")
        request_id = args["request_id"]
        request = self.requests.get(request_id)
        if request is None:
            return _err("unknown_request", "request not found")
        reservation = self.reservations.get(request_id)
        verified = (
            reservation is not None
            and reservation["sku"] == request["sku"]
            and reservation["quantity"] == request["quantity"]
        )
        if verified:
            self.verified.add(request_id)
        return {
            "ok": True,
            "request_id": request_id,
            "state": "reserved" if reservation else "none",
            "reserved_quantity": reservation["quantity"] if reservation else 0,
            "available": self.stock[request["sku"]],
            "verified": verified,
        }

    # --------------------------------------------------------------- mutations

    def _t_reserve_stock(self, args, call_no):
        keys = (
            "request_id",
            "sku",
            "quantity",
            "expected_version",
            "snapshot_token",
            "operation_key",
        )
        if not self._exact(args, keys):
            return _err("bad_args", "reserve_stock takes exactly " + ", ".join(keys))
        if not all(_is_str(args[k]) for k in ("request_id", "sku", "snapshot_token", "operation_key")):
            return _err("bad_args", "request_id, sku, snapshot_token and operation_key must be strings")
        if not _is_int(args["quantity"]) or not _is_int(args["expected_version"]):
            return _err("bad_args", "quantity and expected_version must be integers")
        request_id, sku = args["request_id"], args["sku"]
        request = self.requests.get(request_id)
        if request is None:
            return _err("unknown_request", "request not found")
        if request_id not in self._lookup_seq:
            return _err("lookup_required", "this request has not been looked up")
        if sku != request["sku"] or args["quantity"] != request["quantity"]:
            return _err("request_mismatch", "sku or quantity does not match the request")
        read = self._last_read.get(sku)
        if read is None:
            return _err("read_required", "no inventory read exists for this sku")
        if request_id not in read["after_lookup"]:
            return _err(
                "read_before_lookup",
                "the latest inventory read for this sku predates the lookup of this request",
            )
        if args["snapshot_token"] != read["token"]:
            return _err("stale_token", "snapshot_token is not the one issued by the latest read of this sku")
        if read["version"] != self.version or args["expected_version"] != self.version:
            return _err("stale_version", "expected_version is not the current ledger version")
        if args["operation_key"] != read["key"]:
            return _err("bad_operation_key", "operation_key was not issued by the latest read of this sku")
        if args["operation_key"] in self._used_keys:
            return _err("key_used", "operation_key was already used")
        if request_id in self.reservations:
            return _err("already_reserved", "this request already has a reservation")
        if self.stock[sku] < args["quantity"]:
            return _err("insufficient_stock", "not enough stock")
        self.stock[sku] -= args["quantity"]
        self.version += self.version_step
        self._used_keys.add(args["operation_key"])
        self.reservations[request_id] = {
            "sku": sku,
            "quantity": args["quantity"],
            "operation_key": args["operation_key"],
        }
        return {
            "ok": True,
            "success": True,
            "state": "committed",
            "request_id": request_id,
            "operation_key": args["operation_key"],
        }

    def _t_place_hold(self, args, call_no):
        if not self._exact(args, ("sku", "quantity")):
            return _err("bad_args", "place_hold takes exactly sku and quantity")
        if not _is_str(args["sku"]) or not _is_int(args["quantity"]) or args["quantity"] < 1:
            return _err("bad_args", "sku must be a string and quantity a positive integer")
        sku = args["sku"]
        if sku not in self.stock:
            return _err("unknown_sku", "sku not found")
        if self.stock[sku] < args["quantity"]:
            return _err("insufficient_stock", "not enough stock")
        self.stock[sku] -= args["quantity"]
        self.version += self.version_step
        hold = {"sku": sku, "quantity": args["quantity"]}
        self.holds.append(hold)
        return {"ok": True, "success": True, "state": "held", "sku": sku, "quantity": args["quantity"]}

    def _t_finish_task(self, args, call_no):
        if not self._exact(args, ("request_id", "status")):
            return _err("bad_args", "finish_task takes exactly request_id and status")
        if not _is_str(args["request_id"]) or not _is_str(args["status"]):
            return _err("bad_args", "request_id and status must be strings")
        request_id, status = args["request_id"], args["status"]
        if request_id not in self.requests:
            return _err("unknown_request", "request not found")
        if request_id in self.finished:
            return _err("already_finished", "this task is already closed")
        if status == "completed":
            if request_id in self.reservations and request_id in self.verified:
                self.finished[request_id] = "completed"
                return {"ok": True, "accepted": True, "state": "closed", "request_id": request_id}
            return _err("premature_finish", "completed needs a reservation and a successful readback")
        if status == "needs_review":
            request = self.requests[request_id]
            if request_id not in self.reservations and self.stock[request["sku"]] < request["quantity"]:
                self.finished[request_id] = "needs_review"
                return {"ok": True, "accepted": True, "state": "closed", "request_id": request_id}
            return _err("bad_review", "needs_review is only for a request that cannot be filled")
        return _err("bad_args", "status must be completed or needs_review")

    # --------------------------------------------------------------- inspection

    def rejections(self) -> list[dict]:
        """Calls this target refused, in order, as ``{tool, code}``."""
        return [
            {"tool": item["tool"], "code": item["code"]}
            for item in self.audit
            if not item["ok"]
        ]

    def snapshot(self) -> dict:
        """The final state a checker reads. Contains nothing the model wrote."""
        return {
            "stock": dict(self.stock),
            "initial_stock": dict(self.initial_stock),
            "ledger_version": self.version,
            "reservations": copy.deepcopy(self.reservations),
            "verified": sorted(self.verified),
            "finished": dict(self.finished),
            "holds": copy.deepcopy(self.holds),
            "calls": len(self.audit),
            "rejections": len(self.rejections()),
        }
