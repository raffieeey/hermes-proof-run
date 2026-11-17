#!/usr/bin/env python3
"""Guided-versus-unguided live benchmark for Proof Run.

For each task a teacher model demonstrates a procedure once on a real Hermes
``AIAgent.run_conversation`` turn; ``/proof save`` stores it; a cheaper follower
then runs fresh instances of the same task in two arms with the same tools and
the same API-call budget: guided (``/proof run`` + the plugin's guide and gate)
and unguided (no procedure). A deterministic checker judges each run against the
target's final state. Failed runs are recorded and never retried.

Run it with the Python that has Hermes' dependencies, for example:

    HP=~/.hermes/installs/<id>/environments/<id>/venv/bin/python
    $HP benchmarks/live-guided/run_ab.py --provider <provider> --model glm-5.3-flash \\
        --teacher-provider <provider> --teacher-model glm-5.3 \\
        --tasks all --runs-per-arm 2 --out benchmarks/live-guided/results-2026-11-17.json

``--runs-per-arm`` counts runs per task, so the total per arm is tasks x runs.
The driver does not change the plugin: it loads the repository's ``proof-run/``
behind the plain command-and-hook surface and forwards its hooks to the Hermes
plugin manager (user plugins are not discovered: ``HERMES_SAFE_MODE=1``).
Credentials come from the provider setup Hermes already has; none are read from
or written to the results file.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ab_stats  # noqa: E402
import inventory_target as IT  # noqa: E402
import plugin_host  # noqa: E402
import task_set as T  # noqa: E402

ARMS = ("guided", "unguided")
TEACH_ATTEMPTS = 3
SCHEMA = 1
CLAIM_CEILING = (
    "One synthetic inventory target, a fixed task set, and one teacher/follower pair. The interval "
    "describes these runs only. The teacher's note states the order rules and the unguided arm does not "
    "get them, so this measures what a saved procedure transfers, not what an unguided model can do "
    "with the same instructions in its prompt. No productivity, token-savings, cost-savings or "
    "superiority claim."
)

# One run at a time; handlers and hook observers read this.
CURRENT: dict = {"target": None, "log": None, "session": None}


# ------------------------------------------------------------------ utilities


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def scrub(text, extra_secrets: tuple[str, ...] = ()) -> str:
    """Remove anything that looks like a credential or an endpoint from text kept in results."""
    out = str(text)
    for secret in extra_secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "<redacted>")
    out = re.sub(r"https?://\S+", "<url>", out)
    out = re.sub(r"(?i)bearer\s+\S+", "Bearer <redacted>", out)
    out = re.sub(r"\b(?:sk|key|tok)[-_][A-Za-z0-9_\-]{10,}\b", "<redacted>", out)
    return out[:400]


def write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def git_head(path: Path) -> dict:
    try:
        head = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(path), "status", "--porcelain", "--", "proof-run", "benchmarks/live-guided"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        )
        return {"commit": head or None, "dirty": dirty}
    except Exception:
        return {"commit": None, "dirty": None}


# ------------------------------------------------------------------ Hermes


def bootstrap_hermes(src: Path) -> None:
    """Make Hermes importable without user plugins, hooks or lazy installs."""
    os.environ.setdefault("HERMES_SAFE_MODE", "1")
    os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        from hermes_cli.env_loader import load_hermes_dotenv

        load_hermes_dotenv()
    except ImportError as exc:
        raise SystemExit(
            f"cannot import Hermes from {src} ({exc}). Run this with the Python that has Hermes' "
            "dependencies and pass --hermes-src."
        )


def resolve_route(provider: str, model: str, base_url: str | None, api_key_env: str | None) -> dict:
    from hermes_cli.runtime_provider import resolve_runtime_provider

    kwargs: dict = {"requested": provider, "target_model": model}
    if base_url:
        kwargs["explicit_base_url"] = base_url
    if api_key_env:
        kwargs["explicit_api_key"] = os.environ.get(api_key_env, "")
    runtime = resolve_runtime_provider(**kwargs)
    return {
        "base_url": runtime["base_url"],
        "api_key": runtime.get("api_key") or "",
        "provider": runtime.get("provider"),
        "api_mode": runtime.get("api_mode"),
        "model": model,
    }


def preflight(route: dict, label: str) -> tuple[bool, str]:
    """One tiny chat call. Returns (ok, short message with no credential in it)."""
    body = json.dumps(
        {"model": route["model"], "messages": [{"role": "user", "content": "Reply with: ok"}], "max_tokens": 8}
    ).encode()
    request = urllib.request.Request(
        route["base_url"].rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Authorization": "Bearer " + route["api_key"],
            "Content-Type": "application/json",
            "User-Agent": "proof-run-live-guided/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return True, f"{label}: HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        detail = scrub(exc.read().decode("utf-8", "replace"), (route["api_key"],))
        return False, f"{label}: HTTP {exc.code} {detail}"
    except Exception as exc:  # network, DNS, timeout
        return False, f"{label}: {scrub(repr(exc), (route['api_key'],))}"


def register_tools() -> None:
    """Register the synthetic tools and keep Hermes from hiding them behind tool search."""
    import tools.tool_search as tool_search
    from tools.registry import registry

    tool_search.load_config = lambda: tool_search.ToolSearchConfig.from_raw({"enabled": "off"})

    def make_handler(name):
        def handler(args, **_kwargs):
            target = CURRENT["target"]
            if target is None:
                return json.dumps({"ok": False, "error": "no active task", "code": "no_target"})
            result = target.invoke(name, args if isinstance(args, dict) else {})
            run_log = CURRENT["log"]
            if run_log is not None:
                run_log.executed(name, result)
            return json.dumps(result, sort_keys=True)

        return handler

    for name in IT.TOOL_NAMES:
        registry.register(
            name=name,
            toolset="proof_bench",
            schema=IT.tool_schema(name),
            handler=make_handler(name),
            description=IT.TOOL_SPECS[name]["description"],
        )


class RunLog:
    """What the hooks and handlers saw during one run."""

    def __init__(self, host, session: str):
        self.host = host
        self.session = session
        self.trace: list[dict] = []
        self.blocks = 0
        self.guide_turns = 0
        self.guide_first: str | None = None
        self.last_state: dict | None = None

    def observe(self, hook: str, kwargs: dict, out) -> None:
        if kwargs.get("session_id") != self.session:
            return
        if hook == "pre_tool_call":
            blocked = isinstance(out, dict) and out.get("action") == "block"
            entry = {
                "n": len(self.trace) + 1,
                "tool": kwargs.get("tool_name"),
                "args": kwargs.get("args"),
                "gate": "block" if blocked else "allow",
                "result": None,
            }
            if blocked:
                self.blocks += 1
                entry["gate_message"] = scrub(out.get("message", ""))
            self.trace.append(entry)
        elif hook == "pre_llm_call":
            context = out.get("context") if isinstance(out, dict) else (out if isinstance(out, str) else "")
            if context:
                self.guide_turns += 1
                if self.guide_first is None:
                    # Keep the whole guide: the A/B record must show exactly what
                    # the follower was told, not a preview.
                    self.guide_first = str(context)
        state = self.host.run_state(self.session)
        if state is not None:
            self.last_state = state

    def executed(self, name: str, result: dict) -> None:
        for entry in reversed(self.trace):
            if entry["tool"] == name and entry["gate"] == "allow" and entry["result"] is None:
                entry["result"] = "ok" if result.get("ok") else str(result.get("code") or "error")
                return


def install_hooks(host) -> None:
    """Forward the plugin's registered hooks into the real Hermes plugin manager."""
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    manager.discover_and_load()  # HERMES_SAFE_MODE: marks discovered, loads nothing

    def wrap(name, inner):
        def callback(**kwargs):
            out = inner(**kwargs)
            run_log = CURRENT["log"]
            if run_log is not None:
                run_log.observe(name, kwargs, out)
            return out

        return callback

    for name, inner in host.hooks.items():
        manager._hooks.setdefault(name, []).append(wrap(name, inner))


