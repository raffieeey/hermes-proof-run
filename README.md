# Proof Run

**Show a frontier model the way once. Reuse the procedure with a cheaper model.**

Proof Run records the checks and actions of a successful Hermes task, makes the procedure inspectable, and checks covered actions when you reuse it.

**v0.3.0** · Hermes Agent plugin · MIT

https://github.com/user-attachments/assets/a712c878-fc68-471e-ae21-008ff673e3de

*Product overview. Unmute the player for audio.* [Download the video](https://github.com/raffieeey/hermes-proof-run/releases/download/v0.2.0/proof-run-overview.mp4)

## What's new in v0.3.0

- **Guided runs:** record a task once, `/proof save` it as a procedure with named inputs, then `/proof run` it on new values. The follower is told the steps before it acts and each action call is checked.
- **New commands:** `/proof save`, `/proof pin` and `/proof unpin`. `/proof run` and `/proof inspect` now also handle a saved procedure.
- **New `pre_llm_call` hook:** the guide text is delivered through the standard hook, with no host patch and no model or network call by the plugin.
- Strict guard mode and checked-execution workflows are unchanged and separate.

See the [release notes](CHANGELOG.md#030---2026-11-14) for the full change list.

## Get started

Use Hermes Agent on a Linux/POSIX host with Python 3.11 or newer. Install from `main`:

```sh
hermes plugins install raffieeey/hermes-proof-run/proof-run --ref main --enable
```

v0.3.0 is on `main` and not yet tagged, and the Hermes catalog entry still pins v0.1.0, which has none of these commands.

Publishing a release does not update an already-installed plugin or running gateway automatically. Workflow execution also needs the [supported host setup](docs/host-integration.md).

## Guided runs

In the session where you will demonstrate the task:

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

`/proof save` uses the same recorder rules as `/proof stop`: at least one successful action, no failed call, no known secret patterns. Each `name=value` marks a demonstrated value as an input, so `version=2.1.0` becomes `{{version}}` inside `./deploy.sh api 2.1.0`. The contract is stored unbound with an added `procedure` block, in the same file and with the same versioning and history. It gates nothing outside a run.

### What the follower is told

The plugin returns this text from `pre_llm_call` for the run's session and nothing for any other session:

```text
Proof Run guided run 'deploy' (contract v1), step 1 of 1.
Bound inputs: version=2.2.0.
Reads recorded before this step (advisory unless listed below):
- read_file with arguments {"path": "/srv/app/config.yaml"}
Next: call terminal with arguments {"command": "./deploy.sh api 2.2.0"}.
This is the last step.
Follow these steps exactly. Do not improvise with this procedure's tools (terminal). If a step is blocked or fails, stop and report it.
```

### What a block looks like

An off-procedure call to one of the procedure's action tools is blocked before it runs:

```text
$ terminal {"command": "rm -rf build"}
  BLOCKED: Proof Run: guided run 'deploy', step 1 of 1: expected terminal with arguments {"command": "./deploy.sh api 2.2.0"}. Nothing was executed.
```

The correct call then runs, and the next `pre_llm_call` returns a one-time note: `Proof Run guided run 'deploy' completed: all 1 step(s) succeeded and this procedure's tools are released in this session.`

### Commands

| Command | Behaviour |
|---|---|
| `/proof save [name=value ...]` | Ends the session's recording and saves it as a procedure. Each `name=value` marks a demonstrated value as an input. Does not activate any gate. |
| `/proof run <name> [name=value ...]` | Starts a run of a saved procedure in this session. If no procedure has that name, the existing workflow `run` handles it as before. |
| `/proof inspect <name>` | For a procedure: shows steps, inputs with demonstrated values, and the read facts that can be pinned. Otherwise the existing workflow `inspect`. |
| `/proof pin <name> <tool>.<field>` | Makes a recorded read fact a required precondition for each step that recorded it. |
| `/proof unpin <name> <tool>.<field>` | Removes a pinned fact. |
| `/proof cancel` | Also ends the session's run and releases its tools. |
| `/proof status` | Existing lines unchanged. A procedure's line also says `procedure`; an active run adds a line with its name and `step i of n`. |

`/proof start`, `stop`, `learn`, `rebind`, `unbind` and every workflow command keep their v0.2.0 behaviour. Arguments to `save` and `run` are split with shell-style quoting, so `msg="fix bug"` is one input. Input names match `[A-Za-z][A-Za-z0-9_]{0,31}`.

Example `inspect` output for the procedure above:

```text
$ /proof inspect deploy
Procedure 'deploy' (contract v1, bound=False)
Inputs: version=2.1.0
Step 1 of 1: terminal {"command": "./deploy.sh api {{version}}"}
  read read_file {"path": "/srv/app/config.yaml"}
    read_file.content = sha256:66d3780d91e32a7e28fd59a14ab4bd45e684a88413a81adfefc71cd03f9bfffe (unchanged since recording)
    read_file.total_lines = 2
    read_file.file_size = 29
    read_file.truncated = false
Run it with: /proof run deploy version=<str>
Read facts are advisory unless pinned with /proof pin; values a later action reuses from a read are always required live.
```

### What is checked

Checks apply in the run's session only.

- Read-only tools are always allowed. A successful read is remembered until the next action step completes, for at most 300 seconds.
- Tools that appear in no action step are untouched and do not clear remembered reads.
- A call to one of the procedure's action tools must equal the next step: same tool, and arguments equal to the template rendered from the run's inputs and fresh reads, with JSON types preserved. A live read value that has not been fetched fails closed. Anything else is blocked, and the message names `step i of n` and shows the expected tool and arguments.
- A pinned fact needs a remembered read of the rendered read arguments whose field equals the recorded value. A missing read or a different value blocks, and the message names `<tool>.<field>` and the current value. Pinning a digest field means "unchanged since recording".
- While an allowed step is still running, a second action call is blocked.
- A step succeeds when its result is not an error by the existing error rule (exit code, status, error fields, host metadata). Result text is not compared, so timestamps and durations do not matter and nothing is marked unproven.
- After the last step succeeds the run ends and the tools are released. If a step fails, or after 5 consecutive blocked attempts, the run stops and holds: the procedure's action tools stay blocked in that session, and `/proof cancel` or the end of the session releases them.

Pinning a fact makes the follower confirm it before the step that recorded it. `/proof pin health check_service_health.status` replies `Fact check_service_health.status is pinned for procedure 'health'. It is required before each step that recorded it.`

```text
$ terminal {"command": "./deploy.sh api 2.1.0"}
  BLOCKED: Proof Run: guided run 'health', step 1 of 1: fresh read data is missing (check_service_health.version); perform the required read before this action.

$ check_service_health {"service": "api"}
  -> {"status": "degraded", "version": "2.1.0"}

$ terminal {"command": "./deploy.sh api 2.1.0"}
  BLOCKED: Proof Run: guided run 'health', step 1 of 1: pinned fact check_service_health.status is degraded but the procedure requires healthy. Fix the cause, or /proof cancel.
```

### Limits

- This guides and checks a cooperative follower. It is not a sandbox: a model can still reach the same effect through a tool the procedure never used.
- The follower must reproduce each action's arguments itself. Very large arguments are a poor fit.
- A demonstration with a failed call cannot be saved; record a clean run.
- Inputs must appear in the recorded action arguments. A value that appears in no recorded action argument cannot become an input.
- By default only actions are checked. Reads are advisory unless pinned, except values a later action reuses from a read, which must come from a successful matching read in the same run.
- Run state is in memory and does not survive a plugin reload. A lost run enforces nothing.
- The strict guard and the checked-execution workflow engine are unchanged and separate.

## Strict guard mode

`/proof start` and `/proof stop` keep their v0.2.0 behaviour. `/proof stop` turns the recording into a profile-wide, always-on gate keyed by tool name, with exact demonstrated arguments and fresh matching reads required again.

In the Hermes session where you will demonstrate the task:

```text
/proof start deploy-api
```

Ask your frontier model to perform the task, including its normal read checks and actions. When it has finished:

```text
/proof stop
```

Switch to your preferred cheaper model through Hermes and repeat the task. Proof Run checks the recorded action arguments and requires fresh matching reads before covered mutations. A skipped check or changed protected action is blocked before that call runs.

A strict guard gates that tool name in every session of the profile, so recording a general tool such as `terminal` blocks all other uses of it until `/proof unbind`.

Use any model or provider configured in Hermes for guarded repeats. There is no model-size requirement. When you intentionally change the task, use `/proof rebind deploy-api`, demonstrate the new version, then `/proof stop`.

## Three ways to reuse a demonstration

| Mode | Use it for | Setup |
|---|---|---|
| Guided runs | New input values, with the follower told the steps and checked step by step in one session | Standard Hermes plugin installation |
| Strict guard mode | Reusing the recorded checks and exact action arguments with your chosen Hermes model | Standard Hermes plugin installation |
| Reviewed workflows | New input IDs, explicit bindings, ordered execution, stored runs and recovery | [Supported workflow host](docs/host-integration.md) |

Guided runs use `/proof start`, `/proof save`, `/proof run` and `/proof cancel`. Guarded mode uses `/proof start` and `/proof stop`. Workflow mode uses a separate capture, review and promotion flow so you control which inputs, decisions and effects may vary. `/proof capture finish` saves a trace without activating a guarded contract; an existing legacy recording does not automatically become a runnable workflow.

## Checked-execution workflows (needs the host patch)

**Requires the version-matched checked-execution host patch in the [setup guide](docs/host-integration.md).** Installing the plugin alone does not enable workflow execution. A host without the required API returns `HOST_CAPABILITY_MISSING`; the runner does not fall back to unchecked dispatch.

The v0.2.0 workflow command path supports **at most one effect (mutation) and one bounded decision per workflow**. `/proof prepare` recognizes a fixed catalog of example-ticket and service-health/deploy tool names. Other tool integrations require implementation work, not just different review JSON.

With that setup:

1. `/proof capture start route-ticket` starts a teaching trace.
2. Demonstrate the task through checked tools.
3. `/proof capture finish` saves the trace.
4. Compile the trace with `/proof prepare route-ticket <review-json>`, using the bindings and policy in the [complete example review bundle](scripts/run_actual_host_journey.py).
5. `/proof inspect route-ticket` shows the actual bindings, policy, checks and model route.
6. `/proof promote route-ticket <revision>` makes the exact revision just inspected available. Use `1` only if that is the inspected candidate's revision.
7. `/proof run route-ticket {"ticket_id":"T-22"}` starts a stored run on the new input.

Promotion authorizes that immutable workflow revision without executing its business effects. Subsequent runs reuse the standing review on new inputs; normal Hermes tool checks and approvals still apply.

The runner performs the sequence, checks live state, enforces the reviewed effect and records its outcome. `/proof run` returns a run ID: use `/proof show <run-id>` to inspect its state and evidence rather than treating the initial acknowledgement as task completion. A verified effect readback does not independently verify that the follower's decision was correct.

The [complete synthetic ticket example](scripts/run_actual_host_journey.py) includes a review bundle and shows the registered-command path from capture through target verification. The [host setup guide](docs/host-integration.md) walks through running it.

## Useful commands

### Guided runs

| Command | Purpose |
|---|---|
| `/proof start <name> [intent]` | Start a recording; `/proof save` turns it into a procedure |
| `/proof save [name=value ...]` | Save the recording as a procedure with those inputs |
| `/proof run <name> [name=value ...]` | Start a guided run with those inputs |
| `/proof inspect <name>` | Show steps, inputs and pinnable read facts |
| `/proof pin <name> <tool>.<field>` | Require a recorded read fact before its steps |
| `/proof unpin <name> <tool>.<field>` | Remove that requirement |
| `/proof cancel` | End the session's run or recording and release its tools |
| `/proof status` | Show contracts and the session's run |

### Strict guard mode

| Command | Purpose |
|---|---|
| `/proof start <name> [intent]` | Start a session-local demonstration |
| `/proof stop` | Save a valid demonstration and activate its exact-action guard |
| `/proof status` | View saved guarded contracts |
| `/proof learn <name> [N]` | Capture the current session's recent tool buffer, up to 100 calls |
| `/proof rebind <name>` | Demonstrate an intentional change |
| `/proof cancel` | Discard the current guarded recording |
| `/proof unbind <name>` | Stop enforcing one guarded contract |

### Reviewed workflows

| Command | Purpose |
|---|---|
| `/proof capture start <name> [intent]` | Start a trace-only teaching capture |
| `/proof capture finish` | Save the captured trace without activating a legacy guard |
| `/proof capture cancel` | Discard the current workflow capture |
| `/proof prepare <name> [review-json]` | Compile a trace and review bundle into a candidate workflow |
| `/proof inspect <name>` | Inspect the candidate's bindings, policy, authority, checks and model route |
| `/proof promote <name> <revision>` | Authorize that reviewed revision without running its business effects |
| `/proof run <name> [inputs-json]` | Start a stored run of the promoted workflow on the supplied inputs |
| `/proof runs` | List workflow runs |
| `/proof show <run-id>` | Inspect a run and its evidence |
| `/proof cancel-run <run-id>` | Request cancellation; an already-dispatched effect may still complete. Check the run status |
| `/proof resume <run-id>` | Resume or reconcile using the original operation identity, not a fresh mutation |

Optional `review-json` and `inputs-json` arguments are single JSON objects. See the [workflow design](docs/design.md#public-commands) and [example review bundle](scripts/run_actual_host_journey.py) for the detailed input and policy format. A captured example does not grant new authority or infer safe input ranges.

## Model routes

Guided runs use whatever model the session uses; the plugin calls no model itself. Guarded repeats use any model or provider configured in Hermes. Workflows use an explicit route: a model-free control or a bounded local Ollama follower over loopback HTTP. The built-in follower selects an authorized ticket queue or abstains; the runtime executes the sequence. There is no integrated cloud workflow adapter or silent provider fallback. The cloud pilot below evaluates a separate guide-and-tools setup.

## Try the local walkthrough

From a clone of this repository:

```sh
python3 scripts/guided_demo.py
python3 scripts/demo_runner.py --out ./demo-result.json
```

`scripts/guided_demo.py` loads the plugin through a context that offers only `register_command` and `register_hook`, records a deploy task, saves it with an input, runs it on a new value, and prints the guide text, a blocked call, the completion note and a pinned-fact block. It makes no model calls; the "follower" is the script itself.

`scripts/demo_runner.py` is a fixture-data walkthrough that covers teaching, review, promotion, execution and stored target readback. It uses a scripted teacher and a model-free control, so you can inspect the mechanics without an API key.

For the full registered-command path through the patched Hermes host, use the isolated [workflow host self-test](docs/host-integration.md#check-the-complete-workflow). That example is also scripted and makes no model calls; it is not a frontier-to-cheaper-model quality benchmark.

## Cloud follower results

In our first paired cloud tool-following pilot, the same models received identical tasks, policy, tools and a five-call budget. Adding the reviewed frontier teacher guide changed full workflow completion as follows:

| Follower | Policy + tools | With teacher guide |
|---|---:|---:|
| `gpt-oss:20b` | 0/2 | 1/2 |
| `gemma4:31b` | 1/2 | 2/2 |

All eight runs reserved the correct quantity. Most missed completions ran out of calls before the closing step. Guided runs used about 22% more reported tokens. See the [measured results, methodology and offline replay](benchmarks/cloud-following-pilot/README.md) for the full comparison. This separate cloud pilot does not change the workflow runner's built-in local Ollama backend.

### Live guided-run check

A live synthetic inventory task ran on an isolated Hermes home with a real `AIAgent.run_conversation`. `glm-5.3` taught a five-tool procedure through the configured provider. Four `glm-5.3-flash` guided runs on a different request ID and SKU all completed with exactly one reserve, correct readback and no wrong mutation. Three unguided baseline runs with the same cheaper model also completed correctly.

| Arm | Passed | API calls per run | Reported total tokens |
|---|---:|---:|---:|
| Guided `glm-5.3-flash` | 4/4 | 5 | 15,832–16,174 |
| Unguided `glm-5.3-flash` | 3/3 | 6 | 16,707–16,733 |

This is one small task with four guided samples and three unguided samples. It does not prove general superiority, token savings after teaching cost, or model quality. It does show that the guided procedure resolves fresh snapshot tokens, stock versions, source revisions and operation keys from the run's own reads rather than replaying the teacher's. The recorded evidence and claim ceiling are in [benchmarks/live-guided](benchmarks/live-guided/README.md).

## Test the release

```sh
python3 -m pip install 'jsonschema>=4.18,<5'
cd proof-run
PYTHONPATH=. python3 -m unittest discover -s tests -v
cd ..
python3 -m unittest discover -s benchmarks/cloud-following-pilot -p 'test_*.py' -v
python3 benchmarks/cloud-following-pilot/replay.py
```

The plugin suite covers 178 tests: the 146 existing tests, 19 frozen guided-run acceptance tests, and 13 persistence, validation, live-binding, hook-order, input-templating, and malformed-command regression tests. The benchmark adds 10 driver tests and offline replay of 39 responses across 8 trials. These commands make no model calls. [Verification details](docs/verification.md) document the host checks and individual evidence records.

## How protection works

Guided runs compare each action call against the rendered next step in that session and release the procedure's tools when the run ends. Covered actions in strict guard mode retain their JSON types and exact values unless an operator explicitly reviews workflow bindings. Reads are session-bound, expire, and are consumed before covered mutations. A mismatching outcome pauses the affected contract until it is re-demonstrated.

Keep plugin state in a private, trusted directory. Read the [safety and storage reference](docs/safety.md) for tool coverage, freshness and filesystem behavior, and the [workflow design](docs/design.md) for identity, authorization and recovery.

## Architecture

The release implements the reviewed TDD execution loop. The [TDD-to-code map](docs/tdd-implementation.md) links capture, compilation, promotion, checked execution and recovery to their implementation and tests. See the [design reference](docs/design.md) for the data model and execution phases. The [guided-run design](docs/guided-runs.md) covers procedures, run state and verdicts.

## Background

Proof Run is a Hermes Agent Plugin Hackathon entry. Adjacent work includes workflow-record, crash-test, agent-replay/agentrec and Agent Behavioral Contracts. Proof Run combines demonstration capture, reviewable contracts, pre-execution checks and operator-controlled reuse.
