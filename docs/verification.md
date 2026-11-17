# Verification and migration

Run the checks for each layer: saved contracts, the workflow runtime, the Hermes SDK and model following.

## Current verification (fail-closed fix tree, 2026-11-16)

These commands were run from the repository root. The explicit `cd ..` returns to the root after the plugin suite. `python3` resolved to Hermes-managed Python 3.14.9 at `/home/afifah/.hermes/installs/9e5946996ecbd658/environments/cdbfee10e1b148a1b9d9dee2f798ce48/venv/bin/python3`.

```sh
cd proof-run && PYTHONPATH=. python3 -m unittest discover -s tests -q
cd ..
python3 -m unittest discover -s benchmarks/cloud-following-pilot -p 'test_*.py' -q
python3 benchmarks/cloud-following-pilot/replay.py
python3 -m compileall -q proof-run scripts
git diff --check
python3 scripts/verify_host.py --host-source /home/afifah/.hermes/hermes-agent --output "$HOME/proof-run-host-verification.json"
```

Results: 217 tests passed: 212 plugin behaviour tests and 5 catalog-folder checks. For history, the v0.3.1 preparation commit `9de5f4e` had 187 tests (all plugin behaviour) and the audit tree had 192 (187 plugin behaviour plus the 5 catalog-folder checks). The benchmark suite passed 10 tests; offline replay verified 39 responses across 8 identical target-state replays. `compileall` and `git diff --check` passed. The host verifier ran in-process against host checkout `e96221fa73d2dac7ba9f3bc5a8b866b3d0fc188e` and returned `total=25`, `passed=25`, `failed=0`, `network_attempts=0`. The host checkout had no tracked modifications; untracked files were present. The checked-in receipt [host-verification-2026-11-16.json](host-verification-2026-11-16.json) was committed before the fail-closed fixes (`4742fd9`); the re-run on this tree passed the same 25 probes and its output was not committed.

Coverage is distinct by layer: the plugin and benchmark suites are local automated tests; `scripts/verify_host.py` exercises the real Hermes loader, plugin commands/hooks and synthetic tool paths in temporary profile homes. Its audit hook blocks Python `socket.connect` attempts; the receipt records zero attempts. It is not the patched checked-execution workflow journey or a live gateway test. No model call, production action, or live Telegram reply was tested; no Telegram reply is claimed.

## 1. Historical exact-gate checks (34 / 24)

The project history reports that an early 14/14 smoke run manually imported the plugin, supplied a fake context and invoked callbacks with invented tool outputs. No run record for that smoke result is retained here, so its count is an unverified historical report, not current test evidence. The original eight unit tests and host catalog validator are also described as passing before material session, argument-matching, outcome and storage defects were found.

A later pre-hardening report records **14 failures among 20 checks**, with six controls passing. The per-case run output is not retained, so the failure count is also an unverified historical report, not an estimate of reliability or an independently established defect count.

Historical v0.1.0 exact-gate result:

- 34 core/storage tests passed, including the original eight.
- 24 real-host regression checks passed with zero failed checks and zero attempted socket connections on Hermes `a2db0efb2969d2527d27bf95a53066f5e064d9ad`.
- The real registry mutation counter was 1 after capture, 2 after an allowed replay, and remained 2 after a drift block.
- A mismatching replay result persisted `unproven`; reloading the plugin did not permit another mutation.
- The executed, path-sanitized observations are saved in [`proof-run/demo-output.txt`](../proof-run/demo-output.txt). These are synthetic local fixtures, not model calls.
- The stored receipt is in-process only. A prior note reports that a separate out-of-process startup ended before the plugin loaded, but no run output for that failure is retained.

Those counts do not cover the workflow runner, the checked-execution host patch, or the cloud following pilot.

## 2. Historical local 146-test suite

At the v0.2.0 baseline, `proof-run/tests` had 146 unittest methods (capture, compiler, commands, host adapter, follower, runner, workflow, ticket adapter, plus the original core/storage tests). Plugin runtime has no third-party dependencies. Schema checks in `test_follower.py` import `jsonschema`; CI installs `jsonschema>=4.18,<5` before the suite so those checks are mandatory there.

```sh
python3 -m pip install 'jsonschema>=4.18,<5'
cd proof-run
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

The v0.3.1 preparation commit `9de5f4e` had 187 passing plugin behaviour tests. Five catalog-folder checks brought the suite to 192, and the later fidelity and fail-closed fixes bring it to 218 tests: 213 plugin behaviour tests and 5 catalog-folder checks; see the current verification section above. Passing GitHub Actions on a tag is the public record for that release. Do not reuse the historical 34 as a current local count.

## 3. Patched host integration

Workflow `/proof run` needs the extra checked-execution Hermes change in [docs/host-integration.md](host-integration.md) and [integration/hermes-checked-execution.patch](../integration/hermes-checked-execution.patch). Missing support is `HOST_CAPABILITY_MISSING`. There is no raw `dispatch_tool` fallback.

The patch target is Hermes `67664e236ddcc45824b871d575677287599af152`. The current retained host receipt above is for a stock host and does not test the patched workflow journey. Release-era notes reported 184 host/plugin/conformance tests, 26 hosted-scope denial tests and a successful registered-command journey, but raw test output for those results is not retained here; treat those historical results as unverified.

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


## Schema-2 migration

Old prototype contracts inferred numeric ranges and combined fields independently. They are not trusted for reuse by the hardened schema-2 gate. Re-demonstrate a task with `/proof rebind <name>`, then `/proof stop`, rather than editing an old `schema_version` field to bypass migration.

Rebinding grants a temporary recording exemption only for that named contract in the recording session. The saved old version remains in place for other sessions until a valid replacement is published. Cancellation or a failed demonstration leaves it unchanged. The normal safety gate still applies to other bound contracts.

Do not assume a changed workflow is safe just because a previous task was safe. In particular, an action with no preceding observed reads has an exact-argument and result-evidence check, not independently inferred preconditions. This is a bounded guard, not a workflow executor or a security sandbox.

Legacy contract storage refuses symlink targets. A pre-existing workflow `runner/` directory symlink is followed; keep that private plugin-data root trusted.

## Interpreting success

Passing these regression checks means their stated invariants hold on the tested source and host. It does not establish universal task correctness, transactional freshness against external changes, containment of other plugins or tools, universal secret detection, cross-platform support, or improved cheaper-model task completion. Model-following results and token costs are reported in the paired cloud pilot rather than inferred from interface tests.