def make_agent(route: dict, session: str, budget: int):
    from run_agent import AIAgent

    agent = AIAgent(
        base_url=route["base_url"],
        api_key=route["api_key"],
        provider=route["provider"],
        api_mode=route["api_mode"],
        model=route["model"],
        max_iterations=budget,
        enabled_toolsets=["proof_bench"],
        quiet_mode=True,
        tool_progress_mode="off",
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=True,
        save_trajectories=False,
        ephemeral_system_prompt=T.SYSTEM_PROMPT,
        session_id=session,
        platform="cli",
    )
    # No silent retries: one attempt per model call, no recovery cycles, and no
    # extra summary call when the budget runs out (the budget is the budget).
    setattr(agent, "_api_max_retries", 1)
    setattr(agent, "_auto_recovery_cycles", 0)
    setattr(agent, "_handle_max_iterations", lambda messages, api_call_count: "")
    return agent


def converse(agent, message: str, secret: str, budget: int) -> dict:
    """Run one conversation; never raises; returns measurements."""
    started = time.monotonic()
    result, error = None, None
    try:
        # Hermes prints tool progress lines to stdout; keep stdout for the JSON summary.
        with contextlib.redirect_stdout(sys.stderr):
            result = agent.run_conversation(message)
    except BaseException as exc:  # keep the benchmark alive; record the failure
        if isinstance(exc, KeyboardInterrupt):
            raise
        error = repr(exc)
    wall = round(time.monotonic() - started, 3)
    result = result if isinstance(result, dict) else {}

    def count(key: str, attribute: str) -> int:
        value = result.get(key)
        if value is None:
            value = getattr(agent, attribute, 0)
        return int(value or 0)

    calls = count("api_calls", "session_api_calls")
    completed = bool(result.get("completed"))
    # The host also sets "error" when a turn simply runs out of budget with a tool result
    # pending (failure_reason "loop_error"). That is the budget, not the provider.
    ran_out = calls >= budget and not completed and result.get("turn_exit_reason") == "pending_tool_result"
    provider_error = None
    if error:
        provider_error = scrub(error, (secret,))
    elif result.get("error") and not ran_out:
        provider_error = scrub(result["error"], (secret,))

    return {
        "wall_s": wall,
        "api_calls": calls,
        "total_tokens": count("total_tokens", "session_total_tokens"),
        "input_tokens": count("input_tokens", "session_input_tokens"),
        "output_tokens": count("output_tokens", "session_output_tokens"),
        "cache_read_tokens": count("cache_read_tokens", "session_cache_read_tokens"),
        "completed": completed,
        "turn_exit_reason": result.get("turn_exit_reason") or result.get("failure_reason"),
        "served_model": scrub(result.get("served_model") or "", (secret,)) or None,
        "provider_error": provider_error,
    }


