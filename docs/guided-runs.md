# Guided runs

Status: implemented in v0.3.0. The executable contract is `proof-run/tests/test_guided_acceptance.py`; the implementation is `proof_run/guided.py` and `proof_run/bindings.py`.

## Problem

Proof Run promises: show a frontier model the way once, reuse the procedure with a cheaper model. On a standard Hermes host with Hermes' own tools, v0.2.0 cannot do that:

- `/proof stop` turns the recording into an always-on gate keyed by tool name. One recorded `terminal` command blocks every other `terminal` call in every session.
- Arguments must match exactly, so a new version number or ticket ID is blocked.
- Every result field is success evidence and every field of every prior read is a precondition. A timestamp, a duration or a changed search result marks the contract unproven or blocks it.
- The follower is never told the procedure. The gate only says no.
- Workflows fix some of this but need the host patch and accept only the built-in example tools.

## What a guided run is

A saved **procedure** is the recorded action steps, with chosen values turned into **inputs**. A **run** binds new input values in one session, tells the follower the steps, and checks each action as it is proposed. Nothing is enforced outside a run.

```text
/proof start deploy ship the api
  (the frontier model does the task once)
/proof save version=2.1.0
/proof run deploy version=2.2.0
  (send any message; the follower model is guided and checked step by step)
```

It needs only the standard plugin surface: the `/proof` command and the `pre_tool_call`, `post_tool_call`, `pre_llm_call` and `on_session_finalize` hooks. No host patch, no model call by the plugin, no network.

## Commands

| Command | Behaviour |
| --- | --- |
| `/proof save [name=value ...]` | Ends the session's recording and saves it as a procedure. Each `name=value` marks a demonstrated value as an input. Does not activate any gate. |
| `/proof run <name> [name=value ...]` | Starts a run of a saved procedure in this session. If no procedure has that name, the existing workflow `run` handles it as before. |
| `/proof inspect <name>` | For a procedure: shows steps, inputs with demonstrated values, and the read facts that can be pinned. Otherwise the existing workflow `inspect`. |
| `/proof pin <name> <tool>.<field>` / `/proof unpin ...` | Makes a recorded read fact a required precondition, or removes it. |
| `/proof cancel` | Also ends the session's run and releases its tools. |
| `/proof status` | Existing lines unchanged. A procedure's line also says `procedure`. An active run adds a line with its name and `step i of n`. |

`/proof start`, `stop`, `learn`, `rebind`, `unbind` and every workflow command keep their v0.2.0 behaviour. `/proof stop` still creates the strict always-on guard.

Arguments to `save` and `run` are split with shell-style quoting, so `msg="fix bug"` is one input. Input names match `[A-Za-z][A-Za-z0-9_]{0,31}`.

## Saving

`/proof save` uses the existing recorder, so the same rules apply: at least one successful action, no failed call, no secret patterns. The contract is stored unbound (`bound: false`) with an added `procedure` block, in the same contract file and with the same versioning and history.

Inputs are validated before anything is written. For each `name=value`:

- in every string in the recorded action arguments and read arguments, each occurrence of the value becomes a slot for that input;
- a number in those arguments whose text equals the value becomes a typed slot (int or float);
- if the value appears in no action argument, the save fails, nothing is written and the recording stays active so the operator can retry.

Longer values are matched first. Slots are stored as structure, never as in-band markers, so recorded text such as `{{.Names}}` stays literal. Suggested encoding (internal, not part of the contract):

```json
{"$proof": "concat", "parts": ["./deploy.sh api ", {"input": "version"}, ""]}
{"$proof": "input", "name": "qty", "as": "int"}
```

Recorded arguments that already contain a `$proof` key are refused.

A value returned by a read and reused in a later action becomes a **live slot**, not a copied constant. This covers fresh snapshot tokens, stock versions, source revisions, request IDs returned by a lookup, and operation keys built from those values. A run resolves each live slot only from a successful matching read in that run; a missing read fails closed. Read-only tools that commonly return these values include `request_lookup`, `inventory_read`, `verify_fulfillment` and `operation_status`. A saved procedure records which read calls and result fields each step depends on.

## Running

`/proof run` requires every input and accepts no others. A value for a typed slot must parse as that type. One run per session; it refuses while the session is recording or already running, and refuses a contract that is currently bound as a strict guard.

Run state is per session and in memory. Step numbers count action steps only.

**Guide.** While a run is active, `pre_llm_call` returns `{"context": ...}` for that session and nothing for others. The guide names the procedure, the bound inputs, `step i of n`, any recorded reads (advisory unless a live slot needs them), the read calls required before the next action, and the next action's tool and arguments. Before a required read has happened, the action arguments are shown with readable `{{fresh tool.field}}` markers; after it, the resolved values are shown. Later steps are summarised. It tells the model to follow the steps exactly, not to improvise with the procedure's tools, and to stop and report if a step is blocked or fails.

