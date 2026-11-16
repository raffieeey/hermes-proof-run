"""Durability tests for interrupted guided runs across plugin reloads."""
from __future__ import annotations

import json
import time

try:
    from test_guided_acceptance import GuidedCase, StockHost, deploy, term
except ModuleNotFoundError:
    from tests.test_guided_acceptance import GuidedCase, StockHost, deploy, term


class GuidedDurabilityTests(GuidedCase):
    def _marker_paths(self):
        return sorted((self.root / "contracts" / "guided-runs").glob("*.json"))

    def _start_run(self, session="run"):
        self.teach_deploy()
        reply = self.host.proof("run deploy version=2.2.0", session)
        self.assertIn("started", reply.lower(), reply)
        paths = self._marker_paths()
        self.assertEqual(len(paths), 1)
        return paths[0]

    def test_interrupted_run_rearms_held_and_cancel_removes_marker(self):
        marker_path = self._start_run()
        marker = json.loads(marker_path.read_text())
        self.assertEqual(marker["schema_version"], 1)
        self.assertEqual(marker["procedure_name"], "deploy")
        self.assertEqual(marker["contract_version"], 1)
        self.assertEqual(marker["session_id"], "run")
        self.assertEqual(marker["step_index"], 0)
        self.assertEqual(marker["action_tools"], ["terminal"])
        self.assertIsInstance(marker["updated_at"], (int, float))

        self.host = StockHost(self.root)
        status = self.host.proof("status", "run")
        self.assertIn("stopped", status.lower())
        self.assertIn("interrupted", self.host.guide("run").lower())
        begin = self.host.proof("start another", "run")
        self.assertTrue(begin.startswith("Recording not started:"), begin)
        self.assertIn("/proof cancel", begin)
        self.assertTrue(marker_path.exists())
        message = self.blocked("terminal", deploy("2.2.0"), term("ok"), "run")
        self.assertIn("interrupted", message.lower())
        self.assertIn("/proof cancel", message)
        self.assertIn("stopped", message.lower())
        self.assertIn("report", message.lower())
        self.assertIn("improvise", message.lower())
        self.ran("read_file", {"path": "/srv/app/config.yaml"}, {"content": "ok"}, "run")
        self.ran("write_file", {"path": "/tmp/unrelated", "content": "ok"}, {"bytes_written": 2}, "run")

        cancelled = self.host.proof("cancel", "run")
        self.assertIn("cancelled", cancelled.lower())
        self.assertFalse(marker_path.exists())
        self.ran("terminal", {"command": "ls"}, term("ok"), "run")

    def test_step_completion_updates_marker_index_in_action_order(self):
        h = self.host
        h.proof("start release", "teach")
        self.ran("terminal", {"command": "./build.sh 2.1.0"}, term("built"), "teach")
        self.ran("terminal", deploy("2.1.0"), term("ok"), "teach")
        self.assertIn("saved", h.proof("save version=2.1.0", "teach").lower())
        self.assertIn("started", h.proof("run release version=2.2.0", "run").lower())
        marker_path = self._marker_paths()[0]
        self.ran("terminal", {"command": "./build.sh 2.2.0"}, term("built"), "run")
        marker = json.loads(marker_path.read_text())
        self.assertEqual(marker["step_index"], 1)
        self.assertEqual(marker["action_tools"], ["terminal", "terminal"])
        self.ran("terminal", deploy("2.2.0"), term("ok"), "run")
        self.assertFalse(marker_path.exists())

    def test_failed_step_keeps_a_stopped_marker_for_reload(self):
        marker_path = self._start_run()
        original = json.loads(marker_path.read_text())
        self.ran("terminal", deploy("2.2.0"), term("failed", code=1), "run")
        stopped = json.loads(marker_path.read_text())
        self.assertEqual(stopped["step_index"], 0)
        self.assertGreaterEqual(stopped["updated_at"], original["updated_at"])
        self.host = StockHost(self.root)
        message = self.blocked("terminal", deploy("2.2.0"), term("ok"), "run")
        self.assertIn("interrupted", message.lower())

    def test_session_finalize_deletes_marker(self):
        marker_path = self._start_run()
        self.host.hooks["on_session_finalize"](session_id="run")
        self.assertFalse(marker_path.exists())
        self.ran("terminal", {"command": "ls"}, term("ok"), "run")

    def test_cancelled_run_leaves_no_marker(self):
        self._start_run()
        self.assertIn("cancelled", self.host.proof("cancel", "run").lower())
        self.assertEqual(self._marker_paths(), [])

    def test_completed_run_leaves_no_marker(self):
        self._start_run()
        marker_path = self._marker_paths()[0]
        self.ran("terminal", deploy("2.2.0"), term("ok"), "run")
        self.assertFalse(marker_path.exists())
        self.assertEqual(self._marker_paths(), [])

    def test_corrupt_marker_is_dropped_without_blocking_unrelated_tool(self):
        marker_path = self._start_run()
        marker_path.write_text("{not valid json")
        self.host = StockHost(self.root)
        self.ran("write_file", {"path": "/tmp/unrelated", "content": "ok"}, {"bytes_written": 2}, "run")
        self.assertFalse(marker_path.exists())
        status = self.host.proof("status", "run")
        self.assertNotIn("run deploy", status.lower())

    def test_expired_marker_is_removed_without_rearming(self):
        marker_path = self._start_run()
        marker = json.loads(marker_path.read_text())
        marker["updated_at"] = time.time() - 24 * 60 * 60 - 1
        marker_path.write_text(json.dumps(marker))
        self.host = StockHost(self.root)
        self.ran("terminal", {"command": "ls"}, term("ok"), "run")
        self.assertFalse(marker_path.exists())
        self.assertNotIn("run deploy", self.host.proof("status", "run").lower())


if __name__ == "__main__":
    import unittest

    unittest.main()