# ------------------------------------------------------------------ one run


def judge(task, instance, target, measured: dict, log_: RunLog, budget: int, arm: str) -> dict:
    """Combine the checker's verdict with run facts into one record."""
    passed, reason = T.check(task, instance, target.snapshot())
    rejections = target.rejections()
    notes: list[str] = []
    if not passed:
        if measured["provider_error"]:
            reason = "provider error: " + measured["provider_error"]
        else:
            if measured["api_calls"] >= budget and not measured["completed"]:
                notes.append("call budget exhausted")
            if arm == "guided" and log_.last_state and log_.last_state.get("state") == "stopped":
                notes.append(f"guided run stopped at step {int(log_.last_state['index']) + 1} of {log_.last_state['steps']}")
            if rejections:
                notes.append("first target rejection: " + f"{rejections[0]['tool']}:{rejections[0]['code']}")
            if log_.blocks:
                notes.append(f"{log_.blocks} gate block(s)")
            if measured.get("turn_exit_reason") and not notes:
                notes.append(f"turn ended: {measured['turn_exit_reason']}")
    return {
        "passed": passed,
        "failure_reason": ("; ".join([reason] + notes) if not passed else ""),
        "provider_error": measured["provider_error"],
        "budget": budget,
        "budget_exhausted": bool(measured["api_calls"] >= budget and not measured["completed"]),
        "turn_exit_reason": measured.get("turn_exit_reason"),
        "served_model": measured.get("served_model"),
        "api_calls": measured["api_calls"],
        "total_tokens": measured["total_tokens"],
        "input_tokens": measured["input_tokens"],
        "output_tokens": measured["output_tokens"],
        "cache_read_tokens": measured["cache_read_tokens"],
        "wall_s": measured["wall_s"],
        "tool_calls": len(log_.trace),
        "target_rejections": len(rejections),
        "rejection_codes": [item["code"] for item in rejections][:6],
        "gate_blocks": log_.blocks,
        "guide_turns": log_.guide_turns,
        "guide_first": log_.guide_first,
        "final_state": {k: v for k, v in target.snapshot().items() if k in ("reservations", "finished", "holds", "stock")},
        "trace": log_.trace,
    }


