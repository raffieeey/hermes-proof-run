# Supported Hermes setup for workflows

Guarded repeats (`/proof start`, `/proof stop`, `/proof rebind`) use the standard plugin installation and your chosen Hermes agent model. Ordered workflow execution uses the checked-execution SDK supplied by the host patch in this release.

## Set up the workflow host

The supported source build is Hermes `67664e236ddcc45824b871d575677287599af152` with [`integration/hermes-checked-execution.patch`](../integration/hermes-checked-execution.patch). The patch preserves normal tool checks and approvals while providing trusted execution identity, final authorization and execution receipts to the plugin.

Start from a clean Hermes source installation. From your Proof Run clone:

```sh
PROOF_RUN_DIR="$(pwd)"
HERMES_SOURCE="${HERMES_HOME:-$HOME/.hermes}/hermes-agent"

test -d "$HERMES_SOURCE/.git"
test -z "$(git -C "$HERMES_SOURCE" status --porcelain)"
git -C "$HERMES_SOURCE" fetch origin 67664e236ddcc45824b871d575677287599af152
git -C "$HERMES_SOURCE" switch -c proof-run-v0.2.0 67664e236ddcc45824b871d575677287599af152
git -C "$HERMES_SOURCE" apply --check "$PROOF_RUN_DIR/integration/hermes-checked-execution.patch"
git -C "$HERMES_SOURCE" apply "$PROOF_RUN_DIR/integration/hermes-checked-execution.patch"

hermes plugins install raffieeey/hermes-proof-run/proof-run --ref v0.2.0 --enable
```

The clean-tree check preserves existing customizations rather than overwriting them. Keep an existing customized installation on its own branch and prepare a separate clean source checkout if needed. Hermes' standard source installer and managed-runtime process are documented in the [official installation guide](https://hermes-agent.nousresearch.com/docs/getting-started/installation).

The new CLI process reads the updated host code. For Telegram or another running gateway, finish active work and restart that gateway to load the host update:

```sh
hermes gateway restart
```

This release does not run that restart on your behalf. The supported workflow path uses in-process plugin loading.

## Check the complete workflow

From the Proof Run repository root, use Hermes' managed Python:

```sh
python3 proof_run_selftest.py --host-source "$HERMES_SOURCE" --out ./workflow-check.json
```

This registered-command example uses an isolated temporary profile and synthetic ticket store. It captures a teacher sequence, prepares and inspects the review bundle, promotes the workflow, runs fresh inputs, and reads back the target. It prints the run-store and target-store paths along with the result. The example's teacher is scripted, so the command needs no model quota. The cloud follower comparison is a separate [recorded benchmark](../benchmarks/cloud-following-pilot/README.md).

For embedding in an existing provisioned Python environment, the equivalent entry point is:

```sh
python3 scripts/run_actual_host_journey.py --host-source "$HERMES_SOURCE" --out ./workflow-check.json
```

The complete `REVIEW_TICKET` bundle in that script is the starting example for input bindings, policy, allowed resources and goal predicates. Adapt and inspect the bundle for your own tools before promotion.

## Supported behavior

- Standard Hermes approvals still apply to checked workflow effects.
- Rewritten effective arguments are checked before execution; synchronous receipts bind the actual call and result.
- Unknown outcomes use the original operation's read-only status lookup rather than repeating the mutation.
- Store workflow state in a private, trusted plugin-data root. [Storage and tool-coverage details](safety.md).
- A host without the checked-execution API returns `HOST_CAPABILITY_MISSING`. Use the supported build above and ensure the process loading the plugin has restarted.

The patch is the shipped SDK change for this supported build. Upstream merge status is separate from installing and using this version-pinned setup.
