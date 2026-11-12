# TDD implementation map

Proof Run v0.2.0 implements the execution loop specified in the reviewed execution TDD revision 0.3. The design revision and plugin release version are separate identifiers.

The core split is unchanged: the teacher demonstrates a procedure, an operator reviews its scope, a data-only workflow carries the procedure, and a deterministic runtime executes it. A follower handles bounded decisions. Model choice is based on task quality and execution cost, not a fixed parameter count.

| TDD area | v0.2.0 implementation | Verification |
|---|---|---|
| §3 architecture and §4 command journey | [`capture.py`](../proof-run/proof_run/capture.py), [`commands.py`](../proof-run/proof_run/commands.py): capture, prepare, inspect, promote, run, show, cancel and resume | Capture/compiler/command tests; registered-command workflow example |
| §5 demonstration, workflow and run artifacts | [`capture.py`](../proof-run/proof_run/capture.py), [`workflow.py`](../proof-run/proof_run/workflow.py), [`store.py`](../proof-run/proof_run/store.py) | Complete receipt joins, immutable revisions, separate assurance fields and stored-run tests |
| §6 typed live binding and finite checks | [`compiler.py`](../proof-run/proof_run/compiler.py), [`workflow.py`](../proof-run/proof_run/workflow.py) | Input validation, identity checks, reference/predicate validation and effect-intent bindings |
| §7 bounded decision API | [`follower.py`](../proof-run/proof_run/follower.py), [`engine.py`](../proof-run/proof_run/engine.py) | Closed decision schema, authorized IDs, abstention and explicit route/resource tests |
| §8 checked host boundary | [`hostport.py`](../proof-run/proof_run/hostport.py), [shipped Hermes SDK patch](../integration/hermes-checked-execution.patch) | Rewritten arguments, approvals, bound session identity, receipts and nine independent scope controls |
| §9 scheduling and durable state | [`engine.py`](../proof-run/proof_run/engine.py), [`store.py`](../proof-run/proof_run/store.py) | SQLite intent/reservation, crash/fence, cancellation, unknown-outcome status lookup and recovery tests |
| §10 ticket example | [`example_ticket.py`](../proof-run/proof_run/example_ticket.py), [`run_actual_host_journey.py`](../scripts/run_actual_host_journey.py) | Real checked SDK calls, separate source/routing revisions and persisted target readback |
| §11 compatibility | Existing [`core.py`](../proof-run/proof_run/core.py) exact gates plus the separate workflow namespace | 146-plugin-test suite and 25 stock-host checks |
| §12 approvals/failures and §13 privacy/trust | Host final authorization, credential refusal, private storage, explicit effect and model budgets | Failure/replay tests; [supported setup](host-integration.md) and [storage reference](safety.md) |
| §14 evaluation | [Cloud following pilot](../benchmarks/cloud-following-pilot/README.md), actual SDK example and offline replay | Each reports its own measured layer: model following, deterministic runtime behavior or interface conformance |

## Design responsibility

The runtime owns the program counter, live bindings, authority, effects and recovery. The follower returns a value within the reviewed schema; it does not grant permissions or rewrite the workflow. Promotion makes the reviewed revision reusable, so ordinary repeated runs do not repeat the whole review ceremony.

The paired cloud pilot adds measured guide-following evidence to the TDD's evaluation work. Its teacher guide is prose authored from a real frontier tool sequence. The product compiler and deterministic execution path are exercised separately through the registered-command SDK example. [Verification commands and records](verification.md).
