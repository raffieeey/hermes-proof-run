# Changelog

## [Unreleased]

### Fixed

- The guide states every step of the procedure, not only the current one. `pre_llm_call` fires once per user turn, so the previous text left later steps as tool names; a run now renders each step's tool and arguments, marks the current step `NOW`, and lists the reads to perform before each step in recorded order.
- A recorded number binds to the read that produced it. Candidates are kept per `(tool`, read arguments, `field)` rather than per value, so a demonstration where a request quantity equals the available stock resolves the quantity from the request instead of baking the teacher's constant. When several reads hold the same value, the field name and the action's other arguments pick the source; if that still leaves more than one, the save refuses with a readable error. A stable field returned identically by two reads (a SKU) binds to the newest read.
- A live read older than 300 seconds no longer satisfies a dynamic slot; the gate blocks with the same `fresh read data is missing` message it uses for a missing read.
- The recorder keeps an ordered read history for saved procedures. A read repeated after another read is listed again in the guide, so a follower can see that the earlier value was stale; the strict contract keeps its latest-read-wins preconditions, and only the newest read of a key carries pinnable facts.
- The guide now says explicitly to continue with the next step after a confirmed success, even when the text still marks the earlier step `NOW`; it is refreshed only on the next user turn.
- The A/B record stores the complete guide the follower received, not a 2,400-character preview.

### Verification

- The post-fix A/B run (12 tasks, three runs per arm, `deepseek-v4.1-flash` follower, `gpt-6-luna` teacher) put guided at 36/36 and unguided at 35/36 (+2.8 points, 95% interval -7.1 to +14.2), with 12/12 procedures saved and no gate blocks. The set is at ceiling, so this does not demonstrate superiority. Record: `benchmarks/live-guided/results-2026-11-17-post-fix.json`.

## [0.3.2] - 2026-11-17

### Changed

- `plugin.yaml` declares `requires_hermes: ">=0.21.0"`, the same floor the catalog entry carries. Catalog rule 14 requires both, because older Hermes versions enforce only the manifest. No runtime change.

## [0.3.1] - 2026-11-16

### Changed

- Guided runs write an atomic per-session marker under the existing contracts storage root. A plugin reload re-arms an unexpired marker as a stopped-and-held run; its action tools remain blocked until `/proof cancel`, and expired markers are discarded after 24 hours. No marker that cannot be re-armed is deleted; it fails closed and is kept. A marker that is valid JSON with an invalid field, including an unsupported schema version, holds its own session only when it names one readably and its filename is the one that session's marker would use. The filename is checked before the schema, so a marker whose filename and session disagree holds every session whatever schema it claims; so do a marker that is not JSON, one that names no usable session, an oversize, linked or unreadable marker file, and an unreadable marker directory. Effectful tools stay blocked until `/proof cancel`, read-only tools stay available, and `/proof status` reports the hold. `/proof cancel` does not delete a marker file that holds every session, even from the session the file is named for, and its reply says how many unusable marker files remain and that they hold every session again after the next reload until repaired or removed.
- A live guided run's structure is validated before the "tool is not in the plan" shortcut. If the run is not a mapping, its plan, steps or tool list is missing, empty or mistyped, its step index is outside the plan, or the plan's tool list disagrees with its steps, every effectful tool in that session is blocked until `/proof cancel`; read-only tools remain available. `/proof status` reports the unreadable run.

### Verification

- At release-preparation commit `9de5f4e` (2026-11-16), 187 plugin behaviour tests, 10 benchmark tests, and offline replay of 39 responses across 8 trials passed. The suite has since grown to 221 tests: 216 plugin behaviour tests and 5 catalog-folder checks. The real-host verifier passed 25/25 probes with zero network attempts; the detailed receipt is `docs/host-verification-2026-11-16.json`.

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
