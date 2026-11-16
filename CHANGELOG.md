# Changelog

## [0.3.1] - 2026-11-16

### Changed

- Guided runs write an atomic per-session marker under the existing contracts storage root. A plugin reload re-arms an unexpired marker as a stopped-and-held run; its action tools remain blocked until `/proof cancel`, and expired markers are discarded after 24 hours. A marker that is not JSON is discarded; any other marker that cannot be re-armed fails closed and is kept. A valid marker with an unsupported schema version holds its own session; an unreadable marker directory or marker, or a marker that cannot be validated, holds every session. Effectful tools stay blocked until `/proof cancel`, read-only tools stay available, and `/proof status` reports the hold.
- A live guided run's structure is validated before the "tool is not in the plan" shortcut. If the run is not a mapping, its plan, steps or tool list is missing, empty or mistyped, its step index is outside the plan, or the plan's tool list disagrees with its steps, every effectful tool in that session is blocked until `/proof cancel`; read-only tools remain available. `/proof status` reports the unreadable run.

### Verification

- At release-preparation commit `9de5f4e` (2026-11-16), 187 plugin tests, 10 benchmark tests, and offline replay of 39 responses across 8 trials passed. The current tree adds 5 catalog-folder tests and has 192 plugin tests. The real-host verifier passed 25/25 probes with zero network attempts; the detailed receipt is `docs/host-verification-2026-11-16.json`.

## [0.3.0] - 2026-11-14

Guided runs: record a task once, save it as a procedure with named inputs, and run it with a cheaper model that is told the steps and checked step by step.

### Added

- `/proof save [name=value ...]` turns the session's recording into an unbound schema-2 contract with a `procedure` block. Inputs are validated before anything is written; a refused save keeps the recording active. A declared input that no step can use because a fresh read supplies the same value is refused rather than stored as a dead input.
- `/proof run <name> [name=value ...]` binds new input values and starts one run per session. A name with no procedure falls through to the existing workflow `run`.
- `/proof inspect <name>` lists steps, inputs and pinnable read facts; `/proof pin` and `/proof unpin` make a recorded read fact a required precondition. A damaged procedure refuses inspection, pinning and running with a readable error.
- `pre_llm_call` returns the guide text for the run's session only. `pre_tool_call` checks the run before the strict guard, `post_tool_call` reports step outcomes, and `on_session_finalize` and `/proof cancel` clear the run.
- `tests/test_guided_acceptance.py` (19 tests) is the frozen contract for guided runs; `tests/test_guided_persistence.py` and `tests/test_guided_transfer.py` add thirteen persistence, live-binding, hook-order, input-templating, validation and malformed-command regression tests. The plugin suite is 178 tests.

### Changed

- `/proof status` adds `procedure` to a procedure's line, plus a `step i of n` line for an active run.
- Plugin manifest is version 0.3.0 and lists `pre_llm_call`.

### Compatibility and validation

- Strict guard mode and the checked-execution workflow engine are unchanged. `/proof stop`, `learn`, `rebind`, `unbind` and every workflow command keep their v0.2.0 behaviour.
- Guided runs need no host patch, make no model or network call, and were process-local in v0.3.0. The Unreleased durability change re-arms runs as stopped and held after reload.
- The retained aggregate in `benchmarks/live-guided/results.json` reports one glm-5.3 teacher run, four glm-5.3-flash guided runs on a new request, and three unguided baseline runs passing. The harness and per-run transcripts are not retained, so those live execution details are reported results rather than independently reproducible evidence. This is a small single-task sample, not a performance or savings claim.

## 0.2.0 - 2026-11-12

Teach a procedure with a frontier model, then reuse it with a cheaper follower under reviewed guardrails. There is no student parameter-count requirement.

This tag does not change an already-installed plugin or a running gateway. The upstream catalog entry still targets v0.1.0; no catalog repin for v0.2.0 was made.

### Added

- Optional teach / capture / prepare / inspect / promote / run path beside the legacy exact gate.
- Optional local Ollama follower over loopback HTTP. No silent current-model or cloud fallback. No integrated cloud follower adapter.
- Extra Hermes checked-execution patch at `integration/hermes-checked-execution.patch`, documented in `docs/host-integration.md`. Missing host support fails closed with `HOST_CAPABILITY_MISSING` and never falls back to raw `dispatch_tool`.
- Prompt/tool following pilot notes and an offline replay at `benchmarks/cloud-following-pilot/`. That pilot is not Proof Run IR or runtime acceptance and is not a cost-savings proof.
- Local unittest suite expanded to 146 methods. CI installs `jsonschema>=4.18,<5` before the suite. Plugin runtime still has no third-party dependencies.

### Changed

- Public author identity is `raffieeey`. License remains MIT.

### Compatibility and validation

- The version-pinned host target is Hermes `67664e236ddcc45824b871d575677287599af152` with the bundled checked-execution patch. Release-era notes report 184 host/plugin/conformance tests, 26 hosted-scope denial tests and a registered-command journey, but the repository does not retain raw test output for those results; see [verification notes](docs/verification.md).
- Operator demos (`scripts/demo_runner.py` and the default mock path) are scripted teachers, not frontier demonstrations.
- Legacy contract storage refuses symlink targets. A pre-existing workflow `runner/` directory symlink is followed; keep that private plugin-data root trusted. One is not the other.
- Cloud pilot: two cases per arm. Full completion within a five-call cap moved gpt-oss:20b from 0/2 to 1/2 and gemma4:31b from 1/2 to 2/2. All eight runs reserved the correct stock quantity. Three baseline failures missed `finish_task` despite a correct readback. The guided runs used 22.2% more tokens. The deterministic no-model control completed 2/2. No dollar savings or general ranking.

## 0.1.0

Public exact-gate plugin: capture a demonstration, check exact arguments and session-bound observations, and stop on unproven replay evidence. No model or network calls.
