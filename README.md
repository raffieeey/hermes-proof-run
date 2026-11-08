# Proof Run

Turn a successful Hermes run into a **live execution contract**. Record the tool-call sequence and success evidence once, then gate later mutating calls on the recorded preconditions so a changed world **blocks** instead of silently improvising. No network, no credentials, no inference calls.

Hermes Agent Plugin Hackathon entry.

## The pitch

Hermes Agent is good, but a cheap model can't do what the frontier model does. Proof Run fixes that — record a proven run with the frontier model once, and the cheap model can repeat it safely forever.

## How to use

### Way 1: Pre-plan it

1. `/proof start <name>` — tell it you're about to record
2. Let the agent (frontier model) do the task
3. `/proof stop` — contract is created AND live immediately. Done.

### Way 2: Retroactive (you didn't plan ahead)

1. The frontier model already did something great in this session
2. `/proof learn <name>` — captures the last tool calls and creates a live contract immediately
3. Or `/proof learn <name> 15` to scope it to the last 15 calls

### After the contract is live

Switch to your cheap model. When it runs the same task, every mutating tool call gets checked against the contract's preconditions:

- **Pass → allow** — preconditions match, the call goes through
- **Drift → block** — with the exact fact and current value: `Contract my-task: precondition stock_level.available >= 3, current 1`
- **Unknown → block** — never treated as safe

If the world intentionally changed: `/proof rebind <name>` — record a fresh version.

### Other commands

- `/proof status` — see all contracts
- `/proof unbind <name>` — stop gating (contract is kept, just inactive)

## What it does not do

- No network, no credentials, no inference calls, no auto-approval.
- Only tools in the contract's action window are gated; reads and unrelated tools are untouched.
- Unknown preconditions block, never pass.
- The contract is the product: human-readable JSON, versioned, profile-scoped.

## Tests

```
cd proof-run && PYTHONPATH=. python3 -m unittest discover -s tests
# 8 tests: record→contract, failed-run rejection, operator review gate, allow/block/unknown,
# unrelated-tool passthrough, atomic rebind, persistence, concurrency.
```

## Prior-art boundary

Adjacent: workflow-record (browser record→skill), crash-test (fault injection), agent-replay/agentrec (trace replay), Agent Behavioral Contracts (formal contracts). Proof Run's distinction is the combined **capture → contract → gate → rebind** workflow as one installable Hermes plugin.
