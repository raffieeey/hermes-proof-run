# Live guided-run check

This directory records a live check that a cheaper model can complete a fresh
request through a procedure taught by a stronger model on a real Hermes
`AIAgent` turn. The check used a local synthetic inventory target; no shell,
file, browser, or deployment tool was exposed. Tool output was treated as
evidence, never as instructions.

The harness is intentionally not committed: it depends on a local provider key
and a local Hermes source checkout. The recorded results are in
`results.json`; `docs/guided-runs.md` describes the method.

## Result measured on 2026-11-14

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
