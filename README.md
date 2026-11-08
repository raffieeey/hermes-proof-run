# Proof Run

Turn a successful Hermes run into a **live execution contract**. Record the tool-call sequence and success evidence once, then gate later mutating calls on the recorded preconditions so a changed world **blocks** instead of silently improvising. Operator-reviewed contracts, rebind on intentional drift. No network, no credentials, no inference calls.

Hermes Agent Plugin Hackathon entry.

## What it does

1. `/proof start <name>` — begin capture for this session's tools.
2. Do the task once. `/proof stop` drafts a reviewable contract: intent, mutating tools, allowed arg patterns, preconditions (from observed read facts), success evidence (from mutating results).
3. `/proof bind <name>` — activate the contract (operator gesture, required).
4. Later runs: any mutating tool in the contract's action window is checked against current preconditions. **Pass → allow; drift → block with the exact fact and current value; unknown → block as unsafe.** Unrelated tools are untouched.
5. `/proof rebind <name>` — after an intentional change, record a fresh success and atomically supersede the old version.

## Demo (≤90s)

Synthetic stock-transfer task: contract saves with `stock_level.available >= 3`. Lower the threshold. Next `stock_move` call is blocked **before execution** with `Contract move-stock: precondition stock_level.available >= 3, current 1`. Re-demonstrate, rebind, run proceeds.

## What it does not do

- No network, no credentials, no inference calls, no auto-approval.
- Only tools in the contract's action window are gated; reads and unrelated tools are untouched.
- Unknown preconditions block, never pass.
- The contract is the product: human-readable JSON, versioned, profile-scoped under `plugin_data_dir("proof-run")`.

## Tests

```
cd plugins/proof-run && PYTHONPATH=. python3 -m unittest discover -s tests
# 8 tests: record→contract, failed-run rejection, operator review gate, allow/block/unknown,
# unrelated-tool passthrough, atomic rebind, persistence, concurrency.
```

## Prior-art boundary

Adjacent: workflow-record (browser record→skill), crash-test (fault injection), agent-replay/agentrec (trace replay), Agent Behavioral Contracts (formal contracts). Proof Run's distinction is the combined **capture → contract → gate → rebind** workflow as one installable Hermes plugin.