**Gate.** In the run's session only:

- Read-only tools are always allowed. A successful read is remembered until the next action step completes, for at most 300 seconds.
- Tools that appear in no action step are untouched and do not clear remembered reads.
- A call to one of the procedure's action tools must equal the next step: same tool, and arguments equal to the template rendered from the run's inputs and the run's successful reads, with JSON types preserved. Live slots whose read has not happened fail closed. Anything else is blocked. The message starts with `Proof Run:`, names `step i of n`, and shows the expected tool and arguments so the follower can correct itself.
- If the step has pinned facts, each needs a remembered read for the rendered read arguments whose field equals the recorded value. A missing read or a different value blocks, and the message names `<tool>.<field>` and the current value.
- While an allowed step is still running, a second action call is blocked.

**Outcome.** A step succeeds when its result is not an error by the existing `_is_error_result` rule (exit code, status, error fields, host metadata). Result text is not compared, so timestamps and durations do not matter, and nothing is ever marked unproven.

- After the last step succeeds the run ends and the tools are released. The next `pre_llm_call` for that session returns a one-time note containing `completed`.
- If a step fails, or after 5 consecutive blocked attempts, the run is **stopped and held**: the procedure's action tools stay blocked in that session with a message containing `stopped` and `/proof cancel`, and the guide says the run stopped and tells the model to report and wait. `/proof cancel` or the end of the session releases it. The follower must not be free to improvise a fix with the same tools.

## Pinning

Live result values are always required for the arguments that use them. Other read facts are advisory unless pinned: one demonstration cannot tell which observed facts matter and which are noise. `/proof inspect` lists every recorded read fact as `<tool>.<field>`; `/proof pin` makes it a precondition for each step that recorded it, and `inspect` marks it `[pinned]`. A pinned value that contains an input's demonstrated value is rendered with the run's value. Pinning a digest field means "unchanged since recording".

## Reply wording the tests rely on

- `save`: success contains `saved`, the procedure name and each input name, and never `is live`. Failure starts with `Procedure not saved:`.
- `run`: success contains `started`. Failure starts with `Run not started:` and names a missing or unknown input.
- `pin`: success contains `pinned`. Failure starts with `Not pinned:`.
- `cancel` with an active run contains `cancelled`.

## Scope and limits

- This guides and checks a cooperative follower. It is not a sandbox: a model can still reach the same effect through a tool the procedure never used.
- The follower must reproduce each action's arguments itself. Very large arguments are a poor fit.
- A demonstration with a failed call cannot be saved; record a clean run.
- Run state does not survive a plugin reload. A lost run enforces nothing.
- Live slots bind only exact read-result values; nested objects and arrays inside a result are not substituted. A procedure that needs one must be taught with an explicit scalar field or handled by the follower.
- The strict guard and the checked-execution workflow engine are unchanged and separate.

## Implementation notes

- New module `proof_run/guided.py` holds templating, the procedure block, run state and verdicts, as pure functions and small classes with no host imports.
- `__init__.py` wires it: guided verbs are handled before `commands.handle`; `pre_tool_call` evaluates the session's run first and then the existing strict-guard checks; `post_tool_call` reports results to the run; `pre_llm_call` is registered; `on_session_finalize` clears the run.
- The storage root comes from the existing `_contracts_root(ctx)`. `guided.py` must not import `plugins.plugin_storage`.
- The plugin must work with a context that offers only `register_command` and `register_hook`.
- `core.py` may gain small additive helpers (for example a recorder preview of the steps so far). Existing behaviour and tests must not change.

## Validation performed

The acceptance tests prove the mechanics with Hermes-shaped tool results. They do not establish a general productivity or cost advantage.

A live synthetic inventory task was run on an isolated Hermes home with a real `AIAgent.run_conversation`, using the `glm-5.3` teacher and `glm-5.3-flash` follower through the configured provider. The teacher saved a five-tool procedure. Four guided runs on a different request ID and SKU all completed with exactly one reserve, correct readback and no wrong mutation; three unguided baseline runs with the same cheaper model also completed correctly.

Measured result for these six runs:

- guided: 4/4 passed, 5 model API calls each, 15,832 to 16,174 reported total tokens
- unguided: 3/3 passed, 6 model API calls each, 16,707 to 16,733 reported total tokens

This is a single small task with four guided samples and three unguided samples. It does not prove general superiority, token savings after teaching cost, or model quality. It does show that a cheaper model can complete a fresh-request run through the procedure, including fresh snapshot token, stock version, source revision and operation key, and that the unguided control can also solve the same task. A larger and more varied benchmark is still required before making a performance claim.
