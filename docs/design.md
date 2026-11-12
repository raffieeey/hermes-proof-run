# Proof Run execution design (implemented scope)

This describes the teach → compile → review/promote → run loop that sits
beside the existing schema-2 exact gate. The product pitch is unchanged: a
frontier teacher sets the procedure; a cheaper follower reuses it. There is
no student parameter-count requirement. This is not a security sandbox.

## Architecture

```
demonstration receipts (host ExecutionReceipt.v1)
        → trace-only capture (no schema-2 activation)
        → compiler + explicit operator review bundle
        → candidate workflow (proof.workflow/v1)
        → inspect / promote (no business effect)
        → /proof run name {inputs}
        → WorkflowStore job + checked host execution
        → bounded follower for at most one decide node
        → independent read-back / reconcile
```

Three artifacts stay distinct:

1. **Demonstration evidence** — host-authored receipts. Assistant `tool_calls`
   are not enough. Blocked or incomplete terminals cannot promote as a
   successful demonstration.
2. **Executable workflow** — immutable revision with typed inputs, bindings,
   authority, optional reviewed decision policy/route, and unresolved items.
3. **Run record** — pinned revision, actor `{id, profile_id, session_id}`,
   inputs, cursor, effect intent, and separate
   `effect_readback` / `semantic_judgment` / `whole_goal` labels.

Teacher outcome assurance is preserved by the compiler and is never upgraded
from model output. Known-failed teacher source cannot promote.

## Capture versus legacy gates

| | Legacy `/proof start`…`/stop` | New `/proof capture start`…`finish` |
|---|---|---|
| Storage | `contracts/` schema 2 | `runner/` traces + receipts |
| Activation | Immediate exact gate | Never an incidental exact gate |
| Reuse | Exact JSON args + fresh reads | Promoted workflow + reviewed inputs |
| Secrets | Refused at note/stop | Refused **before** any interested persist |
| Symlinks | Contract files/parents refused | A pre-existing `runner/` directory symlink is followed; keep that private root trusted |

Existing `start` / `stop` / `learn` / `rebind` / `status` / `unbind` /
`cancel` (recording) are unchanged. On a capable host, checked execution sends
`execution_phase=transform` so the legacy pre-hook does not consume; the
registered final authorizer `check` phase is pure and `commit` consumes once.
Calls with no phase keep the old consume-on-pre-hook path.

## Public commands

```
/proof capture start <name> [intent]
/proof capture finish
/proof capture cancel
/proof prepare <name> [{review bundle JSON}]
/proof inspect <name>
/proof promote <name> <revision>
/proof run <name> [{inputs JSON}]
/proof runs
/proof show <run-id>
/proof cancel-run <run-id>
/proof resume <run-id>
```

Inputs are one JSON object. Duplicate keys, NaN/Infinity, unknown review
fields, malformed names and malformed run ids are rejected; tokens are not
repaired. `/proof run` returns a run id (`R-` + 16 hex) and schedules work
with `ctx.spawn_task` when an event loop exists. It does not hold the plugin
lock across model/approval/handler I/O.

Trusted actor fields come from `PluginContext.profile_name` and the command
handler's declared `session_id` / `session_key`. `session_surface()` is a
string (`cli` / `telegram` / …), not a context dict.

## Host boundary

The runner uses `execute_checked`. Missing checked-execution/v1 capabilities
return `HOST_CAPABILITY_MISSING` and never fall back to `dispatch_tool`.
Receipt sinks persist host receipts only inside an active capture or run
scope, after credential refusal.

The extra host change is [docs/host-integration.md](host-integration.md) and
[integration/hermes-checked-execution.patch](../integration/hermes-checked-execution.patch).
The supported source build is Hermes `67664e236ddcc45824b871d575677287599af152`
with the supplied patch. Verification covers 184 checked-host/conformance tests,
26 hosted-scope denial tests, and the full registered-command workflow journey.
The [setup guide](host-integration.md) applies the version-matched SDK change.

## Follower