class Bench:
    def __init__(self, args, host, teacher_route: dict, follower_route: dict):
        self.args = args
        self.host = host
        self.teacher_route = teacher_route
        self.follower_route = follower_route
        self.run_id = uuid.uuid4().hex[:6]

    def budget(self, task) -> int:
        return self.args.call_budget or task.call_budget()

    def _execute(self, route, task, instance, session, message, arm) -> dict:
        target = IT.Target(instance)
        run_log = RunLog(self.host, session)
        CURRENT.update(target=target, log=run_log, session=session)
        budget = self.budget(task)
        try:
            agent = make_agent(route, session, budget)
            measured = converse(agent, message, route["api_key"], budget)
        finally:
            CURRENT.update(target=None, log=None, session=None)
        return judge(task, instance, target, measured, run_log, budget, arm) | {"_log": run_log}

    def teach(self, task, attempt: int = 0) -> tuple[dict, dict | None]:
        instance = T.make_instance(task, "teacher", attempt)
        session = f"bench-{self.run_id}-teach-{task.id}"
        name = f"bench-{task.id}"
        started = self.host.proof(f"start {name} fulfil inventory requests", session)
        record = {"task": task.id, "band": task.band, "teacher_model": self.teacher_route["model"]}
        if "Recording" not in started:
            record |= {"demonstration_passed": False, "saved": False, "failure_reason": "recording not started: " + scrub(started)}
            return record, None
        run = self._execute(self.teacher_route, task, instance, session, T.teacher_prompt_for(task, instance), "teacher")
        run.pop("_log", None)
        record |= {
            "demonstration_passed": run["passed"],
            "failure_reason": run["failure_reason"],
            "api_calls": run["api_calls"],
            "total_tokens": run["total_tokens"],
            "wall_s": run["wall_s"],
            "tool_calls": run["tool_calls"],
            "target_rejections": run["target_rejections"],
            "provider_error": run["provider_error"],
            "trace": run["trace"],
        }
        if not run["passed"]:
            self.host.proof("cancel", session)
            record |= {"saved": False, "save_reply": "not saved: the demonstration did not pass its own checker"}
            return record, None
        pairs = " ".join(f"{n}={v}" for n, v in T.input_pairs(task, instance))
        reply = self.host.proof("save " + pairs, session)
        saved = "saved" in reply.lower() and not reply.startswith("Procedure not saved")
        record |= {"saved": saved, "save_reply": scrub(reply)}
        if not saved:
            self.host.proof("cancel", session)
            return record, None
        return record, instance

    def follow(self, task, teacher_instance: dict, arm: str, k: int) -> dict:
        instance = T.make_instance(task, "run", k, avoid=teacher_instance)
        session = f"bench-{self.run_id}-{arm}-{task.id}-{k}"
        base = {"task": task.id, "band": task.band, "arm": arm, "k": k, "model": self.follower_route["model"]}
        if arm == "guided":
            pairs = " ".join(f"{n}={v}" for n, v in T.input_pairs(task, instance))
            reply = self.host.proof(f"run bench-{task.id} {pairs}", session)
            if "started" not in reply.lower() or reply.startswith("Run not started"):
                return base | {
                    "passed": False,
                    "failure_reason": "run not started: " + scrub(reply),
                    "provider_error": None, "budget": self.budget(task), "api_calls": 0, "total_tokens": 0,
                    "wall_s": 0.0, "tool_calls": 0, "target_rejections": 0, "gate_blocks": 0, "trace": [],
                }
        run = self._execute(self.follower_route, task, instance, session, T.prompt_for(task, instance), arm)
        run_log = run.pop("_log")
        state = run_log.last_state
        if arm == "guided":
            self.host.proof("cancel", session)
            run["guided_state"] = state
        return base | run


