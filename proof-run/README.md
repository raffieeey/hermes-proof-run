# Proof Run plugin

v0.2.0. Capture demonstrated actions and check their arguments, observed preconditions and replay evidence at Hermes' tool boundary. Optionally teach a **trace-only** demonstration, compile a reviewed workflow, promote it, and `/proof run` it through checked execution. Legacy start/stop still activate an exact gate immediately; capture finish does not.

The pitch is unchanged: a frontier teacher sets the procedure; a cheaper follower reuses it. There is no student size requirement.

Install from the v0.2.0 tag as shown in the [repository README](../README.md). That pin does not change an already-installed plugin, apply the host patch, or repin the Hermes catalog. Workflow execution needs the extra checked-execution host change in [docs/host-integration.md](../docs/host-integration.md) and [integration/hermes-checked-execution.patch](../integration/hermes-checked-execution.patch). Missing support fails closed.

The local follower speaks loopback Ollama HTTP only. There is no integrated cloud adapter. The prompt/tool following notes in [benchmarks/cloud-following-pilot/README.md](../benchmarks/cloud-following-pilot/README.md) are not IR/runtime acceptance.

`scripts/demo_runner.py` is a **scripted** teacher. It is not a frontier demonstration.

## Legacy commands

- `/proof start <name> [intent]`
- `/proof stop`
- `/proof cancel`
- `/proof learn <name> [N]`, with 1 <= N <= 100
- `/proof rebind <name>`
- `/proof status`
- `/proof unbind <name>`

## Workflow commands

- `/proof capture start <name> [intent]`
- `/proof capture finish`
- `/proof capture cancel`
- `/proof prepare <name> [json]`
- `/proof inspect <name>`
- `/proof promote <name> <revision>`
- `/proof run <name> [json]`
- `/proof runs`
- `/proof show <run-id>`
- `/proof cancel-run <run-id>`
- `/proof resume <run-id>`

Capture and recording exemptions are session-local. Saved gates are profile-wide. Rebind keeps the prior saved contract active for other sessions while the operator demonstrates its replacement. A failed replacement does not remove the old contract.

From this directory, schema tests need `jsonschema>=4.18,<5`. The plugin runtime does not:

```sh
python3 -m pip install 'jsonschema>=4.18,<5'
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

`scripts/verify_host.py` exercises a real, pre-provisioned Hermes host. `scripts/run_actual_host_journey.py` is the explicit actual-host opt-in using registered `/proof` commands and `execute_checked`. This plugin does not train models. Exact demonstrated schema-2 actions remain exact; parameterized reuse requires an explicit reviewed workflow.
