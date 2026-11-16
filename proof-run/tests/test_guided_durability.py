"""Durability tests for interrupted guided runs across plugin reloads."""
from __future__ import annotations

import json
import os
import sys
import time
from unittest import mock

try:
    from test_guided_acceptance import GuidedCase, StockHost, deploy, term
except ModuleNotFoundError:
    from tests.test_guided_acceptance import GuidedCase, StockHost, deploy, term


_GONE = object()


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

    def test_valid_marker_rearms_its_own_session_without_a_global_hold(self):
        # Positive control for the unparseable-marker hold: the same marker, intact,
        # re-arms only its own run and holds nothing else.
        marker_path = self._start_run()
        kept = marker_path.read_bytes()
        self.host = StockHost(self.root)
        self._assert_kept(marker_path, kept)
        self.assertIn("interrupted", self.blocked("terminal", deploy("2.2.0"), term("ok"), "run").lower())
        self.ran("write_file", {"path": "/tmp/unrelated", "content": "ok"}, {"bytes_written": 2}, "run")
        self.ran("terminal", {"command": "ls"}, term("ok"), "other")
        self.ran("write_file", {"path": "/tmp/unrelated", "content": "ok"}, {"bytes_written": 2}, "other")
        self.assertNotIn("hold", self.host.proof("status", "other").lower())

    def test_unparseable_marker_holds_until_cancel(self):
        marker_path = self._start_run()
        for label, payload in (
            ("truncated json", b"{not valid json"),
            ("empty file", b""),
            ("not utf-8", b"\xff\xfe\x00{"),
            ("nested past the parser limit", b"[" * 50000),
        ):
            with self.subTest(case=label):
                marker_path.write_bytes(payload)
                self.host = StockHost(self.root)
                # Loading keeps the file: it is evidence, not litter.
                self._assert_kept(marker_path, payload)
                # No session can be named from it, so every session is held.
                self._assert_effectful_blocked("run", scope="every session")
                self._assert_effectful_blocked("other", scope="every session")
                status = self.host.proof("status", "other")
                self.assertIn("hold", status.lower())
                self.assertIn("/proof cancel", status)
                # /proof cancel releases the hold and leaves the file where it is.
                self.assertIn("cancelled", self.host.proof("cancel", "other").lower())
                self._assert_kept(marker_path, payload)
                self.ran("terminal", {"command": "ls"}, term("ok"), "run")
                self.ran("terminal", {"command": "ls"}, term("ok"), "other")
                self.assertNotIn("hold", self.host.proof("status", "other").lower())
                # The file is still there, so a reload holds again until it is removed.
                self.host = StockHost(self.root)
                self._assert_effectful_blocked("other", scope="every session")
                marker_path.unlink()
                self.host = StockHost(self.root)
                self.ran("terminal", {"command": "ls"}, term("ok"), "other")

    def test_expired_marker_is_removed_without_rearming(self):
        marker_path = self._start_run()
        marker = json.loads(marker_path.read_text())
        marker["updated_at"] = time.time() - 24 * 60 * 60 - 1
        marker_path.write_text(json.dumps(marker))
        self.host = StockHost(self.root)
        self.ran("terminal", {"command": "ls"}, term("ok"), "run")
        self.assertFalse(marker_path.exists())
        self.assertNotIn("run deploy", self.host.proof("status", "run").lower())


    def test_unreadable_run_state_blocks_every_effectful_tool(self):
        self._start_run()
        run = getattr(self.host, "_proof_run_state")["runs"]["run"]
        # A shape the gate cannot read at all, so it cannot name covered tools.
        run["plan"] = "corrupt"
        message = self.blocked("terminal", deploy("2.2.0"), term("ok"), "run")
        self.assertIn("unreadable", message.lower())
        self.assertIn("/proof cancel", message)
        self.ran("read_file", {"path": "/srv/app/config.yaml"}, {"content": "ok"}, "run")
        # We cannot prove coverage, so unrelated effectful tools are blocked too.
        message = self.blocked(
            "write_file", {"path": "/tmp/unrelated", "content": "ok"}, {"bytes_written": 2}, "run"
        )
        self.assertIn("/proof cancel", message)
        self.assertIn("cancelled", self.host.proof("cancel", "run").lower())
        self.ran("terminal", {"command": "ls"}, term("ok"), "run")


    # ----- marker load failures must fail closed, not open -----

    def _rewrite_marker(self, marker_path, **changes):
        marker = json.loads(marker_path.read_text())
        for key, value in changes.items():
            if value is _GONE:
                marker.pop(key, None)
            else:
                marker[key] = value
        marker_path.write_text(json.dumps(marker))
        return marker_path.read_bytes()

    def _assert_effectful_blocked(self, session, *, scope):
        """Every non-read-only tool is blocked; the message names /proof cancel."""
        for tool, args in (
            ("terminal", {"command": "ls"}),
            ("write_file", {"path": "/tmp/unrelated", "content": "ok"}),
        ):
            message = self.blocked(tool, args, term("ok"), session)
            self.assertIn("/proof cancel", message)
            self.assertIn(scope, message)
        self.ran("read_file", {"path": "/srv/app/config.yaml"}, {"content": "ok"}, session)

    def _assert_kept(self, marker_path, expected):
        self.assertTrue(
            marker_path.exists() or marker_path.is_symlink(),
            "the marker was deleted while loading",
        )
        self.assertEqual(marker_path.read_bytes(), expected)

    def _skip_if_permissions_do_not_bind(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root bypasses file permissions")

    def _restore_on_cleanup(self, path, mode):
        def restore():
            try:
                os.chmod(path, mode)
            except FileNotFoundError:
                pass

        self.addCleanup(restore)

    def test_unsupported_schema_marker_is_kept_and_holds_its_session(self):
        for label, value in (
            ("newer", 2),
            ("far newer", 99),
            ("zero", 0),
            ("text", "1"),
            ("missing", _GONE),
        ):
            with self.subTest(schema=label):
                marker_path = self._start_run()
                kept = self._rewrite_marker(marker_path, schema_version=value)
                self.host = StockHost(self.root)
                self._assert_kept(marker_path, kept)
                self._assert_effectful_blocked("run", scope="this session")
                # Only the marker's own session is held.
                self.ran("terminal", {"command": "ls"}, term("ok"), "other")
                status = self.host.proof("status", "run")
                self.assertIn("hold", status.lower())
                self.assertIn("/proof cancel", status)
                self.assertNotIn("hold", self.host.proof("status", "other").lower())
                refused = self.host.proof("run deploy version=2.3.0", "run")
                self.assertTrue(refused.startswith("Run not started:"), refused)
                self.assertIn("/proof cancel", refused)
                begin = self.host.proof("start another", "run")
                self.assertTrue(begin.startswith("Recording not started:"), begin)
                # Refusals leave the marker untouched, so a reload still holds.
                self._assert_kept(marker_path, kept)
                self.host = StockHost(self.root)
                self._assert_effectful_blocked("run", scope="this session")
                self.assertIn("cancelled", self.host.proof("cancel", "run").lower())
                self.assertFalse(marker_path.exists())
                self.ran("terminal", {"command": "ls"}, term("ok"), "run")
                self.host = StockHost(self.root)
                self.ran("terminal", {"command": "ls"}, term("ok"), "run")
                self.assertNotIn("hold", self.host.proof("status", "run").lower())

    def test_session_finalize_releases_a_session_hold(self):
        marker_path = self._start_run()
        self._rewrite_marker(marker_path, schema_version=2)
        self.host = StockHost(self.root)
        self._assert_effectful_blocked("run", scope="this session")
        self.host.hooks["on_session_finalize"](session_id="run")
        self.ran("terminal", {"command": "ls"}, term("ok"), "run")

    def test_unsupported_schema_marker_without_a_readable_session_holds_every_session(self):
        marker_path = self._start_run()
        good = marker_path.read_bytes()
        for label, changes in (
            ("session missing", {"schema_version": 2, "session_id": _GONE}),
            ("session empty", {"schema_version": 2, "session_id": ""}),
            ("session not text", {"schema_version": 2, "session_id": 7}),
        ):
            with self.subTest(case=label):
                marker_path.write_bytes(good)
                kept = self._rewrite_marker(marker_path, **changes)
                self.host = StockHost(self.root)
                self._assert_kept(marker_path, kept)
                self._assert_effectful_blocked("run", scope="every session")
                self._assert_effectful_blocked("other", scope="every session")
                self.assertIn("hold", self.host.proof("status", "other").lower())
                self.assertIn("cancelled", self.host.proof("cancel", "other").lower())
                self.ran("terminal", {"command": "ls"}, term("ok"), "run")
                self.ran("terminal", {"command": "ls"}, term("ok"), "other")
                # The kept marker is not ours to remove from another session's cancel.
                self._assert_kept(marker_path, kept)

    def test_marker_that_names_no_session_holds_every_session_and_is_kept(self):
        marker_path = self._start_run()
        good = marker_path.read_bytes()
        oversize = b"{" + b" " * (64 * 1024) + b"}"
        for label, payload in (
            ("not an object", lambda m: ["schema_version", 1]),
            ("oversize", lambda m: oversize),
        ):
            with self.subTest(case=label):
                marker = json.loads(good)
                value = payload(marker)
                kept = value if isinstance(value, bytes) else json.dumps(value).encode()
                marker_path.write_bytes(kept)
                self.host = StockHost(self.root)
                self._assert_kept(marker_path, kept)
                self._assert_effectful_blocked("run", scope="every session")
                self._assert_effectful_blocked("other", scope="every session")
                self.assertIn("hold", self.host.proof("status", "run").lower())
                self.assertIn("cancelled", self.host.proof("cancel", "other").lower())
                self.ran("terminal", {"command": "ls"}, term("ok"), "other")

    def test_invalid_field_with_a_valid_session_id_holds_only_that_session(self):
        marker_path = self._start_run()
        good = marker_path.read_bytes()
        for label, changes in (
            ("step index outside the actions", {"step_index": 5}),
            ("negative step index", {"step_index": -1}),
            ("empty action tools", {"action_tools": []}),
            ("read-only action tool", {"action_tools": ["read_file"]}),
            ("invalid procedure name", {"procedure_name": "not a name!"}),
            ("invalid contract version", {"contract_version": 0}),
            ("invalid timestamp", {"updated_at": "yesterday"}),
        ):
            with self.subTest(case=label):
                marker_path.write_bytes(good)
                kept = self._rewrite_marker(marker_path, **changes)
                self.host = StockHost(self.root)
                self._assert_kept(marker_path, kept)
                self._assert_effectful_blocked("run", scope="this session")
                # A second session is untouched: effectful tools still run.
                self.ran("terminal", {"command": "ls"}, term("ok"), "other")
                self.ran("write_file", {"path": "/tmp/unrelated", "content": "ok"}, {"bytes_written": 2}, "other")
                self.assertIn("hold", self.host.proof("status", "run").lower())
                self.assertNotIn("hold", self.host.proof("status", "other").lower())
                # Another session's cancel does not release this session's hold.
                self.assertIn("cancelled", self.host.proof("cancel", "other").lower())
                self._assert_effectful_blocked("run", scope="this session")
                self._assert_kept(marker_path, kept)
                self.assertIn("cancelled", self.host.proof("cancel", "run").lower())
                self.assertFalse(marker_path.exists())
                self.ran("terminal", {"command": "ls"}, term("ok"), "run")

    def test_marker_whose_session_cannot_be_trusted_holds_every_session(self):
        marker_path = self._start_run()
        good = marker_path.read_bytes()
        for label, changes in (
            ("session missing", {"session_id": _GONE}),
            ("session empty", {"session_id": ""}),
            ("session not text", {"session_id": 7}),
            ("session too long", {"session_id": "s" * 1025}),
            # Valid text, but the file is not the one that session's marker would use.
            ("filename does not match the session", {"session_id": "someone-else"}),
        ):
            with self.subTest(case=label):
                marker_path.write_bytes(good)
                kept = self._rewrite_marker(marker_path, **changes)
                self.host = StockHost(self.root)
                self._assert_kept(marker_path, kept)
                self._assert_effectful_blocked("run", scope="every session")
                self._assert_effectful_blocked("other", scope="every session")
                self.assertIn("hold", self.host.proof("status", "other").lower())
                self.assertIn("cancelled", self.host.proof("cancel", "other").lower())
                self.ran("terminal", {"command": "ls"}, term("ok"), "run")
                self.ran("terminal", {"command": "ls"}, term("ok"), "other")
                self._assert_kept(marker_path, kept)

    def test_unreadable_marker_directory_holds_every_session(self):
        self._skip_if_permissions_do_not_bind()
        marker_path = self._start_run()
        directory = marker_path.parent
        directory.chmod(0o000)
        self._restore_on_cleanup(directory, 0o700)
        self.host = StockHost(self.root)
        # Registration completed: the command and every hook are installed.
        self.assertIsNotNone(self.host.command)
        for hook in ("pre_tool_call", "post_tool_call", "pre_llm_call", "on_session_finalize"):
            self.assertIn(hook, self.host.hooks)
        self._assert_effectful_blocked("run", scope="every session")
        self._assert_effectful_blocked("other", scope="every session")
        status = self.host.proof("status", "other")
        self.assertIn("hold", status.lower())
        self.assertIn("/proof cancel", status)
        self.assertIn("cancelled", self.host.proof("cancel", "other").lower())
        self.ran("terminal", {"command": "ls"}, term("ok"), "run")
        self.ran("terminal", {"command": "ls"}, term("ok"), "other")
        self.assertNotIn("hold", self.host.proof("status", "other").lower())
        # The unreachable marker was not pretended away: it re-arms once readable.
        directory.chmod(0o700)
        self.assertTrue(marker_path.exists())
        self.host = StockHost(self.root)
        self.assertIn("interrupted", self.blocked("terminal", deploy("2.2.0"), term("ok"), "run").lower())

    def test_unreadable_contracts_store_holds_every_session_and_status_still_says_so(self):
        self._skip_if_permissions_do_not_bind()
        marker_path = self._start_run()
        contracts = marker_path.parent.parent
        contracts.chmod(0o000)
        self._restore_on_cleanup(contracts, 0o700)
        self.host = StockHost(self.root)
        self.assertIn("pre_tool_call", self.host.hooks)
        self._assert_effectful_blocked("other", scope="every session")
        status = self.host.proof("status", "other")
        self.assertIn("hold", status.lower())
        self.assertIn("/proof cancel", status)
        self.assertNotIn("Contract not created or changed", status)
        self.assertIn("cancelled", self.host.proof("cancel", "other").lower())
        # The marker hold is released; the strict-guard path still refuses an
        # unreadable contract store on its own account.
        message = self.blocked("terminal", {"command": "ls"}, term("ok"), "other")
        self.assertIn("contract is unreadable", message)
        self.assertNotIn("every session", message)
        contracts.chmod(0o700)
        self.ran("terminal", {"command": "ls"}, term("ok"), "other")

    def test_loading_leaves_a_read_only_marker_directory_alone(self):
        self._skip_if_permissions_do_not_bind()
        marker_path = self._start_run()
        directory = marker_path.parent
        directory.chmod(0o500)
        self._restore_on_cleanup(directory, 0o700)
        self.host = StockHost(self.root)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o500)
        self.assertTrue(marker_path.exists())
        # A readable directory is not a load failure; the run re-arms held as before.
        self.assertIn("interrupted", self.blocked("terminal", deploy("2.2.0"), term("ok"), "run").lower())
        self.ran("terminal", {"command": "ls"}, term("ok"), "other")

    def test_marker_directory_listing_failure_holds_every_session(self):
        self._start_run()
        real_listdir = os.listdir

        def failing_listdir(path="."):
            if isinstance(path, int):
                raise PermissionError(13, "Permission denied")
            return real_listdir(path)

        with mock.patch("os.listdir", failing_listdir):
            self.host = StockHost(self.root)
        self._assert_effectful_blocked("run", scope="every session")
        self._assert_effectful_blocked("other", scope="every session")
        self.assertIn("cancelled", self.host.proof("cancel", "other").lower())
        self.ran("terminal", {"command": "ls"}, term("ok"), "other")

    def test_unreadable_marker_file_holds_every_session_and_is_not_deleted(self):
        self._skip_if_permissions_do_not_bind()
        marker_path = self._start_run()
        original = marker_path.read_bytes()
        marker_path.chmod(0o000)
        self._restore_on_cleanup(marker_path, 0o600)
        self.host = StockHost(self.root)
        self._assert_effectful_blocked("run", scope="every session")
        self._assert_effectful_blocked("other", scope="every session")
        self.assertIn("cancelled", self.host.proof("cancel", "other").lower())
        self.ran("terminal", {"command": "ls"}, term("ok"), "other")
        marker_path.chmod(0o600)
        self._assert_kept(marker_path, original)

    def test_marker_that_is_not_a_regular_file_holds_every_session_and_is_not_deleted(self):
        marker_path = self._start_run()
        target = self.root / "elsewhere.json"
        target.write_bytes(marker_path.read_bytes())
        marker_path.unlink()
        marker_path.symlink_to(target)
        self.host = StockHost(self.root)
        self.assertTrue(marker_path.is_symlink())
        self._assert_effectful_blocked("run", scope="every session")
        self._assert_effectful_blocked("other", scope="every session")
        self.assertIn("cancelled", self.host.proof("cancel", "other").lower())
        self.ran("terminal", {"command": "ls"}, term("ok"), "other")
        self.assertTrue(marker_path.is_symlink())

    def test_marker_write_failure_leaves_the_run_stopped_and_blocked(self):
        h = self.host
        h.proof("start release", "teach")
        self.ran("terminal", {"command": "./build.sh 2.1.0"}, term("built"), "teach")
        self.ran("terminal", deploy("2.1.0"), term("ok"), "teach")
        self.assertIn("saved", h.proof("save version=2.1.0", "teach").lower())
        self.assertIn("started", h.proof("run release version=2.2.0", "run").lower())
        plugin = sys.modules["proof_run_plugin_under_test"]
        with mock.patch.object(plugin.core, "_atomic_write", side_effect=OSError("disk full")):
            self.ran("terminal", {"command": "./build.sh 2.2.0"}, term("built"), "run")
            message = self.blocked("terminal", deploy("2.2.0"), term("ok"), "run")
        self.assertIn("stopped", message.lower())
        self.assertIn("/proof cancel", message)
        self.assertIn("stopped", self.host.proof("status", "run").lower())
        # The marker written at start survives, so a reload still holds the run.
        self.host = StockHost(self.root)
        self.assertIn("interrupted", self.blocked("terminal", deploy("2.2.0"), term("ok"), "run").lower())


if __name__ == "__main__":
    import unittest

    unittest.main()