# ------------------------------------------------------------------ results


def summarize(teaching: list[dict], runs: list[dict]) -> dict:
    by_arm = {arm: [r for r in runs if r["arm"] == arm] for arm in ARMS}
    attempts = [a for task in teaching for a in (task.get("attempts") or [task])]
    summary: dict = {
        "arms": {arm: ab_stats.arm_summary(by_arm[arm]) for arm in ARMS},
        "comparison": ab_stats.compare(by_arm["guided"], by_arm["unguided"]),
        "reading": ab_stats.reading(by_arm["guided"], by_arm["unguided"]),
        "by_band": {},
        "by_task": {},
        "teaching": {
            "tasks_taught": len(teaching),
            "demonstrations_passed": sum(1 for t in teaching if t.get("demonstration_passed")),
            "procedures_saved": sum(1 for t in teaching if t.get("saved")),
            "attempts": len(attempts),
            "attempts_failed": sum(1 for a in attempts if not a.get("saved")),
            "api_calls": ab_stats.describe([a["api_calls"] for a in attempts if "api_calls" in a]),
            "total_tokens": ab_stats.describe(
                [a["total_tokens"] for a in attempts if "total_tokens" in a]
            ),
            "wall_s": ab_stats.describe([a["wall_s"] for a in attempts if "wall_s" in a]),
            "note": (
                "Every teaching attempt is counted, including failed ones. Teaching cost is "
                "reported on its own and is not amortised over the follower runs."
            ),
        },
    }
    for band in T.BANDS:
        rows = {arm: [r for r in by_arm[arm] if r["band"] == band] for arm in ARMS}
        if any(rows.values()):
            summary["by_band"][band] = {arm: ab_stats.arm_summary(rows[arm]) for arm in ARMS} | {
                "comparison": ab_stats.compare(rows["guided"], rows["unguided"])
            }
    for task_id in dict.fromkeys(r["task"] for r in runs):
        summary["by_task"][task_id] = {
            arm: f"{sum(1 for r in by_arm[arm] if r['task'] == task_id and r['passed'])}/"
                 f"{sum(1 for r in by_arm[arm] if r['task'] == task_id)}"
            for arm in ARMS
        }
    failures: dict = {}
    for r in runs:
        if not r["passed"]:
            key = (r["failure_reason"].split(";")[0])[:90]
            failures.setdefault(r["arm"], {}).setdefault(key, 0)
            failures[r["arm"]][key] += 1
    summary["failure_reasons"] = failures
    return summary


