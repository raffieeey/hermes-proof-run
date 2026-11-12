# Verification and migration

Run the checks for each layer: saved contracts, the workflow runtime, the Hermes SDK and model following.

## 1. Historical exact-gate checks (34 / 24)

The original `14/14` smoke assertions manually imported the plugin, supplied a fake context and invoked callbacks with invented tool outputs. They did not call a frontier model, call a cheap model, execute a real mutation handler through Hermes, or measure a model's task success. The original eight unit tests and the host's catalog validator passed despite material session, argument-matching, outcome and storage defects.

Before the hardening changes, the expanded real-host regression harness reproduced **14 failures among 20 checks**, with six controls passing. One failure was an uncaught command-validation exception. The failure count is a set of deliberately chosen regression cases, not an estimate of overall reliability or an independent defect count.

Historical v0.1.0 exact-gate result:

- 34 core/storage tests passed, including the original eight.
- 24 real-host regression checks passed with zero failed checks and zero attempted socket connections on Hermes `a2db0efb2969d2527d27bf95a53066f5e064d9ad`.
- The real registry mutation counter was 1 after capture, 2 after an allowed replay, and remained 2 after a drift block.
- A mismatching replay result persisted `unproven`; reloading the plugin did not permit another mutation.
- The executed, path-sanitized observations are saved in [`proof-run/demo-output.txt`](../proof-run/demo-output.txt). These are synthetic local fixtures, not model calls.
- Separate out-of-process plugin-host startup could not be exercised in that isolated setup: its channel closed before the plugin loaded. That host/bootstrap path is not included in the passing in-process result.

Those counts do not cover the workflow runner, the checked-execution host patch, or the cloud following pilot.

## 2. Local 146-test suite

`proof-run/tests` now has 146 unittest methods (capture, compiler, commands, host adapter, follower, runner, workflow, ticket adapter, plus the original core/storage tests). Plugin runtime has no third-party dependencies. Schema checks in `test_follower.py` import `jsonschema`; CI installs `jsonschema>=4.18,<5` before the suite so those checks are mandatory there.

```sh
python3 -m pip install 'jsonschema>=4.18,<5'
cd proof-run
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

Passing GitHub Actions on this tag is the public record for that suite. Do not reuse the historical 34 as the current local count.

## 3. Patched host integration

Workflow `/proof run` needs the extra checked-execution Hermes change in [docs/host-integration.md](host-integration.md) and [integration/hermes-checked-execution.patch](../integration/hermes-checked-execution.patch). Missing support is `HOST_CAPABILITY_MISSING`. There is no raw `dispatch_tool` fallback.

The release patch applies to Hermes `67664e236ddcc45824b871d575677287599af152`. The rebased host passed 184 checked-execution, middleware, plugin and conformance tests, including the original nine independent scope controls, plus26 hosted-scope denial tests. The actual registered-command journey captured 6 receipts, prepared/inspected/promoted a revision, ran 2 inputs and independently read back both target queues. The standard-host verifier also passed 25 checks with 0 network attempts.

```sh
python3 scripts/verify_host.py --host-source /path/to/hermes-agent
```

Use an existing provisioned Hermes interpreter. It does not install dependencies. An unavailable host dependency is an environment blocker, not a reason to skip a check and report success. Probe `H10-workflow-command-surface` checks that `/proof capture` does not activate a schema-2 contract and that `/proof run` fails closed without inventing a dispatch fallback.

The script refuses socket connections and uses temporary profile homes. No paid model calls or production actions are part of this test. Installing the v0.2.0 plugin tag does not apply the host patch or change an already-installed plugin or running gateway.

`scripts/demo_runner.py` is a **scripted-operator** golden path with inspectable SQLite handles. It is not a frontier-model demonstration. `scripts/run_actual_host_journey.py` is the explicit actual-host opt-in (registered `/proof` command + public `execute_checked`). Unit follower tests mock HTTP.

## 4. Cloud following pilot (prompt/tool only)

[benchmarks/cloud-following-pilot/README.md](../benchmarks/cloud-following-pilot/README.md) records a two-case-per-arm prompt/tool microbenchmark. It is not Proof Run IR or runtime acceptance, not a statistical ranking, and not a cost-savings proof. There is no integrated cloud follower adapter.

Observed full completion within a five-call cap:

| Follower | Policy + tools only | Same + teacher guide |
|---|---:|---:|
| gpt-oss:20b | 0/2 | 1/2 |
| gemma4:31b | 1/2 | 2/2 |

All eight runs reserved the correct stock quantity. Three baseline failures missed finish despite a correct readback. Guided runs used 22.2% more tokens. The deterministic no-model control completed 2/2. Independent check: 39 HTTP 200 responses and eight traces replayed. The frontier teacher completed six tool operations. The guide was authored and reviewed from that trace, not compiled automatically.

Offline checks, no model calls:

```sh
python3 -m unittest discover -s benchmarks/cloud-following-pilot -p 'test_*.py' -v
python3 benchmarks/cloud-following-pilot/replay.py
```

CI runs both commands after the plugin unittest suite.

## Schema-2 migration

Old prototype contracts inferred numeric ranges and combined fields independently. They are not trusted for reuse by the hardened schema-2 gate. Re-demonstrate a task with `/proof rebind <name>`, then `/proof stop`, rather than editing an old `schema_version` field to bypass migration.

Rebinding grants a temporary recording exemption only for that named contract in the recording session. The saved old version remains in place for other sessions until a valid replacement is published. Cancellation or a failed demonstration leaves it unchanged. The normal safety gate still applies to other bound contracts.

Do not assume a changed workflow is safe just because a previous task was safe. In particular, an action with no preceding observed reads has an exact-argument and result-evidence check, not independently inferred preconditions. This is a bounded guard, not a workflow executor or a security sandbox.

Legacy contract storage refuses symlink targets. A pre-existing workflow `runner/` directory symlink is followed; keep that private plugin-data root trusted.

## Interpreting success

Passing these regression checks means their stated invariants hold on the tested source and host. It does not establish universal task correctness, transactional freshness against external changes, containment of other plugins or tools, universal secret detection, cross-platform support, or improved cheaper-model task completion. Model-following results and token costs are reported in the paired cloud pilot rather than inferred from interface tests.
