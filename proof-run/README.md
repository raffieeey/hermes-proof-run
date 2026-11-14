# Proof Run plugin

v0.3.0.

Proof Run records a successful Hermes task at the tool boundary and saves it as a procedure with named inputs. A guided run tells a cheaper follower each step and checks every action before it executes; strict exact guards and checked-execution workflows remain available beside it.

The plugin needs only the standard plugin surface: the `/proof` command and the `pre_tool_call`, `post_tool_call`, `pre_llm_call` and `on_session_finalize` hooks. It makes no model or network calls.

## Guided runs

```text
$ /proof start deploy ship the api
Recording 'deploy'. Do the task once, then /proof stop.

$ read_file {"path": "/srv/app/config.yaml"}
  -> {"content": "replicas: 3\nimage: api:2.1.0\n", "file_size": 29, "total_lines": 2, "truncated": false}

$ terminal {"command": "./deploy.sh api 2.1.0"}
  -> {"cwd": "/srv/app", "error": null, "exit_code": 0, "output": "deployed api 2.1.0 ok"}

$ /proof save version=2.1.0
Procedure 'deploy' saved (v1, unbound). Inputs: version. Start it with /proof run deploy version=<value>. Nothing is gated outside a run.

$ /proof run deploy version=2.2.0
Run started: 'deploy' step 1 of 1 with version=2.2.0. The next message carries the guide; every action call is checked.
```

The saved contract is unbound and carries a `procedure` block with the recorded action steps and the `version` slot. Nothing is gated until a run starts. On every turn of the run's session, `pre_llm_call` returns the guide:

```text
Proof Run guided run 'deploy' (contract v1), step 1 of 1.
Bound inputs: version=2.2.0.
Reads recorded before this step (advisory unless listed below):
- read_file with arguments {"path": "/srv/app/config.yaml"}
Next: call terminal with arguments {"command": "./deploy.sh api 2.2.0"}.
This is the last step.
Follow these steps exactly. Do not improvise with this procedure's tools (terminal). If a step is blocked or fails, stop and report it.
```

A call to one of the procedure's action tools that does not equal the rendered next step is blocked before it runs:

```text
$ terminal {"command": "rm -rf build"}
  BLOCKED: Proof Run: guided run 'deploy', step 1 of 1: expected terminal with arguments {"command": "./deploy.sh api 2.2.0"}. Nothing was executed.
```

The correct call then runs, and the next `pre_llm_call` returns a one-time note: `Proof Run guided run 'deploy' completed: all 1 step(s) succeeded and this procedure's tools are released in this session.`

Reads are advisory unless pinned, except values a later action reuses from a read. Those are required live: a fresh snapshot token, stock version, revision, or request ID is resolved from a matching read in the run. `/proof pin <name> <tool>.<field>` makes another recorded read fact required.

## Commands

| Command | Behaviour |
|---|---|
| `/proof start <name> [intent]` | Start a recording in this session |
| `/proof save [name=value ...]` | Save the recording as a procedure; each `name=value` marks an input |
| `/proof run <name> [name=value ...]` | Start a guided run of a procedure with those inputs |
| `/proof inspect <name>` | Show a procedure's steps, inputs and pinnable read facts |
| `/proof pin <name> <tool>.<field>` | Require a recorded read fact before the steps that recorded it |
| `/proof unpin <name> <tool>.<field>` | Remove that requirement |
| `/proof cancel` | End this session's recording or run and release its tools |
| `/proof status` | Show saved contracts, and the session's run when one is active |
| `/proof stop` | End the recording and activate a strict exact gate |
| `/proof learn <name> [N]` | Record the current session's recent tool buffer as a strict guard |
| `/proof rebind <name>` | Re-demonstrate an existing contract |
| `/proof unbind <name>` | Stop enforcing one strict guard |
| `/proof capture start\|finish\|cancel` | Trace-only teaching capture for workflows |
| `/proof prepare <name> [json]` | Compile a trace and review bundle into a workflow candidate |
| `/proof promote <name> <revision>` | Authorize a reviewed workflow revision |
| `/proof runs`, `/proof show`, `/proof cancel-run`, `/proof resume` | Stored workflow runs and recovery |

`/proof run` and `/proof inspect` handle a saved procedure when a contract of that name has a `procedure` block; otherwise they keep the v0.2.0 workflow behaviour. Only one run is active per session, and a run refuses while the session is recording or the contract is bound as a strict guard.

## Tests

From this directory:

```sh
python3 -m pip install 'jsonschema>=4.18,<5'
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

The suite has 178 tests: the 146 pre-0.3.0 tests, 19 frozen guided-run acceptance tests in `tests/test_guided_acceptance.py`, and 13 persistence, live-binding, hook-order, input-templating, validation and malformed-command regression tests across `tests/test_guided_persistence.py` and `tests/test_guided_transfer.py`. The plugin runtime has no third-party dependencies; only some schema tests use `jsonschema`.

`scripts/guided_demo.py` in the repository root prints the transcript quoted above from a temporary data directory, with a context that offers only `register_command` and `register_hook`.

## Strict guard mode

`/proof stop` saves the recording as a bound contract and gates that tool name in every session of the profile. Actions must match the demonstrated arguments exactly, and fresh matching reads are required again; a mismatch pauses the contract until it is re-demonstrated. `/proof unbind <name>` stops enforcing it.

A strict guard gates that tool name in every session of the profile, so recording a general tool such as `terminal` blocks all other uses of it until `/proof unbind`.

## Checked-execution workflows

Workflow execution needs the checked-execution host patch in [docs/host-integration.md](../docs/host-integration.md) and [integration/hermes-checked-execution.patch](../integration/hermes-checked-execution.patch); the plugin alone cannot enable it, and a host without the required API returns `HOST_CAPABILITY_MISSING` rather than falling back to unchecked dispatch. Workflows support at most one effect and one bounded decision each, and `/proof prepare` recognizes a fixed catalog of example tool names.

`scripts/demo_runner.py` is a scripted teacher, not a frontier demonstration. `scripts/verify_host.py` exercises a real, pre-provisioned Hermes host, and `scripts/run_actual_host_journey.py` is the explicit actual-host opt-in using registered `/proof` commands. This plugin does not train models.

## Limits

- Guided runs check a cooperative follower and are not a sandbox. A model can still reach the same effect through a tool the procedure never used.
- Only the procedure's action tools are gated, and only in the run's session. Reads are advisory unless pinned, except values a later action reuses from a read, which must come from a fresh matching read.
- Step success is the absence of an error result, not a content match, so result text is never compared.
- The follower must reproduce each action's arguments itself; very large arguments are a poor fit.
- Run state is in memory. A plugin reload ends enforcement, and a lost run enforces nothing.
- A demonstration with a failed call cannot be saved; record a clean run.
- The strict guard and the checked-execution workflow engine are unchanged and separate from guided runs.