def build_record(args, tasks, teaching, runs, complete: bool, started_at: str, finished_at: str | None) -> dict:
    selftest = bool(getattr(args, "selftest", False))
    root = HERE.parents[1]
    manifest = (root / "proof-run" / "plugin.yaml").read_text(encoding="utf-8")
    version = re.search(r'version:\s*"?([0-9][^"\n]*)"?', manifest)
    return {
        "schema": SCHEMA,
        "date": started_at[:10],
        "complete": complete,
        "selftest": selftest,
        "claim_ceiling": (
            "SELFTEST: scripted loopback provider. This checks the driver's wiring and is not a measurement."
            if selftest
            else CLAIM_CEILING
        ),
        "coverage": {
            "planned_runs": len(tasks) * len(ARMS) * args.runs_per_arm,
            "executed_runs": len(runs),
            "missing_runs": len(tasks) * len(ARMS) * args.runs_per_arm - len(runs),
            "tasks_without_procedure": [
                item["task"] for item in teaching if not item.get("saved")
            ],
        },
        "config": {
            "follower_model": args.model,
            "teacher_model": args.teacher_model,
            "route": "configured provider (name omitted from the public record)",
            "tasks": [t.id for t in tasks],
            "runs_per_arm_per_task": args.runs_per_arm,
            "call_budget": args.call_budget or "per task: 5 x requests + 1 closing reply + 4 slack",
            "budgets": {t.id: (args.call_budget or t.call_budget()) for t in tasks},
            "retries": "no follower retry; teaching is attempted up to 3 times and every attempt is recorded",
            "teacher_note": "the teacher sees the order rules in its prompt; followers never do",
            "plugin_version": version.group(1) if version else None,
            "repo": git_head(root),
            "started_at": started_at,
            "finished_at": finished_at,
        },
        "teaching": teaching,
        "runs": runs,
        "summary": summarize(teaching, runs),
    }


# ------------------------------------------------------------------ main


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--provider", help="follower provider name as configured in Hermes")
    parser.add_argument("--model", help="follower model")
    parser.add_argument("--teacher-provider", help="teacher provider (default: --provider)")
    parser.add_argument("--teacher-model", help="teacher model")
    parser.add_argument("--tasks", default="all", help="'all', a number N (spread across bands) or comma-separated ids")
    parser.add_argument("--runs-per-arm", type=int, default=2, help="runs per arm per task (default 2)")
    parser.add_argument("--out", help="results path (default: results-<date>.json beside this file)")
    parser.add_argument("--call-budget", type=int, default=0, help="API calls per run for every task (default: per task)")
    parser.add_argument("--hermes-src", default=os.environ.get("HERMES_SRC", "~/.hermes/hermes-agent"))
    parser.add_argument("--state-dir", help="where the plugin keeps saved procedures (default: a temporary directory)")
    parser.add_argument("--no-preflight", action="store_true", help="skip the one-call credential check per route")
    parser.add_argument("--base-url", help="override the endpoint for both routes (loopback test servers only)")
    parser.add_argument("--api-key-env", help="environment variable holding a key for --base-url")
    parser.add_argument("--list-tasks", action="store_true", help="print the task set and exit")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run end to end against a scripted loopback provider (no key, no network); the output is a wiring "
        "check and is marked as such, never a result",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    mock = None
    if args.selftest:
        from mock_provider import MockServer

        mock = MockServer().start()
        os.environ["PROOF_RUN_SELFTEST_KEY"] = "selftest-not-a-credential"
        args.provider, args.model, args.teacher_model = "custom", args.model or "mock-correct", args.teacher_model or "mock-correct"
        args.teacher_provider = None
        args.base_url, args.api_key_env = mock.base_url, "PROOF_RUN_SELFTEST_KEY"
        args.no_preflight = True
        if args.tasks == "all":
            args.tasks = "3"
        if not args.out:
            args.out = str(Path(tempfile.mkdtemp(prefix="proof-run-selftest-")) / "selftest.json")
    try:
        return _main(args)
    finally:
        if mock is not None:
            mock.stop()


