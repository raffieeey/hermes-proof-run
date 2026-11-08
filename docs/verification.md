# Verification and migration

## What the prototype checks did not establish

The original `14/14` smoke assertions manually imported the plugin, supplied a fake context and invoked callbacks with invented tool outputs. They did not call a frontier model, call a cheap model, execute a real mutation handler through Hermes, or measure a model's task success. The original eight unit tests and the host's catalog validator passed despite material session, argument-matching, outcome and storage defects.

Before the hardening changes, the expanded real-host regression harness reproduced **14 failures among 20 checks**, with six controls passing. One failure was an uncaught command-validation exception. The failure count is a set of deliberately chosen regression cases, not an estimate of overall reliability or an independent defect count.

## Hardened verification result

- 34 core/storage tests passed, including the original eight.
- 24 real-host regression checks passed with zero failed checks and zero attempted socket connections.
- The real registry mutation counter was 1 after capture, 2 after an allowed replay, and remained 2 after a drift block.
- A mismatching replay result persisted `unproven`; reloading the plugin did not permit another mutation.
- The executed, path-sanitized observations are saved in [`proof-run/demo-output.txt`](../proof-run/demo-output.txt). These are synthetic local fixtures, not model calls.
- Separate out-of-process plugin-host startup could not be exercised in this isolated setup: its channel closed before the plugin loaded. That host/bootstrap path is not included in the passing in-process result.

## Current acceptance procedure

Run from the published source tree, not a plugin copy left in a live profile:

```sh
cd proof-run
PYTHONPATH=. python3 -m unittest discover -s tests -v
cd ..
python3 scripts/verify_host.py --host-source /path/to/hermes-agent
```

Use an existing provisioned Hermes interpreter for the host harness. It does not install dependencies. An unavailable host dependency is an environment blocker, not a reason to skip a check and report success. The tested source host is commit `a2db0efb2969d2527d27bf95a53066f5e064d9ad` on Linux, with in-process plugin loading. Tests of the optional separate plugin-host process must be reported separately; a manifest's `host_ready` result is only a static capability assessment.

The integration harness verifies actual loader/context registration, production command dispatch, normalized result hooks and registry handler call counts. It checks that a drift block prevents the synthetic mutation from running. It also exercises capture and stop isolation, argument-bound read observations, consumption before concurrent calls, known failure metadata, invalid commands, failed rebind preservation, corrupt-contract handling and a replay-evidence stop that survives reload.

The script refuses socket connections and uses temporary profile homes. No paid model calls or production actions are part of this test. Core/storage unit tests separately exercise type-correct JSON matching, safe persistence and adversarial paths. The CI workflow runs those standalone tests; it does not imply the real-host harness ran remotely.

## Schema-2 migration

Old prototype contracts inferred numeric ranges and combined fields independently. They are not trusted for reuse by version 0.2.0. Re-demonstrate a task with `/proof rebind <name>`, then `/proof stop`, rather than editing an old `schema_version` field to bypass migration.

Rebinding grants a temporary recording exemption only for that named contract in the recording session. The saved old version remains in place for other sessions until a valid replacement is published. Cancellation or a failed demonstration leaves it unchanged. The normal safety gate still applies to other bound contracts.

Do not assume a changed workflow is safe just because a previous task was safe. In particular, an action with no preceding observed reads has an exact-argument and result-evidence check, not independently inferred preconditions. This is a bounded guard, not a workflow executor or a security sandbox.

## Interpreting success

Passing these regression checks means their stated invariants hold on the tested source and host. It does not establish universal task correctness, transactional freshness against external changes, containment of other plugins or tools, universal secret detection, cross-platform support, or improved cheaper-model task completion. A real frontier-to-cheap-model comparison would need a separate controlled, user-approved evaluation.
