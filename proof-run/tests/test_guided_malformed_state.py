"""A malformed in-memory guided run must fail closed, never let an action through.

Drives the same registered surface as the acceptance tests. Each case corrupts the
run held for one session and checks the whole fail-closed contract: every effectful
tool in that session is blocked with a message naming /proof cancel, read-only tools
stay available, other sessions are untouched, the hook never raises, and
/proof cancel releases the session.
"""
from __future__ import annotations

import copy

try:
    from test_guided_acceptance import GuidedCase, deploy, term
except ModuleNotFoundError:
    from tests.test_guided_acceptance import GuidedCase, deploy, term


class MalformedRunStateTests(GuidedCase):
    def setUp(self):
        super().setUp()
        self.teach_deploy()
        self.serial = 0

    def _runs(self):
        return getattr(self.host, "_proof_run_state")["runs"]

    def _run_in_memory(self):
        """Start a valid run in a fresh session; return (session, live run dict)."""
        self.serial += 1
        session = f"run-{self.serial}"
        reply = self.host.proof("run deploy version=2.2.0", session)
        self.assertIn("started", reply.lower(), reply)
        return session, self._runs()[session]

    def _assert_fails_closed(self, session):
        # The hook must never raise: blocked() calls it directly.
        message = self.blocked("terminal", deploy("2.2.0"), term("ok"), session)
        self.assertIn("/proof cancel", message)
        # A tool the plan does not name is blocked too: the coverage is unreadable.
        message = self.blocked(
            "write_file", {"path": "/tmp/unrelated", "content": "ok"}, {"bytes_written": 2}, session
        )
        self.assertIn("/proof cancel", message)
        self.ran("read_file", {"path": "/srv/app/config.yaml"}, {"content": "ok"}, session)
        self.ran("terminal", {"command": "ls"}, term("ok"), "untouched-session")
        self.assertIn("cancelled", self.host.proof("cancel", session).lower())
        self.ran("terminal", {"command": "ls"}, term("ok"), session)

    def _corrupt(self, label, mutate):
        session, run = self._run_in_memory()
        with self.subTest(case=label):
            mutate(run)
            self._assert_fails_closed(session)

    def test_missing_or_empty_plan_structure_fails_closed(self):
        cases = {
            "plan missing": lambda run: run.pop("plan"),
            "plan empty mapping": lambda run: run.update(plan={}),
            "steps missing": lambda run: run["plan"].pop("steps"),
            "steps empty": lambda run: run["plan"].update(steps=[]),
            "tools missing": lambda run: run["plan"].pop("tools"),
            "tools empty": lambda run: run["plan"].update(tools=[]),
            "plan none": lambda run: run.update(plan=None),
        }
        for label, mutate in cases.items():
            self._corrupt(label, mutate)

    def test_wrongly_typed_plan_fields_fail_closed(self):
        cases = {
            "plan is a string": lambda run: run.update(plan="corrupt"),
            "plan is a list": lambda run: run.update(plan=[]),
            "steps is a mapping": lambda run: run["plan"].update(steps={"tool": "terminal"}),
            "tools is a string": lambda run: run["plan"].update(tools="terminal"),
            "tools holds a non-string": lambda run: run["plan"].update(tools=["terminal", 7]),
            "step is not a mapping": lambda run: run["plan"].update(steps=["terminal"]),
            "step has no tool": lambda run: run["plan"].update(steps=[{"args": {}}]),
            "step tool is not a string": lambda run: run["plan"].update(steps=[{"tool": 3}]),
        }
        for label, mutate in cases.items():
            self._corrupt(label, mutate)

    def test_step_index_outside_the_plan_fails_closed(self):
        cases = {
            "index past the end": 1,
            "index far past the end": 99,
            "index negative": -1,
            "index is text": "0",
            "index is a float": 0.0,
            "index is a bool": True,
            "index is none": None,
        }
        for label, value in cases.items():
            self._corrupt(label, lambda run, value=value: run.update(index=value))
        self._corrupt("index missing", lambda run: run.pop("index"))

    def test_plan_tools_that_disagree_with_the_steps_fail_closed(self):
        # The call must not slip through because the plan list omits the step's tool.
        cases = {
            "plan names another tool": lambda run: run["plan"].update(tools=["write_file"]),
            "plan omits a step tool": lambda run: run["plan"]["steps"].append({"tool": "execute_code"}),
            "plan lists a tool no step uses": lambda run: run["plan"].update(
                tools=["terminal", "execute_code"]
            ),
        }
        for label, mutate in cases.items():
            self._corrupt(label, mutate)

    def test_non_mapping_run_fails_closed_without_raising(self):
        for label, value in {
            "string": "corrupt",
            "list": [],
            "integer": 7,
            "none": None,
            "empty mapping": {},
        }.items():
            session, _ = self._run_in_memory()
            with self.subTest(case=label):
                self._runs()[session] = copy.deepcopy(value)
                self._assert_fails_closed(session)

    def test_status_reports_an_unreadable_run_instead_of_failing(self):
        session, run = self._run_in_memory()
        run["plan"] = "corrupt"
        status = self.host.proof("status", session)
        self.assertIn("/proof cancel", status)
        self.assertNotIn("Contract not created or changed", status)

    def test_guide_hook_reports_an_unreadable_run_without_raising(self):
        session, run = self._run_in_memory()
        run["plan"] = "corrupt"
        self.assertIn("unreadable", self.host.guide(session).lower())
        other, _ = self._run_in_memory()
        self._runs()[other] = "corrupt"
        guide = self.host.guide(other)
        self.assertIn("unreadable", guide.lower())
        self.assertIn("/proof cancel", guide)

    def test_valid_run_is_still_gated_exactly_as_before(self):
        session, _ = self._run_in_memory()
        message = self.blocked("terminal", {"command": "rm -rf build"}, term(""), session)
        self.assertIn("step 1 of 1", message.lower())
        # Tools the procedure never uses stay free for a readable run.
        self.ran("write_file", {"path": "/tmp/n.txt", "content": "x"}, {"bytes_written": 1}, session)
        self.ran("terminal", deploy("2.2.0"), term("ok"), session)


if __name__ == "__main__":
    import unittest

    unittest.main()