def _main(args) -> int:
    tasks = T.select_tasks(args.tasks)
    if args.list_tasks:
        for task in T.TASKS:
            print(f"{task.id:22s} {task.band:14s} requests={task.requests} budget={task.call_budget()}  {task.title}")
        return 0
    for flag in ("provider", "model", "teacher_model"):
        if not getattr(args, flag):
            raise SystemExit(f"--{flag.replace('_', '-')} is required")
    if args.runs_per_arm < 1:
        raise SystemExit("--runs-per-arm must be at least 1")
    started_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    out = Path(args.out) if args.out else HERE / f"results-{started_at[:10]}.json"

    bootstrap_hermes(Path(args.hermes_src).expanduser())
    teacher_route = resolve_route(args.teacher_provider or args.provider, args.teacher_model, args.base_url, args.api_key_env)
    follower_route = resolve_route(args.provider, args.model, args.base_url, args.api_key_env)
    if not args.no_preflight:
        for route, label in ((teacher_route, "teacher"), (follower_route, "follower")):
            ok, message = preflight(route, label)
            log("preflight " + message)
            if not ok:
                log("stopping: the provider did not accept the call. Nothing was run and no results were written.")
                return 3

    state_dir = Path(args.state_dir) if args.state_dir else Path(tempfile.mkdtemp(prefix="proof-run-bench-"))
    host = plugin_host.Host(state_dir)
    register_tools()
    install_hooks(host)
    bench = Bench(args, host, teacher_route, follower_route)

    teaching: list[dict] = []
    runs: list[dict] = []

    def save(complete: bool) -> None:
        finished = datetime.datetime.now().astimezone().isoformat(timespec="seconds") if complete else None
        write_atomic(out, build_record(args, tasks, teaching, runs, complete, started_at, finished))

    try:
        for index, task in enumerate(tasks):
            log(f"[{index + 1}/{len(tasks)}] teaching {task.id} ({task.band})")
            record, teacher_instance = None, None
            attempts: list[dict] = []
            for attempt in range(TEACH_ATTEMPTS):
                record, teacher_instance = bench.teach(task, attempt)
                attempts.append({k: v for k, v in record.items() if k != "trace"})
                if teacher_instance is not None:
                    break
                log(f"    attempt {attempt + 1}/{TEACH_ATTEMPTS} not saved")
            record["teaching_attempts"] = attempt + 1
            record["attempts"] = attempts
            teaching.append(record)
            log(f"    demo passed={record.get('demonstration_passed')} saved={record.get('saved')} {record.get('failure_reason', '')[:100]}")
            save(False)
            if teacher_instance is None:
                continue  # no procedure, so no matched arms for this task
            for k in range(args.runs_per_arm):
                order = ARMS if (index + k) % 2 == 0 else tuple(reversed(ARMS))
                for arm in order:
                    run = bench.follow(task, teacher_instance, arm, k)
                    runs.append(run)
                    log(f"    {arm:9s} k={k} passed={run['passed']} calls={run['api_calls']} tokens={run['total_tokens']} {run['failure_reason'][:90]}")
                    save(False)
    except KeyboardInterrupt:
        log("interrupted: partial results kept (complete=false)")
        save(False)
        return 130
    planned = len(tasks) * len(ARMS) * args.runs_per_arm
    missing_tasks = [item["task"] for item in teaching if not item.get("saved")]
    coverage_ok = len(runs) == planned and not missing_tasks
    save(coverage_ok)
    record = json.loads(out.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "out": str(out),
                "complete": coverage_ok,
                "coverage": record["coverage"],
                "summary": record["summary"],
            },
            sort_keys=False,
        )
    )
    if not coverage_ok:
        log(
            f"incomplete: {len(runs)}/{planned} planned runs executed; "
            f"tasks without a saved procedure: {missing_tasks or 'none'}"
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