At most one decide node. No tools, no loop, no silent current-model or cloud
fallback. There is no integrated cloud follower adapter. Closed public output
remains `queue-decision/v1`: `select` with an authorized queue and genuine
packet evidence ids, or `abstain`. A schema-valid wrong select is **not**
independently correct; run receipts keep
`semantic_judgment=not_independently_verified`.

The local Ollama backend is a loopback text classifier, not a support agent.
A compact reviewed rubric (revision, rules, optional examples, output
contract) is the stable system prefix. The untrusted observation body is the
user message only and cannot become tools, policy, or control flow. Keep that
prefix short: this local CPU path is prefill-bound. Internal HTTP JSON is
`{"queue": <candidate or "abstain">}` and maps strictly onto the
canonical decision (select uses the packet's evidence ids; abstain uses
`ambiguous`). Canonical `select`/`abstain` objects are still accepted. One
decide call makes at most one loopback `/api/chat` request: no warmup, retry,
vote, or cloud fallback.

Transport defaults (overridable by reviewed `model_route`, then bounded):
`temperature=0`, `seed=0`, `num_ctx=1024` (256–2048), `num_predict=32`
(8–256), `num_thread=2` (1–8), `timeout_s=45` (0.05–60), `keep_alive=120`
seconds (1–300), `max_inflight=1` (1–4) per endpoint. Negative/NaN/non-finite
limits are rejected. `keep_alive` and the other resource fields are serialized
on the HTTP body/options and recorded on `identity()`. A per-endpoint bounded
semaphore serializes inference on a serial CPU server; it is not a database or
global-store lock. Permit wait plus HTTP share the decision deadline; cancel
and status stay on the existing off-loop jobs.

Backends:

- deterministic no-model control (uses **packet.policy.rules**; abstains if
  the runtime omits them rather than inventing a rubric)
- labeled scripted test backend
- local Ollama HTTP (`stream=false`, bounded ctx/output/deadline/keepalive,
  parsed loopback hostname only; no userinfo, no unapproved redirects, no
  silent network when `allow_network=False`)

Reviewed example route resources for a local classifier:

```json
{
  "provider": "ollama-local",
  "model": "llama3.2:1b",
  "endpoint": "http://127.0.0.1:11439",
  "fallback": false,
  "timeout_s": 45,
  "num_ctx": 1024,
  "num_predict": 32,
  "num_thread": 2,
  "keep_alive": "2m",
  "max_inflight": 1
}
```

Unit tests mock HTTP. Mocked JSON is not a usefulness claim. The separate
prompt/tool following notes in
[benchmarks/cloud-following-pilot/README.md](../benchmarks/cloud-following-pilot/README.md)
are not this follower and not IR/runtime acceptance.

## Synthetic ticket example

`example_ticket_*` is opt-in, caller-owned, private profile SQLite. It CAS
**both** content and routing revisions, dedupes same-key/same-payload,
conflicts on same-key/different-payload, and checks tenant identity before
read or route. Uncertain operations survive reopen. It is not a production
business integration.

## First-run demo and new-data repeat

```sh
python3 scripts/demo_runner.py --out /tmp/proof-run-demo.json
```

The script labels its teacher as **scripted-operator** (not a frontier
demonstration) and prints independent `run_store` / `target_store` paths.
After one explicit review/promote, `/proof run route-ticket {"ticket_id":"T-22"}`
reuses the standing procedure on a new id. `/proof inspect` shows reviewed
tools, bindings, authority, input constraints (including regex/enums),
constant values, policy rubric values, verification/identity/goal
predicates, operation identity, recovery, model route/limits and source
refs without dumping private trace bodies or recognized secrets.

The labeled mock remains the default demo. Actual registered-command /
`execute_checked` wiring is:

```sh
python3 scripts/run_actual_host_journey.py --host-source /path/to/hermes-agent
```

## Implemented versus unproven

Implemented here: trace capture, receipt-backed compiler, inspect/promote,
durable run/show/list/cancel/resume commands, checked HostPort adapter,
opt-in ticket adapter, bounded loopback followers, scripted golden path.

Not claimed: frontier-to-cheap parity, measured savings, an integrated cloud
follower, universal secret detection, sandboxing of installed plugins, or
production ticket/payment safety. A local 1B-class model in an example route
is a fixture, not a product requirement.
