"""A scripted OpenAI-compatible server for testing the driver. Loopback only.

This is a test double, not a model. It lets the driver's wiring (real
``AIAgent`` turns, the plugin's hooks, tool registration, token and call
accounting, result files) be exercised without a provider key. Nothing it
produces is a benchmark result.

Policies, chosen by the request's ``model`` name:

    mock-correct     lookup, read, reserve, verify, finish for each request, in order
    mock-naive       reads stock before the lookup, reserves from that read, gives up on an error
    mock-hold        calls place_hold and finishes; never reserves
    mock-loop        calls request_lookup forever (call-budget exhaustion path)
    mock-error       answers every request with HTTP 500 (provider-error path)
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_REQ = re.compile(r"REQ-\d+")
_SKU = re.compile(r"SKU-[A-Z0-9]+")


def _history(messages: list[dict]) -> list[tuple[str, dict, object]]:
    """``(tool, args, result)`` for each tool call in order; result is parsed JSON or raw text."""
    calls: dict = {}
    out: list[tuple[str, dict, object]] = []
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                calls[call.get("id")] = (fn.get("name"), args)
        elif message.get("role") == "tool":
            name, args = calls.get(message.get("tool_call_id"), (None, {}))
            if name is None:
                continue
            content = message.get("content")
            try:
                result = json.loads(content) if isinstance(content, str) else content
            except ValueError:
                result = content
            out.append((name, args, result))
    return out


def _ids(messages: list[dict]) -> tuple[list[str], list[str]]:
    text = ""
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            text = message["content"]
            break
    rids = list(dict.fromkeys(_REQ.findall(text)))
    skus = list(dict.fromkeys(_SKU.findall(text)))
    return rids, skus


def _ok(result) -> bool:
    return isinstance(result, dict) and bool(result.get("ok"))


def decide(model: str, messages: list[dict]) -> tuple[str | None, dict | None]:
    """Return ``(final_text, None)`` or ``(None, {name, arguments})`` for the next move."""
    rids, skus = _ids(messages)
    history = _history(messages)
    if not rids:
        return "No request found.", None
    if model == "mock-loop":
        return None, {"name": "request_lookup", "arguments": {"request_id": rids[0]}}
    if model == "mock-hold":
        if not history:
            return None, {"name": "place_hold", "arguments": {"sku": skus[0], "quantity": 1}}
        return "Held.", None
    if model == "mock-naive":
        return _naive(rids, skus, history)
    return _correct(rids, skus, history)


def _correct(rids, skus, history):
    # Walk the history against the plan; the first unmet step is next.
    plan: list[tuple[str, str]] = []
    for index, rid in enumerate(rids):
        sku = skus[index] if index < len(skus) else skus[-1]
        plan += [("request_lookup", rid), ("inventory_read", sku), ("reserve_stock", rid), ("verify_fulfillment", rid), ("finish_task", rid)]
    pointer = 0
    last_lookup: dict = {}
    last_read: dict = {}
    for name, args, result in history:
        if pointer < len(plan) and name == plan[pointer][0] and _ok(result):
            if name == "request_lookup":
                last_lookup = result
            elif name == "inventory_read":
                last_read = result
            pointer += 1
    if pointer >= len(plan):
        return "Done.", None
    name, key = plan[pointer]
    if name == "request_lookup":
        return None, {"name": name, "arguments": {"request_id": key}}
    if name == "inventory_read":
        return None, {"name": name, "arguments": {"sku": key}}
    if name == "reserve_stock":
        return None, {
            "name": name,
            "arguments": {
                "request_id": last_lookup.get("request_id", key),
                "sku": last_lookup.get("sku", ""),
                "quantity": last_lookup.get("quantity", 0),
                "expected_version": last_read.get("stock_version", 0),
                "snapshot_token": last_read.get("snapshot_token", ""),
                "operation_key": last_read.get("operation_key", ""),
            },
        }
    if name == "verify_fulfillment":
        return None, {"name": name, "arguments": {"request_id": key}}
    return None, {"name": "finish_task", "arguments": {"request_id": key, "status": "completed"}}


def _naive(rids, skus, history):
    names = [item[0] for item in history]
    if not history:
        return None, {"name": "inventory_read", "arguments": {"sku": skus[0]}}
    if any(not _ok(result) for _n, _a, result in history):
        return "I could not complete the task.", None
    if names == ["inventory_read"]:
        return None, {"name": "request_lookup", "arguments": {"request_id": rids[0]}}
    if names == ["inventory_read", "request_lookup"]:
        read, lookup = history[0][2], history[1][2]
        return None, {
            "name": "reserve_stock",
            "arguments": {
                "request_id": lookup["request_id"],
                "sku": lookup["sku"],
                "quantity": lookup["quantity"],
                "expected_version": read["stock_version"],
                "snapshot_token": read["snapshot_token"],
                "operation_key": read["operation_key"],
            },
        }
    return "Done.", None


def _usage(messages: list[dict], content: str) -> dict:
    prompt = max(1, len(json.dumps(messages)) // 4)
    completion = max(1, len(content) // 4)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


class _Handler(BaseHTTPRequestHandler):
    server_version = "mock-provider/0"

    def log_message(self, format, *args):  # noqa: A002 - silent
        return

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, json.dumps({"object": "list", "data": [{"id": "mock-correct", "object": "model"}]}).encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, b'{"error":{"message":"bad json"}}')
            return
        model = str(body.get("model") or "")
        self.server.requests.append(model)  # type: ignore[attr-defined]
        if model == "mock-error":
            self._send(500, b'{"error":{"message":"mock provider failure","type":"server_error"}}')
            return
        messages = body.get("messages") or []
        text, call = decide(model, messages)
        message: dict = {"role": "assistant", "content": text or ""}
        finish = "stop"
        if call is not None:
            n = self.server.next_call_id()  # type: ignore[attr-defined]
            message["content"] = None
            message["tool_calls"] = [
                {
                    "id": f"call_{n}",
                    "type": "function",
                    "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])},
                }
            ]
            finish = "tool_calls"
        usage = _usage(messages, json.dumps(message))
        if body.get("stream"):
            delta = {"role": "assistant", "content": message["content"]}
            if call is not None:
                delta = {"role": "assistant", "tool_calls": [dict(message["tool_calls"][0], index=0)]}
            chunks = [
                {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                 "choices": [], "usage": usage},
            ]
            payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            self._send(200, payload.encode(), "text/event-stream")
            return
        reply = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage,
        }
        self._send(200, json.dumps(reply).encode())


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests: list[str] = []
        self._n = 0
        self._lock = threading.Lock()

    def next_call_id(self) -> int:
        with self._lock:
            self._n += 1
            return self._n

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"

    def start(self) -> "MockServer":
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
