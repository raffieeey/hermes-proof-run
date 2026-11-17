# Live guided-run check

This directory holds two things: a retained record of a first live check (2026-11-14), and a
committed, repeatable guided-versus-unguided benchmark built to test the product claim that
the first check could not (2026-11-17). The benchmark has not produced a live result yet; see
[Status](#status).

## Result measured on 2026-11-14

This check used a local synthetic inventory target on a real Hermes `AIAgent` turn: a
stronger model taught a procedure and a cheaper model followed it on a fresh request. No
shell, file, browser, or deployment tool was exposed. Tool output was treated as evidence,
never as instructions. Its harness was not committed (it depended on a local provider key and
a local Hermes checkout); the recorded results are in `results.json`, and `docs/guided-runs.md`
describes the method.

One teacher run, four guided runs, and three unguided controls:

| Arm | Model | Passed | API calls per run | Reported total tokens |
|---|---|---:|---:|---:|
| Teacher | `glm-5.3` | 1/1 | 6 | 16,671 |
| Guided | `glm-5.3-flash` | 4/4 | 5 | 15,832-16,174 |
| Unguided | `glm-5.3-flash` | 3/3 | 6 | 16,707-16,733 |

All runs exposed `request_lookup`, `inventory_read`, `reserve_stock`,
`verify_fulfillment`, and `finish_task`. Model names are route labels; the
provider name is intentionally omitted from the public record. `session_total_tokens` is the client's reported total
and includes cached prompt tokens; this is not a dollar-cost measurement.

## Claim ceiling

This is one small synthetic task with four guided samples and three unguided samples. Both arms passed,
so this result does **not** show that guided runs outperform an unguided model
on this task. It does show that the guided path works end to end on a live
model and that the module resolves fresh snapshot tokens, stock versions,
source revisions, and operation keys from the run's own reads rather than
replaying the teacher's. Teaching and review cost are not amortized. A larger,
varied benchmark is required before claiming productivity, token, or cost
savings.

**This ceiling is unchanged.** The A/B benchmark below exists to move it, and until it has a
live result it does not.

## Repeatable A/B benchmark

### Status

Committed and verified offline. **Not yet run live.** On 2026-11-17 the configured provider
credential was rejected by the provider (HTTP 401, "API key inactive") at the driver's
preflight and by direct probes repeated over about 15 minutes. The driver stopped before any
run, so there is no `results-<date>.json` and no measured table. Nothing in this README is a
measurement of the A/B question. A key that the provider accepts is the only thing missing.

### What it does

For each task the driver:

1. has the teacher model demonstrate the task once on a real `AIAgent.run_conversation` turn
   (recording with `/proof start`), checks the demonstration against the target's final state,
   and saves it with `/proof save`;
2. runs `--runs-per-arm` fresh instances of the same task with the follower in two arms, on
   matched instances: **guided** (`/proof run`, the plugin's guide and gate) and **unguided**
   (no procedure), with the same tools, system prompt, task text and API-call budget.

Every run is recorded with pass/fail, failure reason, API calls, reported total tokens, wall
time, target rejections, gate blocks and a tool trace. A failed run is never retried. If a
teacher demonstration fails its own checker or cannot be saved, that task has no matched arms
and is reported under `teaching`, not dropped silently.

### Run it

Use the Python that has Hermes' dependencies (`HERMES_PY`); the plugin under test is the
repository's `proof-run/`, loaded by the driver, not whatever is installed in Hermes.

```sh
HERMES_PY=/path/to/hermes/venv/bin/python
$HERMES_PY benchmarks/live-guided/run_ab.py \
  --provider <configured-provider> --model glm-5.3-flash \
  --teacher-provider <configured-provider> --teacher-model glm-5.3 \
  --tasks all --runs-per-arm 2 \
  --out benchmarks/live-guided/results-YYYY-MM-DD.json
```

- `--tasks`: `all`, a number N (spread across the three bands), or comma-separated ids.
- `--runs-per-arm`: runs per arm **per task**; the total per arm is tasks x runs.
- `--call-budget N`: one budget for every task. The default is per task: `5 x requests + 1 closing reply + 4 slack`.
- `--list-tasks` prints the task set. `--selftest` runs the whole driver against a scripted
  loopback provider with no key; its output is marked `selftest` and is a wiring check only.
- The driver makes one 8-token call per route first. If the provider rejects it, it exits with
  status 3 and writes nothing.
- Stdout is one JSON line (path and summary); progress goes to stderr; the full record is `--out`,
  rewritten after every run so an interrupted run keeps what it measured.
- Credentials come from the provider setup Hermes already has. The results file holds model
  names only: no provider name, endpoint or key, and no raw transcripts.

### Task set

Twelve tasks, four per band. Each is a template: every run gets its own request ids, skus,
quantities, stock version, version step and salt, so the teacher's values never equal a
follower's.

| Band | Tasks | What forces real work |
|---|---|---|
| simple | `s01-basic`, `s02-exact-stock`, `s03-note-injection`, `s04-one-unit` | one request, one read; an exact-stock edge; an untrusted note asking for another quantity; a terse prompt |
| fresh-version | `f01-two-skus`, `f02-same-sku`, `f03-same-sku-exact`, `f04-two-skus-tight` | two requests; each reservation moves the ledger version, so a read taken before it is stale |
| order-trap | `o01-stock-first`, `o02-all-values-given`, `o03-set-aside`, `o04-batch-reads` | the task text invites an order the target rejects: stock before lookup, a direct reserve, `place_hold`, reading both skus up front |

The target (`inventory_target.py`, stdlib only) exposes the five tools from the 2026-11-14
check plus one distractor, `place_hold`, which succeeds and changes stock but is not a
reservation. Rules enforced at the target, not only at the gate:

- `inventory_read` issues a stock version, snapshot token and operation key that exist only in
  that read, derived from a per-world salt. The teacher's key or token is refused.
- A read counts for a request only if the request was looked up first.
- Every committed change moves the ledger version, so an earlier read goes stale.
- `finish_task(completed)` needs a reservation and a successful readback.

A task passes only if, for every request, exactly one reservation of the authoritative sku
and quantity exists, was read back and closed as completed, no hold was placed, and stock
moved by exactly the reserved amounts. The checker (`task_set.check`) reads target state and
never the model's prose. Rejected calls that changed nothing do not fail a run; they are
counted. Using `place_hold` fails the run even if a correct reservation follows.

### What is and is not controlled

Controlled: model, tools, tool descriptions, system prompt, task text, instance (matched per
run index), API-call budget (`max_iterations`, with the host's extra summary call after
exhaustion suppressed; a turn that runs out of budget is recorded as budget exhaustion, not as a
provider error), no fallback model, the host's per-call retry count set to one (see below for
what remains), arm order alternated across tasks and runs, one run at a time.

Not controlled, and it limits what a result means:

- **The teacher is told the order rules; the unguided follower is not.** The teacher's prompt
  carries a short note stating them (`task_set.TEACHER_NOTE`) so its demonstration can be clean,
  because a recording with a failed call cannot be saved. A gap between the arms therefore
  measures what a saved procedure transfers against *nothing*, not against the same rules
  written into the follower's prompt. A third arm with the note as text is the missing
  control.
- Tool descriptions state what each tool takes, not the order to use them. The unguided arm
  can learn the order from target errors, inside the budget.
- Host transport: Hermes can make up to three HTTP attempts for one failed model call
  (observed against the scripted provider). They happen inside one run; the driver never
  re-runs it. A provider error is recorded as a failed run and counted separately.
- A model may batch several tool calls in one message. Per the host's dispatch planner, only a
  fixed set of read-only built-ins runs in parallel, so these tools run one at a time in the
  order listed; a batch is still one API call.
- One teacher/follower pair, one target, one task family. Wall time includes provider latency.

### Statistics and reading a result

Pass rate per arm with a 95% Wilson interval; the difference (guided minus unguided) with
Newcombe's hybrid score interval and Fisher's exact p; per-band breakdown (small n, counts
first); API calls, reported total tokens and wall time per arm; **teaching cost reported on its
own**, not amortised. `summary.reading` is a neutral sentence chosen by rule: if both arms
passed everything the task set does not discriminate and the next step is a harder set; if the
guided arm is lower it says so. No dollar claims, no superiority wording.

### Verified offline

```sh
python3 -m compileall -q benchmarks/live-guided
python3 -m unittest discover -s benchmarks/live-guided -p 'test_*.py' -v
# adds the end-to-end driver check on a real AIAgent against the scripted provider:
HERMES_PY=/path/to/hermes/venv/bin/python python3 -m unittest discover -s benchmarks/live-guided -p 'test_*.py'
```

38 tests: target rules (each rejection code), task set shape and instance disjointness, checker
reasons, statistics (including Newcombe's published example and a brute-force Fisher check),
and scripted agents through the real plugin. Those show that a perfect teacher records and
saves all 12 tasks, a perfect guided follower passes all 12 on two fresh instances each, the
teacher's literal values are refused (by the gate when guided, by the target when not), and
the wrong orders fail at the target with the intended codes. The driver check runs a real
`AIAgent` against a scripted loopback provider and covers the budget (exactly the budget in API
calls, no extra summary call, recorded as budget exhaustion), a provider error (recorded, never
retried into a pass) and a follower that reads before the lookup (passes the gate, refused by
the target). A scripted agent is not a model: this shows the benchmark is passable and its traps
are real, not that any model passes them. It also caught one driver bug before any live run: a
turn that runs out of budget carries an `error` field in the host's result and was first
recorded as a provider error.

### Findings while building it

Observed with the plugin as committed; none was changed here.

- **Coincident numbers in a demonstration defeat live binding.** If a recorded quantity equals
  a stock level, the plugin keeps one candidate per value (the later field) and cannot bind it
  to the action's `quantity`, so the saved procedure replays the teacher's quantity and the
  gate then blocks a correct follower. Demonstrations here always have slack stock with no
  repeated numbers; exact stock is only an edge case for the runs.
  `test_known_plugin_limitation_coincident_numbers_in_a_demonstration` pins the behaviour and
  will fail, as a prompt to update this note, if the plugin stops doing it.
- **The guide is delivered once per conversation turn.** `pre_llm_call` ran once per run (every
  guided run in the driver check shows `guide_turns == 1`, including 4-step runs), so later steps of a multi-step procedure are
  visible to the follower only as "Later steps" tool names and in block messages.
- **The gate does not enforce read order.** Reads are not action steps, so a follower that
  reads stock before the lookup passes the gate and is refused by the target, which stops the
  guided run. The guide's "Required before this action" list names `inventory_read` before
  `request_lookup`; the recorded order is only in "Reads recorded before this step".

### Next

1. Run it with a provider key that works, then replace [Status](#status) with the measured
   table and the interval, and update the claim ceiling from what the numbers allow.
2. If both arms pass everything, say the task set does not discriminate and use a harder set:
   more requests per task, a stale read that appears mid-run, an unknown commit outcome that
   must be reconciled, and an instruction in the tool output to resist.
3. Add the third arm: the teacher note as plain text in the follower's prompt.
4. More than 2 runs per task if the intervals are wider than the effect worth detecting.
