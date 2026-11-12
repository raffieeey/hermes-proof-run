"""Crash/reopen recovery tests using a real subprocess and file-backed counters."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_FIX = Path(__file__).resolve().parent / "fixtures"
if str(_FIX) not in sys.path:
    sys.path.insert(0, str(_FIX))

from runtime_support import FileHost, load_fixture, new_state_dir  # noqa: E402

from proof_run.engine import WorkflowRunner  # noqa: E402
from proof_run.store import WorkflowStore  # noqa: E402

ACTOR = {"id": "operator", "profile_id": "test-profile", "session_id": "sess-1"}
WORKER = Path(__file__).resolve().parent / "fixtures" / "runtime_crash_worker.py"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "profile"
        self.root.mkdir()
        self.state = new_state_dir(Path(self.temp.name))
        self.store = WorkflowStore(self.root)
        self.host = FileHost(self.state, self.store)
        self.decision_path = Path(self.temp.name) / "decision.json"

    def _promote_exact(self) -> dict:
        doc = load_fixture("exact_ensure_fixture_release.json")
        stored = self.store.put_candidate(doc["workflow_id"], doc)
        return self.store.promote(stored["workflow_id"], stored["revision"], ACTOR)

    def _env(self) -> dict:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["HERMES_HOME"] = str(Path(self.temp.name) / "hermes-home")
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        return env

    def _spawn(self, run_id: str, follower: str = "") -> subprocess.CompletedProcess:
        cmd = [
            sys.executable,
            str(WORKER),
            "--root",
            str(self.root),
            "--run-id",
            run_id,
            "--state-dir",
            str(self.state),
        ]
        if follower:
            cmd.extend(["--follower", follower])
        return subprocess.run(
            cmd,
            env=self._env(),
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_crash_after_effect_reconciles_without_redispatch(self):
        wf = self._promote_exact()
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        self.host.configure(crash="after_handler")
        crashed = self._spawn(run["run_id"])
        self.assertNotEqual(crashed.returncode, 0)
        self.assertEqual(FileHost(self.state, self.store).counters()["effect"], 1)
        self.assertEqual(FileHost(self.state, self.store).world()["deploy"]["version"], "v1")
        reopened = WorkflowStore(self.root)
        host = FileHost(self.state, reopened)
        host.configure(crash="none")
        result = WorkflowRunner(reopened, host).resume(run["run_id"])
        self.assertIn(result["state"], ("completed", "verified"))
        self.assertEqual(result["effect_readback"], "verified")
        self.assertEqual(host.counters()["effect"], 1)
        self.assertGreaterEqual(host.counters()["status"], 1)
        self.assertEqual(host.world()["deploy"]["version"], "v1")

    def test_crash_before_handler_unknown_no_retry(self):
        wf = self._promote_exact()
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        self.host.configure(crash="before_handler")
        crashed = self._spawn(run["run_id"])
        self.assertNotEqual(crashed.returncode, 0)
        self.assertEqual(FileHost(self.state, self.store).counters()["effect"], 0)
        reopened = WorkflowStore(self.root)
        host = FileHost(self.state, reopened)
        host.configure(crash="none")
        result = WorkflowRunner(reopened, host).resume(run["run_id"])
        self.assertEqual(result["state"], "unknown_outcome")
        self.assertEqual(result.get("error_code"), "UNKNOWN_OUTCOME")
        self.assertEqual(host.counters()["effect"], 0)
        self.assertEqual(host.world()["deploy"]["version"], "v0")

    def test_cancel_before_dispatch_prevents_effect(self):
        wf = self._promote_exact()
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        cancelled = WorkflowRunner(self.store, self.host).cancel(run["run_id"])
        self.assertTrue(
            cancelled.get("cancel_requested")
            or cancelled.get("state") in ("canceled", "cancel_requested")
        )
        result = WorkflowRunner(self.store, self.host).drive(run["run_id"])
        self.assertIn(result["state"], ("canceled", "cancelled"))
        self.assertEqual(self.host.counters()["effect"], 0)

    def test_stale_fence_cannot_admit_after_reopen_lease(self):
        wf = self._promote_exact()
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        lease1 = self.store.acquire_lease(run["run_id"], "W-old")
        payload = {"service": "fixture-api", "version": "v1", "operation_key": "K-fence"}
        self.store.persist_effect_intent(
            run_id=run["run_id"],
            node_id="deploy",
            operation_key="K-fence",
            resource_key="tenant-A|fixture-api|deploy",
            payload=payload,
            expectations={},
            fence=lease1["fence"],
        )
        reopened = WorkflowStore(self.root)
        lease2 = reopened.acquire_lease(run["run_id"], "W-new")
        self.assertNotEqual(lease2["fence"], lease1["fence"])
        stale_ctx = {
            "run_id": run["run_id"],
            "profile_id": "test-profile",
            "session_id": "sess-1",
            "workflow_digest": wf["content_digest"],
            "fence": lease1["fence"],
            "intent_ref": {"operation_key": "K-fence"},
        }
        with self.assertRaises(Exception) as ctx:
            reopened.admit_dispatch(stale_ctx, "deploy_service", payload)
        code = getattr(ctx.exception, "code", "")
        self.assertEqual(code, "FENCE_STALE")
        fresh_ctx = dict(stale_ctx)
        fresh_ctx["fence"] = lease2["fence"]
        reopened.admit_dispatch(fresh_ctx, "deploy_service", payload)

    def test_not_found_status_is_inconclusive(self):
        wf = self._promote_exact()
        run = self.store.create_run(wf["workflow_id"], {}, ACTOR)
        self.host.configure(crash="after_admit")
        crashed = self._spawn(run["run_id"])
        self.assertNotEqual(crashed.returncode, 0)
        # after_admit increments the effect counter then dies before mutation if
        # FileHost increments first; require no successful target change and no
        # second dispatch on resume.
        reopened = WorkflowStore(self.root)
        host = FileHost(self.state, reopened)
        host.configure(crash="none")
        before = host.counters()["effect"]
        result = WorkflowRunner(reopened, host).resume(run["run_id"])
        self.assertEqual(result["state"], "unknown_outcome")
        self.assertEqual(host.counters()["effect"], before)
        self.assertEqual(host.world()["deploy"]["version"], "v0")


if __name__ == "__main__":
    unittest.main()
