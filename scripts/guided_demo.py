#!/usr/bin/env python3
"""Print a real guided-run transcript from the plugin's public surface.

Stdlib only. Loads the plugin the same way the acceptance harness does: a stub
``plugins.plugin_storage``, a temporary data directory, and a context that
offers only ``register_command`` and ``register_hook``.

Run from the repository root:

    python3 scripts/guided_demo.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "proof-run"
MODULE = "proof_run_demo_plugin"
ROOT: list = [None]


def load_plugin():
    if MODULE in sys.modules:
        return sys.modules[MODULE]
    if "plugins" not in sys.modules:
        package = types.ModuleType("plugins")
        package.__path__ = []
        sys.modules["plugins"] = package
    storage = sys.modules.get("plugins.plugin_storage")
    if storage is None:
        storage = types.ModuleType("plugins.plugin_storage")
        sys.modules["plugins.plugin_storage"] = storage
    storage.plugin_data_dir = lambda _name: ROOT[0]  # type: ignore[attr-defined]
    spec = importlib.util.spec_from_file_location(
        MODULE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE] = module
    spec.loader.exec_module(module)
    return module


class Host:
    """A plugin context with only the standard command and hook surface."""

    def __init__(self, root: Path):
        ROOT[0] = root
        self.command = None
        self.hooks: dict = {}
        self.count = 0
        load_plugin().register(self)

    def register_command(self, name, handler, **kwargs):
        self.command = handler

    def register_hook(self, name, callback, **kwargs):
        self.hooks[name] = callback

    def proof(self, text: str, session: str = "s1") -> str:
        return self.command(text, session_id=session)

    def call(self, tool: str, args: dict, result: dict, session: str = "s1") -> str:
        self.count += 1
        call_id = f"call-{self.count}"
        verdict = self.hooks["pre_tool_call"](
            tool_name=tool, args=args, session_id=session, tool_call_id=call_id
        )
        if isinstance(verdict, dict) and verdict.get("action") == "block":
            return str(verdict.get("message") or "")
        self.hooks["post_tool_call"](
            tool_name=tool,
            args=args,
            result=json.dumps(result),
            session_id=session,
            tool_call_id=call_id,
        )
        return ""

    def guide(self, session: str = "s1") -> str:
        out = self.hooks["pre_llm_call"](
            session_id=session,
            task_id="task",
            turn_id="turn",
            user_message="go",
            conversation_history=[],
            is_first_turn=False,
            model="follower",
            platform="cli",
            parent_session_id="",
            sender_id="",
        )
        if isinstance(out, dict):
            return str(out.get("context") or "")
        return out if isinstance(out, str) else ""


CFG = {"path": "/srv/app/config.yaml"}
CFG_TEXT = "replicas: 3\nimage: api:2.1.0\n"
HEALTH = {"service": "api"}


def term(output: str, code: int = 0) -> dict:
    return {"output": output, "exit_code": code, "error": None, "cwd": "/srv/app"}


def file_result(text: str) -> dict:
    return {
        "content": text,
        "total_lines": len(text.splitlines()),
        "file_size": len(text),
        "truncated": False,
    }


def show(label: str, text) -> None:
    print(label)
    print(str(text).rstrip())
    print()


def run_tool(host: Host, label: str, tool: str, args: dict, result: dict, session: str) -> str:
    blocked = host.call(tool, args, result, session)
    if blocked:
        print(f"$ {label}")
        print(f"  BLOCKED: {blocked}")
    else:
        print(f"$ {label}")
        print(f"  -> {json.dumps(result, sort_keys=True)}")
    print()
    return blocked


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        host = Host(Path(temp))
        print("Proof Run guided run transcript")
        print("==============================")
        print()

        show("$ /proof start deploy ship the api", host.proof("start deploy ship the api", "teach"))
        run_tool(host, f'read_file {json.dumps(CFG, sort_keys=True)}', "read_file", CFG, file_result(CFG_TEXT), "teach")
        run_tool(
            host,
            'terminal {"command": "./deploy.sh api 2.1.0"}',
            "terminal",
            {"command": "./deploy.sh api 2.1.0"},
            term("deployed api 2.1.0 ok"),
            "teach",
        )
        show("$ /proof save version=2.1.0", host.proof("save version=2.1.0", "teach"))
        show("$ /proof inspect deploy", host.proof("inspect deploy", "teach"))
        show("$ /proof run deploy version=2.2.0", host.proof("run deploy version=2.2.0", "run"))
        show("$ /proof status (follower session)", host.proof("status", "run"))
        show("--- pre_llm_call context for the follower ---", host.guide("run"))

        run_tool(
            host,
            'terminal {"command": "rm -rf build"}',
            "terminal",
            {"command": "rm -rf build"},
            term(""),
            "run",
        )
        run_tool(
            host,
            'terminal {"command": "./deploy.sh api 2.2.0"}',
            "terminal",
            {"command": "./deploy.sh api 2.2.0"},
            term("deployed api 2.2.0 ok"),
            "run",
        )
        show("--- pre_llm_call context after the last step ---", host.guide("run"))

        print("Pinned fact")
        print("-----------")
        print()
        show("$ /proof start health check the api", host.proof("start health check the api", "teach2"))
        run_tool(
            host,
            f'check_service_health {json.dumps(HEALTH, sort_keys=True)}',
            "check_service_health",
            HEALTH,
            {"status": "healthy", "version": "2.1.0"},
            "teach2",
        )
        run_tool(
            host,
            'terminal {"command": "./deploy.sh api 2.1.0"}',
            "terminal",
            {"command": "./deploy.sh api 2.1.0"},
            term("deployed api 2.1.0 ok"),
            "teach2",
        )
        show("$ /proof save", host.proof("save", "teach2"))
        show(
            "$ /proof pin health check_service_health.status",
            host.proof("pin health check_service_health.status", "teach2"),
        )
        show("$ /proof run health", host.proof("run health", "run2"))
        show("--- pre_llm_call context for the follower ---", host.guide("run2"))
        run_tool(
            host,
            'terminal {"command": "./deploy.sh api 2.1.0"}',
            "terminal",
            {"command": "./deploy.sh api 2.1.0"},
            term("deployed api 2.1.0 ok"),
            "run2",
        )
        run_tool(
            host,
            f'check_service_health {json.dumps(HEALTH, sort_keys=True)}',
            "check_service_health",
            HEALTH,
            {"status": "degraded", "version": "2.1.0"},
            "run2",
        )
        run_tool(
            host,
            'terminal {"command": "./deploy.sh api 2.1.0"}',
            "terminal",
            {"command": "./deploy.sh api 2.1.0"},
            term("deployed api 2.1.0 ok"),
            "run2",
        )
        run_tool(
            host,
            f'check_service_health {json.dumps(HEALTH, sort_keys=True)}',
            "check_service_health",
            HEALTH,
            {"status": "healthy", "version": "2.1.0"},
            "run2",
        )
        run_tool(
            host,
            'terminal {"command": "./deploy.sh api 2.1.0"}',
            "terminal",
            {"command": "./deploy.sh api 2.1.0"},
            term("deployed api 2.1.0 ok"),
            "run2",
        )
        show("--- pre_llm_call context after the last step ---", host.guide("run2"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
