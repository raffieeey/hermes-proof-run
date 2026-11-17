# Live guided-run check

This directory holds two things: a retained record of a first live check (2026-11-14), and a
committed, repeatable guided-versus-unguided benchmark built to test the product claim that
the first check could not. The benchmark's first live run (2026-11-17) is reported under
[Result of the live A/B run](#result-of-the-live-ab-run-2026-11-17).

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

That ceiling describes the 2026-11-14 check only. The A/B run below replaces it for the
guided-versus-unguided question; see [Claim ceiling after the A/B run](#claim-ceiling-after-the-ab-run).

## Repeatable A/B benchmark

### Result of the live A/B run (2026-11-17)

Run once on the full set through the configured provider: the follower is `glm-5.3-flash`, the
teacher is `glm-5.3`, three runs per arm per task. That is 36 guided and 36 unguided follower runs
plus 12 teacher demonstrations. The record is [`results-2026-11-17.json`](results-2026-11-17.json)
(`complete: true`, 24.6 minutes from start to finish). A one-task smoke run (`s01-basic`, one run
per arm) came first, passed end to end and needed no fix; its output was not kept. No model call
was retried, and none of the 84 model-driven runs ended in a provider error.

Pass rates, with 95% Wilson intervals:

| Arm | n | Passed | Pass rate (95% Wilson) | API calls, median (range) | Reported total tokens, median (range) | Wall time per run, median (range); arm total |
|---|---:|---:|---|---|---|---|
| Guided | 36 | 30 | 83.3% (68.1 to 92.1) | 6 (5 to 15) | 20,495 (16,092 to 109,879) | 11.9 s (5.9 to 116.8); 851 s |
| Unguided | 36 | 33 | 91.7% (78.2 to 97.1) | 6.5 (4 to 8) | 19,585 (11,260 to 30,122) | 11.6 s (5.1 to 19.8); 424 s |

Difference in pass rate, guided minus unguided: **-8.3 points, 95% interval -24.5 to +7.8**
(Newcombe's hybrid score interval; Fisher's exact p = 0.48). The interval includes zero, so this
run does not separate the arms. Computed from the record rather than by the driver, the paired
view over the 36 matched instances agrees: 27 both passed, 3 only guided passed, 6 only unguided
passed, none both failed (exact McNemar p = 0.51).

By band (counts first; small n, so every interval is wide):

| Band | Guided | Unguided | Difference, 95% interval |
|---|---|---|---|
| simple | 12/12 (75.8 to 100.0) | 9/12 (46.8 to 91.1) | +25.0 (-4.1 to +53.2) |
| fresh-version | 9/12 (46.8 to 91.1) | 12/12 (75.8 to 100.0) | -25.0 (-53.2 to +4.1) |
| order-trap | 9/12 (46.8 to 91.1) | 12/12 (75.8 to 100.0) | -25.0 (-53.2 to +4.1) |

Eight of the twelve tasks went 3/3 in both arms. The other four:

| Task | Guided | Unguided |
|---|---:|---:|
| `s04-one-unit` | 3/3 | 0/3 |
| `f01-two-skus` | 2/3 | 3/3 |
| `f02-same-sku` | 1/3 | 3/3 |
| `o04-batch-reads` | 0/3 | 3/3 |

Teaching cost, reported on its own and not netted out of the table above: 12 demonstrations, all
12 passed their own checker and all 12 were saved. Teacher API calls: 98 in total, median 6
(range 6 to 12). Reported total tokens: 368,162 in total, median 20,007 (range 17,590 to
78,478). Wall time: 194 s in total, median 12.6 s (range 8.2 to 36.5). The follower totals
across all 36 runs of each arm were 245 calls and 1,233,686 tokens (guided) against 222 calls
and 739,798 tokens (unguided), so the guided arm used about two thirds more reported tokens
overall. About 93% of that 493,888-token gap sits on the five two-request tasks (`f01` to `f04`
and `o04`; fresh-version band median 50,355 against 26,284 tokens). `session_total_tokens` includes cached prompt tokens; this is not a dollar cost.

Arms and budget. Both arms had the same six tools (`request_lookup`, `inventory_read`,
`reserve_stock`, `verify_fulfillment`, `finish_task`, and the distractor `place_hold`), the same
system prompt, task text and instances. The guided arm additionally had a saved procedure run
with `/proof run`: the plugin's guide, delivered once per run (`guide_turns` was 1 in all 36
guided runs and 0 in all 36 unguided runs), and its gate. The call budget was the per-task
default: 10 API calls for the single-request tasks and 15 for the two-request tasks. No run was
cut off by the budget. Three guided runs came close: one used 15 of 15 calls and passed on the
last call (`f01-two-skus`, k=1), two used 14 of 15 (`f02-same-sku` k=0, `f03-same-sku-exact`
k=2). The most any unguided run used was 8.

What the traces show. These are observations from the record, not an experiment on cause.

- **All six guided failures have one signature.** In each, the follower called `inventory_read`
  before `request_lookup`. The target refused the first `reserve_stock` with
  `read_before_lookup`, the plugin marked step 1 failed and stopped the guided run, and the
  follower's corrected retry was blocked ("stopped at step 1 of 4"). The six are `f01` k=2,
  `f02` k=1 and k=2, and `o04` k=0, 1 and 2. The unguided arm made the same mistake three
  times, all on `o01-stock-first`, and recovered each time by retrying. So in this run the
  stop-on-failure rule turned a recoverable target refusal into a failed run. This is the
  behaviour the findings below describe from the offline work, now seen live.
- **Why the guided follower read first is not established.** Guided followers read before the
  lookup in six runs on `f01`, `f02` and `o04` and never on the other nine tasks; unguided
  followers did so only on `o01`. The earlier finding that the guide's "Required before this
  action" list names `inventory_read` before `request_lookup` fits this, but the results file does
  not store the guide text the followers saw, so that link is an inference.
- **All three unguided failures are one task and one omission.** On `s04-one-unit` the prompt
  is `Fulfil <request> (<sku>).` and does not ask to close the task. All three unguided followers
  reserved, read back, and replied in text without calling `finish_task`, which the checker
  requires. All three guided followers called it; the saved procedure ends with that step. This
  is what a saved procedure transferred against nothing on a terse prompt; it says little about
  how an unguided model does when the prompt says to close the task (`s01` does, and both arms
  passed it 3/3).
- **The gate stopped stale reads before the target saw them, at a price.** Guided runs had 0
  `stale_version` or `stale_token` rejections at the target against 12 in the unguided arm
  (11 and 1, all recovered). Instead, 11 of the 30 passing guided runs were blocked 22 times in
  total: 19 with "fresh read data is missing; perform the required read before this action" and
  3 with "expected finish_task ...". The re-reads are where the extra calls and tokens on the
  two-request tasks went.

### Claim ceiling after the A/B run

One synthetic inventory target, a twelve-task set, three matched instances per task, and one
teacher and follower pair. On this set the guided arm did not pass more runs than the unguided
arm: 30/36 against 33/36, a difference of -8.3 points whose interval (-24.5 to +7.8) includes
zero. The data neither show a benefit nor exclude a harm of that size, and the point estimate is
lower for guided. The arms differ by task, not overall: guided passed the one task where
unguided never closed the request, and failed on three tasks that share one failure signature
(a read-order mistake, then the gate's stop). The set does discriminate (four of twelve tasks
had a failure in at least one arm); it does not yet show that guidance helps.

What the number does not establish:

- The intervals treat the 72 runs as independent, but they are 12 tasks times 3 instances, so the
  uncertainty about a new task is larger than the intervals say. Three runs per task cannot say
  whether `f02` at 1/3 and `o04` at 0/3 are rates or chance.
- The teacher saw the order rules and the unguided follower did not (see "What is and is not
  controlled"), so the comparison is a saved procedure against nothing, not against the same
  rules in the prompt.
- No productivity, token-savings, cost-savings or superiority claim. On the two-request tasks
  the guided arm used more calls and reported tokens, and teaching cost is not amortised.

`repo.dirty` in the record reads `true`. The only untracked path was the results file itself,
which sits under the checked directory; the tree was clean when the run started (`HEAD` `3c26e63`,
plugin version 0.3.2).

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

Observed with the plugin at `fa4085b`. Two were fixed afterwards; one remains.

- **Coincident numbers in a demonstration defeated live binding — fixed.** If a recorded quantity
  equalled a stock level, the plugin kept one candidate per value and could not bind it to the
  action's `quantity`, so the saved procedure replayed the teacher's quantity. Candidates are now
  kept per `(tool, field)`; `test_coincident_numbers_bind_to_the_right_field` pins the corrected
  behaviour, and `proof-run/tests/test_guided_fidelity.py` covers the gate.
- **The guide was delivered once per conversation turn — fixed in the guide text.** `pre_llm_call`
  runs once per user turn (every guided run in the driver check shows `guide_turns == 1`), so the
  old text left later steps as tool names only. The guide now renders every step with its tool and
  arguments and marks the current one `NOW`.
- **The gate does not enforce read order — remains.** Reads are not action steps, so a follower
  that reads stock before the lookup passes the gate and is refused by the target, which stops the
  guided run. The guide now lists each step's reads in recorded order and warns that an earlier
  read may be stale, but the contract records reads as a set, so it cannot require a re-read after
  a later read. The 2026-11-17 live run met this in all six guided failures (see the result above).

### Next

1. Decide the plugin question separately; it was not touched here. Should a step the target
   refused with no state change stop a guided run, and should the guide or gate carry the
   recorded read order? Then re-run `f01`, `f02` and `o04` with many more runs per arm than 3,
   because 1/3 and 0/3 cannot say how often the stop happens.
2. Make the record able to check the cause. The driver captures the guide text each follower saw
   (`RunLog.guide_first`) but does not write it to the results, so why the guided follower read
   first is an inference. Also leave the output file out of `repo.dirty`.
3. Add the third arm: the teacher note as plain text in the follower's prompt, so a saved
   procedure is compared with the same rules written down and not with nothing.
4. Add a variant of `s04-one-unit` whose prompt asks to close the task, to separate "did not know
   the closing step" from "followed a procedure".
5. If guided stops losing on the order-sensitive tasks and both arms then pass everything, the
   set no longer discriminates; use a harder one: a weaker follower, tighter call budgets, more
   order traps, more requests per task, a stale read that appears mid-run, an unknown commit
   outcome that must be reconciled, and an instruction in the tool output to resist.
