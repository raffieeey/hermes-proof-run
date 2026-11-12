# Proof Run

**Show a frontier model the way once. Reuse the procedure with a cheaper model.**

Proof Run records the checks and actions of a successful Hermes task, makes the procedure inspectable, and checks covered actions when you reuse it.

**v0.2.0** · Hermes Agent plugin · MIT

https://github.com/user-attachments/assets/a712c878-fc68-471e-ae21-008ff673e3de

*Product overview. Unmute the player for audio.* [Watch separately](https://github.com/user-attachments/assets/a712c878-fc68-471e-ae21-008ff673e3de) · [Download the video](https://github.com/raffieeey/hermes-proof-run/releases/download/v0.2.0/proof-run-overview.mp4)

## Get started

Use Hermes Agent on a Linux/POSIX host with Python 3.11 or newer. Install the release:

```sh
hermes plugins install raffieeey/hermes-proof-run/proof-run --ref v0.2.0 --enable
```

In the Hermes session where you will demonstrate the task:

```text
/proof start deploy-api
```

Ask your frontier model to perform the task, including its normal read checks and actions. When it has finished:

```text
/proof stop
```

Switch to your preferred cheaper model through Hermes and repeat the task. Proof Run checks the recorded action arguments and requires fresh matching reads before covered mutations. A skipped check or changed protected action is blocked before that call runs.

Use any model or provider configured in Hermes for guarded repeats. There is no model-size requirement. When you intentionally change the task, use `/proof rebind deploy-api`, demonstrate the new version, then `/proof stop`.

## Two ways to reuse a demonstration

| Mode | Use it for | Setup |
|---|---|---|
| Guarded repeats | Reusing the recorded checks and exact action arguments with your chosen Hermes model | Standard Hermes plugin installation |
| Reviewed workflows | New input IDs, explicit bindings, ordered execution, stored runs and recovery | [Supported workflow host](docs/host-integration.md) |

Guarded mode uses `/proof start` and `/proof stop`. Workflow mode uses a separate capture, review and promotion flow so you control which inputs, decisions and effects may vary.

## Run a reviewed workflow

With the [supported Hermes setup](docs/host-integration.md):

1. `/proof capture start route-ticket` starts a teaching trace.
2. Demonstrate the task through checked tools.
3. `/proof capture finish` saves the trace.
4. Compile the trace with `/proof prepare route-ticket <review-json>`, using the bindings and policy in the [complete example review bundle](scripts/run_actual_host_journey.py).
5. `/proof inspect route-ticket` shows the actual bindings, policy, checks and model route.
6. `/proof promote route-ticket 1` makes the reviewed revision available.
7. `/proof run route-ticket {"ticket_id":"T-22"}` starts a stored run on the new input.

The runner performs the sequence, checks live state, enforces the reviewed effect and records its outcome. An optional local Ollama follower selects an allowed decision or asks for review. Workflow model routes are explicit and do not silently change providers.

The [complete synthetic ticket example](scripts/run_actual_host_journey.py) includes a review bundle and shows the registered-command path from capture through target verification. The [host setup guide](docs/host-integration.md) walks through running it.

Useful commands:

| Command | Purpose |
|---|---|
| `/proof status` | View saved guarded contracts |
| `/proof learn <name> [N]` | Capture the current session's recent tool buffer, up to 100 calls |
| `/proof rebind <name>` | Demonstrate an intentional change |
| `/proof cancel` | Discard the current guarded recording |
| `/proof unbind <name>` | Stop enforcing one guarded contract |
| `/proof runs` | List workflow runs |
| `/proof show <run-id>` | Inspect a run and its evidence |
| `/proof cancel-run <run-id>` | Cancel a workflow run |
| `/proof resume <run-id>` | Reconcile an interrupted run using its stored operation identity |

## Try the local walkthrough

From a clone of this repository:

```sh
python3 scripts/demo_runner.py --out ./demo-result.json
```

This fixture-data walkthrough covers teaching, review, promotion, execution and stored target readback. It uses a scripted teacher and a model-free control, so you can inspect the mechanics without an API key.

## Cloud follower results

In our first paired cloud tool-following pilot, the same models received identical tasks, policy, tools and a five-call budget. Adding the reviewed frontier teacher guide changed full workflow completion as follows:

| Follower | Policy + tools | With teacher guide |
|---|---:|---:|
| `gpt-oss:20b` | 0/2 | 1/2 |
| `gemma4:31b` | 1/2 | 2/2 |

All eight runs reserved the correct quantity. Most missed completions ran out of calls before the closing step. Guided runs used about 22% more reported tokens. See the [measured results, methodology and offline replay](benchmarks/cloud-following-pilot/README.md) for the full comparison. This separate cloud pilot does not change the workflow runner's built-in local Ollama backend.

## Test the release

```sh
python3 -m pip install 'jsonschema>=4.18,<5'
cd proof-run
PYTHONPATH=. python3 -m unittest discover -s tests -v
cd ..
python3 -m unittest discover -s benchmarks/cloud-following-pilot -p 'test_*.py' -v
python3 benchmarks/cloud-following-pilot/replay.py
```

The plugin suite covers 146 tests. The benchmark adds 10 driver tests and offline replay of 39 responses across 8 trials. These commands make no model calls. [Verification details](docs/verification.md) document the host checks and individual evidence records.

## How protection works

Covered actions retain their JSON types and exact values unless an operator explicitly reviews workflow bindings. Reads are session-bound, expire, and are consumed before covered mutations. A mismatching outcome pauses the affected contract until it is re-demonstrated.

Keep plugin state in a private, trusted directory. Read the [safety and storage reference](docs/safety.md) for tool coverage, freshness and filesystem behavior, and the [workflow design](docs/design.md) for identity, authorization and recovery.

## Architecture

The release implements the reviewed TDD execution loop. The [TDD-to-code map](docs/tdd-implementation.md) links capture, compilation, promotion, checked execution and recovery to their implementation and tests. See the [design reference](docs/design.md) for the data model and execution phases.

## Background

Proof Run is a Hermes Agent Plugin Hackathon entry. Adjacent work includes workflow-record, crash-test, agent-replay/agentrec and Agent Behavioral Contracts. Proof Run combines demonstration capture, reviewable contracts, pre-execution checks and operator-controlled reuse.
