# Proof Run

**Help cheaper models behave more like frontier models on repeatable tasks.**

Hermes Agent Plugin Hackathon entry. No model calls or network requests are made by the plugin.

## The pitch

Let a frontier model do the task once. Proof Run captures the checks and actions from that successful run. Then use a cheaper model for the repeats, with those guardrails in place.

If the agent skips a recorded check or changes a protected action, Proof Run stops that call before it runs. When a change is intentional, demonstrate the new version and update the contract.

## Install

```sh
hermes plugins install raffieeey/hermes-proof-run/proof-run --enable
```

The plugin lives in the repository's `proof-run/` subdirectory. Review third-party plugin code before enabling it. A running gateway may need a safe restart to load an updated plugin; updating this Git repository does not replace code already loaded in a running process.

## Use

1. Run `/proof start deploy-api` in the session that will demonstrate the task.
2. Let the agent perform the task, including its read checks and mutations.
3. Run `/proof stop`. A successful capture becomes a live, profile-scoped contract in one atomic write. There is no separate `/proof bind` command.
4. On reuse, perform the same reads again before the covered mutation. The complete JSON argument object must match a demonstrated action, with the same JSON types. Missing, changed, expired or consumed observations block.
5. If a change is intentional, run `/proof rebind deploy-api`, demonstrate the changed task, then `/proof stop`. Only that contract, only in the recording session, is exempt during re-demonstration. Other sessions and contracts remain gated.

Other commands:

- `/proof learn deploy-api 15`: capture the last 15 tool calls from this session. Omit the count to use its current rolling buffer of up to 100 calls. Counts must be between 1 and 100. Inspect the scope carefully: a recent buffer can contain more than one task.
- `/proof status`: list versions, bound state and evidence status.
- `/proof cancel`: discard this session's in-progress capture without changing a saved contract.
- `/proof unbind deploy-api`: explicitly stop enforcing that saved contract.

Existing names require `rebind`, not an accidental overwrite through `start` or `learn`. Failed captures leave the previous contract unchanged. Old contract versions are retained in the private history directory. Legacy files containing recognized secrets are refused and need manual removal or sanitization outside the guarded agent before a fresh capture. Version-1 contracts from the original prototype require re-recording; their inferred ranges are not silently trusted.

## Verify

Core and storage tests need only Python's standard library on a supported POSIX host:

```sh
cd proof-run
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

For real-host integration, use an already-provisioned Hermes interpreter and checkout. From the repository root:

```sh
python3 scripts/verify_host.py --host-source /path/to/hermes-agent
```

This uses the actual plugin loader/context, slash-command dispatcher, JSON result normalization, pre/post hooks and registry. Its synthetic mutation handler has a real call counter, so a drift block must prevent execution, not merely return a plausible message. It also probes session isolation, read freshness, concurrent consumption, failed capture/rebind, evidence failure across reload, command validation and corrupt contracts. The script refuses socket connections and writes only isolated temporary homes. It exits nonzero for failed checks. It is not a live deployment or a model benchmark.

Early prototype work included real `glm-5.3` and `glm-5.3-flash` requests on the same tasks, alongside scripted start/stop/learn/rebind and deviation-blocking tests.

See [verification and migration notes](docs/verification.md).

## Prior-art boundary

Adjacent work includes workflow-record (record-to-skill), crash-test (fault injection), agent-replay/agentrec (trace replay) and Agent Behavioral Contracts. This entry combines local capture, inspectable contracts, pre-execution checks and operator re-demonstration. It is not a claim to have invented recording or guardrails.

## Safety contract and limits

- **Exact actions, not guessed ranges.** No numeric interpolation, recombination of independently observed fields, or additional `force` argument is inferred as safe. Nested objects, arrays, null and booleans retain their JSON meaning.
- **Bound observations.** Read facts are matched to their read arguments and session. Only reads before the action contribute to that action's preconditions. Numeric and boolean facts use equality by default; one example cannot establish a general safety threshold.
- **Consumable evidence.** Observations expire after 60 seconds and are consumed before dispatch of a mutation, including competing calls. Refresh reads before each reuse. A read is not a transaction: external state can still change between a read and the mutation. Use a transactional tool for that guarantee.
- **Known failures are not proof.** Host error/cancellation metadata, error JSON and nonzero command exits reject a capture. Missing mutation evidence is rejected. Recorded result fields are checked on guarded replays; a mismatch persists `unproven` and blocks subsequent covered calls, including after reload. This cannot undo the mutation that produced the mismatch.
- **Scope is deliberately narrow.** Known read-only tools and unrelated tools are not generally sandboxed. New tool names, alternate execution mechanisms, external side effects and adversarial models are not contained by this plugin. Multiple bound contracts covering the same tool must all pass and can intentionally conflict. A step with no observed reads provides an argument/evidence check, not an independent world-state guarantee.
- **Fail closed on damaged protection.** An invalid/unreadable contract stops mutating calls rather than silently disappearing. Repair it outside the guarded agent or re-record a valid contract as appropriate.
- **Private local persistence.** Contracts are profile-scoped under `plugin_data_dir("proof-run")/contracts/`, with private directories, mode-0600 files, serialized atomic updates and retained versions. Symlinked storage targets are refused. Known credential patterns and sensitive named fields are refused, not silently rewritten into a different allowed action. Secret detection is heuristic, not a guarantee that arbitrary credentials or personal data can be recognized. Do not record sensitive tasks.
- **Host boundary.** The tested in-process host is Hermes commit `a2db0efb2969d2527d27bf95a53066f5e064d9ad` on Linux. Durable locking uses POSIX file locking. Other operating systems or host versions are not certified by these checks. The host must deliver consistent session and tool-call identity. Hermes' normal approvals remain in force.

## What this is, and is not

A cheaper model can use the same guard as a frontier model. That is not model training or automatic workflow replay: Proof Run does not teach the model a plan, choose the next tool, enforce an entire workflow's ordering, or establish that the task is correct. The operator supplies the task and decides whether a demonstration is worth preserving. There is no measured frontier-to-cheap-model quality or cost improvement claimed here.

**Capture a demonstrated action, then check it before reuse.** Proof Run is a local Hermes plugin that checks tool arguments and observed preconditions before a covered mutating call runs. A mismatching result marks the contract **unproven** and stops subsequent covered calls until the operator re-demonstrates it.

Historical `14/14` prototype assertions used a fake plugin context and did not prove real-host integration or model learning.
