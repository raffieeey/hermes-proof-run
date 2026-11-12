# Changelog

## 0.2.0 - 2026-11-12

Teach a procedure with a frontier model, then reuse it with a cheaper follower under reviewed guardrails. There is no student parameter-count requirement.

This tag does not change an already-installed plugin or a running gateway. There is no catalog repin and no upstream Hermes pull request.

### Added

- Optional teach / capture / prepare / inspect / promote / run path beside the legacy exact gate.
- Optional local Ollama follower over loopback HTTP. No silent current-model or cloud fallback. No integrated cloud follower adapter.
- Extra Hermes checked-execution patch at `integration/hermes-checked-execution.patch`, documented in `docs/host-integration.md`. Missing host support fails closed with `HOST_CAPABILITY_MISSING` and never falls back to raw `dispatch_tool`.
- Prompt/tool following pilot notes and an offline replay at `benchmarks/cloud-following-pilot/`. That pilot is not Proof Run IR or runtime acceptance and is not a cost-savings proof.
- Local unittest suite expanded to 146 methods. CI installs `jsonschema>=4.18,<5` before the suite. Plugin runtime still has no third-party dependencies.

### Changed

- Public author identity is `raffieeey`. License remains MIT.

### Compatibility and validation

- The supported host build is Hermes `67664e236ddcc45824b871d575677287599af152` with the bundled checked-execution patch. Verification passed 184 host/plugin/conformance tests, 26 hosted-scope denial tests and the full registered-command workflow example.
- Operator demos (`scripts/demo_runner.py` and the default mock path) are scripted teachers, not frontier demonstrations.
- Legacy contract storage refuses symlink targets. A pre-existing workflow `runner/` directory symlink is followed; keep that private plugin-data root trusted. One is not the other.
- Cloud pilot: two cases per arm. Full completion within a five-call cap moved gpt-oss:20b from 0/2 to 1/2 and gemma4:31b from 1/2 to 2/2. All eight runs reserved the correct stock quantity. Three baseline failures missed `finish_task` despite a correct readback. The guided runs used 22.2% more tokens. The deterministic no-model control completed 2/2. No dollar savings or general ranking.

## 0.1.0

Public exact-gate plugin: capture a demonstration, check exact arguments and session-bound observations, and stop on unproven replay evidence. No model or network calls.
