"""Load the repository's Proof Run plugin behind the plain command-and-hook surface.

This does not change the plugin. It gives it the same minimal context that
``scripts/guided_demo.py`` and the acceptance tests use: ``register_command`` and
``register_hook`` only, with its contract store pointed at a directory the caller
owns. The driver then forwards the registered hooks into a real Hermes plugin
manager so a real ``AIAgent`` turn calls them.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "proof-run"
_MODULE = "proof_run_live_guided_bench_plugin"
_ROOT: list = [None]


def _install_storage_stub(root: Path) -> None:
    """Point ``plugins.plugin_storage.plugin_data_dir`` at ``root`` before the plugin imports it."""
    _ROOT[0] = root
    try:
        import plugins.plugin_storage as storage  # a real Hermes checkout provides this
    except ImportError:
        if "plugins" not in sys.modules:
            package = types.ModuleType("plugins")
            package.__path__ = []  # type: ignore[attr-defined]
            sys.modules["plugins"] = package
        storage = sys.modules.get("plugins.plugin_storage")
        if storage is None:
            storage = types.ModuleType("plugins.plugin_storage")
            sys.modules["plugins.plugin_storage"] = storage
    storage.plugin_data_dir = lambda _name: _ROOT[0]  # type: ignore[attr-defined]


def load_plugin(plugin_dir: Path, root: Path):
    """Import the plugin once per process; later calls only re-point the store."""
    _install_storage_stub(root)
    if _MODULE in sys.modules:
        return sys.modules[_MODULE]
    spec = importlib.util.spec_from_file_location(
        _MODULE,
        Path(plugin_dir) / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the plugin from {plugin_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE] = module
    spec.loader.exec_module(module)
    return module


class Host:
    """A plugin context with only ``register_command`` and ``register_hook``."""

    def __init__(self, root: Path, plugin_dir: Path = PLUGIN_DIR):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.command = None
        self.hooks: dict = {}
        self.count = 0
        load_plugin(plugin_dir, root).register(self)

    # The plugin calls these two from register().
    def register_command(self, name, handler, **kwargs):
        self.command = handler

    def register_hook(self, name, callback, **kwargs):
        self.hooks[name] = callback

    def proof(self, text: str, session: str) -> str:
        """Run a ``/proof`` command in ``session``."""
        handler = self.command
        if handler is None:
            raise RuntimeError("the plugin registered no command")
        return handler(text, session_id=session)

    def run_state(self, session: str) -> dict | None:
        """Plain facts about a session's guided run, or None. Read-only diagnostics."""
        run = getattr(self, "_proof_run_state", {}).get("runs", {}).get(session)
        if not isinstance(run, dict):
            return None
        return {
            "state": run.get("state"),
            "index": run.get("index"),
            "steps": len((run.get("plan") or {}).get("steps") or []),
            "blocks": run.get("blocks"),
        }

    # Direct hook calls, used by the offline scripted check (no Hermes needed).
    def call(self, tool: str, args: dict, result: dict | str, session: str):
        """One tool call as a host runs it. Returns ``(blocked_message, None)`` or ``("", result)``."""
        self.count += 1
        call_id = f"call-{self.count}"
        verdict = self.hooks["pre_tool_call"](
            tool_name=tool, args=args, session_id=session, tool_call_id=call_id
        )
        if isinstance(verdict, dict) and verdict.get("action") == "block":
            return str(verdict.get("message") or ""), None
        payload = result() if callable(result) else result
        text = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True)
        self.hooks["post_tool_call"](
            tool_name=tool, args=args, result=text, session_id=session, tool_call_id=call_id
        )
        return "", text

    def guide(self, session: str) -> str:
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
