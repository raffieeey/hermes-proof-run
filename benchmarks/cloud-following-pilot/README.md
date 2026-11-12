# Frontier teacher → cheaper cloud followers: first paired pilot

**Finding:** cloud followers can execute the procedure. Adding the reviewed teacher guide improved full completion within the fixed budget in this small sample. Gemma4:31b with the guide completed both cases. This is an initial signal, not validated general transfer or proven cost savings.

## Observed results
Completed the full workflow, including finish_task, within the same five-cloud-call budget per trial:

| Follower | Policy + tools only | Same + teacher guide |
|---|---:|---:|
| gpt-oss:20b | 0/2 | 1/2 |
| gemma4:31b | 1/2 | 2/2 |

Aggregate: 1/4 baseline vs 3/4 guided. Every one of the eight runs reserved the correct quantity with one effect and no wrong mutation attempts. Seven verified the target readback. Three baseline failures had correctly applied and verified the effect but exhausted their budget before explicit finish. The guided GPT-OSS failure used the literal placeholder RE... as its verification request ID. Therefore do NOT report the table as general task accuracy or a transfer of frontier-level intelligence.

## Actual experiment
- Teacher: the current gpt-6.1-sol/openai-codex Hermes conversation actually invoked six synthetic tool operations. Its unknown-outcome reservation was reconciled read-only, independently read back, and finished with exactly one effect. teacher-trace.json contains the real receipt sequence.
- The guide was authored/reviewed from that trace and the tool contract. It is not weight training or an automatically compiled Proof Run workflow.
- Two fresh synthetic fixtures: normal reservation; unknown-but-committed reservation with an untrusted note asking for an excessive quantity.
- Both conditions got the same complete task/safety policy, tools, fixtures, five-model-turn/16-tool-call caps and ordered tool execution. The policy-only baseline already contained procedural requirements. Only the extra teacher guide differed. Guide order was counterbalanced across fixtures.
- Two students ran concurrently. 39 of the approved maximum40 cloud requests were made:20 gpt-oss:20b,19 gemma4:31b. All returned HTTP200 with the requested model IDs. Actual HTTP window was about25.7seconds, excluding preparation.
- The parent independently reran10driver tests, rejected5scoring counterexamples, replayed all8actual model tool traces, checked every saved target snapshot and source/fixture hash, and reconciled39request reservations/events.

## Cost and billing
The guided runs used 26,554 reported input+output tokens vs 21,738 baseline, **22.2% more**, despite19 vs20requests. Teacher/review cost and a matched frontier-every-time baseline were not separately measured. No dollar savings or break-even point is established.

The run used approved included quota. Private account identity, billing, quota and browser-session metadata are intentionally not published.

## Limits and recommendation
This is a two-case-per-condition prompt/tool microbenchmark, not integration acceptance of the actual Proof Run IR/controller, a statistical ranking, or production approval. The expert-authored deterministic control also completed2/2 without model calls; these structured cases do not establish that an LLM is necessary. The toy backend is not a security sandbox; its postcommit replay shortcut lacks full payload equality, so an additional independent grader checked replay bindings. No changed-payload mutation occurred in these trials.

Gemma4:31b is the more promising next student **for this workflow**, not a proven overall winner. Next meaningful validation: bind a cloud follower into the actual typed Proof Run controller, keep sequencing/verification/finalization deterministic, and test a broader frozen set of bounded semantic decisions plus matched no-demo/full-trace/no-model baselines. There is no parameter-count requirement.

Nothing was installed, activated, pushed or published by this pilot.

## Audit this evidence without a model or API key

From the repository root:

```sh
python3 -m unittest discover -s benchmarks/cloud-following-pilot -p 'test_*.py' -v
python3 benchmarks/cloud-following-pilot/replay.py
```

`calls.jsonl` contains the actual synthetic request/response fields (no headers, credentials or hidden reasoning). `trials.jsonl` preserves every success and failure. `results.json` is the measured aggregate. The offline command verifies all39responses,8target-state replays, fixture/input hashes, and reported token/call/outcome totals. It does not make new inference calls or prove independently that a provider served the claimed underlying weights. Live re-execution requires a separately authorized quota budget and credential/billing preflight; no credential-bearing runner or fabricated billing-guard file is shipped.
