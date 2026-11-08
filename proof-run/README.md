# Proof Run plugin

Capture demonstrated actions and check their arguments, observed preconditions and replay evidence at Hermes' tool boundary. No model calls or network requests are made by the plugin.

The canonical installation, commands, safety limits and verification instructions are in the [repository README](../README.md). In particular, `/proof stop` activates a successful capture immediately; there is no separate `/proof bind` step.

## Commands

- `/proof start <name> [intent]`
- `/proof stop`
- `/proof cancel`
- `/proof learn <name> [N]`, with 1 <= N <= 100
- `/proof rebind <name>`
- `/proof status`
- `/proof unbind <name>`

Capture and recording exemptions are session-local. Saved gates are profile-wide. Rebind keeps the prior saved contract active for other sessions while the operator demonstrates its replacement. A failed replacement does not remove the old contract.

From this directory, run the standalone regression suite:

```sh
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

The repository's `scripts/verify_host.py` separately exercises a real, pre-provisioned Hermes host. This plugin does not train models or automatically replay a workflow. Only exact demonstrated actions are inferred, not safe numeric ranges, whole-workflow ordering or new parameter combinations.
