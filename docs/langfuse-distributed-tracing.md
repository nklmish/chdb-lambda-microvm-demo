# Cookbook — one Langfuse trace across an isolated MicroVM fleet

**The problem.** A single logical operation — the *agentic fan-out* — spans processes that
share nothing. A local coordinator decomposes one question, hands a different sub-question to
each of N Firecracker MicroVMs (each its own private chDB, no shared backend), then folds the
partial answers into one briefing. Off the shelf you get N+1 disconnected traces and no way to
see the run as a whole.

**The result.** All of it stitches into a *single* Langfuse trace tree:

```
agentic-fanout                 root observation (coordinator)
├── plan                       coordinator decomposes the question
├── agent-0 ─┐                 each worker answers a different sub-question…
├── agent-1  │                 …on its own MicroVM; the worker's Strands agent spans
├── agent-2  │                 (Bedrock + chDB tools) nest under agent-i via remote
├── agent-3 ─┘                 W3C trace-context (trace_id + parent_span_id)
└── synthesis                  coordinator folds the partials into one briefing
```

…and the whole tree — coordinator spans *and* the remote worker spans — is grouped under one
Langfuse **Session**, so a fleet run reads as a single entry in the Sessions view.

Three ingredients make it work. Each degrades to a no-op when Langfuse is absent — the fan-out
runs identically untraced, so the demo never hard-depends on observability.

---

## Ingredient 1 — the coordinator builds the trace tree

[`fleet_tracing.py`](../fleet_tracing.py) is a thin, dependency-isolated wrapper around the
Langfuse client. [`fleet_core.py`](../fleet_core.py) takes an **optional** `tracer` and never
imports Langfuse itself (`tracer=None` → tracing off, behaviour byte-for-byte identical). Every
tracer call in the fan-out goes through a `_safe(...)` helper that swallows errors —
observability must never break the answer.

```python
root      = tracer.start_root(question, n)          # name="agentic-fanout", as_type="agent"
plan_span = tracer.child(root, "plan", input=question)
# … per worker …
worker    = tracer.start_worker(root, idx, subquestion)   # name=f"agent-{idx}"
ctx       = tracer.context(worker)                  # {"trace_id", "parent_span_id"}
# … send ctx to the MicroVM (Ingredient 2) …
syn_span  = tracer.child(root, "synthesis", input=question)
tracer.flush()
```

`FleetTracer` is duck-typed — `fleet_core` only calls `start_root` / `start_worker` / `child` /
`context` / `run_scope` / `flush` — so the tests drive it with a fake client and the production
path needs no special-casing.

## Ingredient 2 — the worker nests its agent under the coordinator's span

`tracer.context(worker)` returns exactly the linkage the worker needs:

```python
{"trace_id": worker.trace_id, "parent_span_id": worker.id}
```

The coordinator posts that to the MicroVM's `/invocations`. On the worker,
[`agent.py`](../agent.py) `run_agent_with_tracing(user_input, trace_id, parent_span_id)` rebuilds
a **non-recording remote `SpanContext`** from the hex ids and attaches it as the ambient OTEL
parent, so the worker's own Strands spans (Bedrock call + chDB tools) join the caller's trace and
nest under `agent-i`:

```python
parent_ctx = set_span_in_context(NonRecordingSpan(SpanContext(
    trace_id=int(trace_id, 16), span_id=int(parent_span_id, 16),
    is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED),
)))
token = context.attach(parent_ctx)
try:
    with tracer.start_as_current_span("strands-agent"):
        return agent(user_input)
finally:
    context.detach(token)
```

Malformed ids fall back to a plain run — the answer still comes back, just unlinked.

**One export path, no duplicates.** The worker links via *explicit OTEL remote-parent context*,
**not** the Langfuse SDK. A single `BatchSpanProcessor` on the global tracer provider is the only
exporter on the VM (see [`observability.py`](../observability.py) `configure_langfuse_runtime`),
so each worker span is exported exactly once.

## Ingredient 3 — credentials resolve from SSM, never from code

No Langfuse secret is ever baked into an image or hard-coded.

- **Coordinator:** `build_fleet_tracer()` calls `load_langfuse_env_from_ssm()` to read
  `/langfuse/LANGFUSE_HOST`, `/LANGFUSE_PUBLIC_KEY`, `/LANGFUSE_SECRET_KEY` (region
  `LANGFUSE_SSM_REGION`, default `us-east-1`). An explicit env var (e.g. a local `.env`) always
  wins via `setdefault`. Missing param / no AWS → returns `None` → untraced.
- **MicroVM worker:** Lambda MicroVMs snapshots the container at **build** time and *resumes* it
  at run — so the build role must not hold prod secrets. Tracing is stood up **after resume**, in
  the run lifecycle hook (execution role), by `configure_langfuse_runtime()`, gated on
  `LANGFUSE_RESOLVE_FROM_SSM=true`. It resolves `/langfuse/*` and attaches the OTLP processor to
  the *existing* global provider (installing a second global provider is blocked once
  `opentelemetry-instrument` has set one).

## Session grouping — the whole distributed trace under one Session

`run_scope(session_id=…, user_id=…, tags=…)` wraps the fan-out in Langfuse v4's
`propagate_attributes(...)`. Because `session_id` is a *trace-level* attribute, it stamps every
observation created inside the scope — **including the remote worker spans** that nest in via
trace-context — so the entire distributed trace lands under one Session:

```python
with tracer.run_scope(session_id=sess_id, user_id=user_id, tags=["agentic-fanout"]):
    root = tracer.start_root(question, n)
    ...
```

The consensus fleet uses the same primitive: each worker produces its *own* trace, and passing a
shared `session_id` to every worker (`fleet_core.consensus_fleet`) groups the N traces in the
Sessions view. Worker traces are named `consensus-worker` (via `session_scope`'s `trace_name`)
instead of the FastAPI-default `POST /chat`.

---

## Run it

```bash
# Live browser view: one card per sub-question, then a synthesized briefing.
# Prints whether tracing is ON (creds found) or OFF (running untraced).
python scripts/agentic_console.py --region us-west-2
```

When tracing is on, the terminal `done` event carries a clickable `trace_url`
(`{LANGFUSE_HOST}/trace/{trace_id}`) and the console footer shows the Langfuse session id — open
either to see the full tree and the run grouped as one Session.

## Graceful degradation — the through-line

Every layer is best-effort:

| Layer | No Langfuse → |
|---|---|
| `build_fleet_tracer()` | returns `None`; `fleet_core` runs the fan-out untraced |
| `_safe(tracer.*, …)` | swallows any tracer error mid-run; the answer is unaffected |
| `configure_langfuse_runtime()` | returns `False`; worker agent runs without export |
| `run_agent_with_tracing` | malformed/absent ids → plain agent run |
| `session_scope()` | no `session_id` / SDK absent → `nullcontext()` |

There is no code path where a telemetry failure changes the analytical result.

## Where each piece lives

| File | Role |
|---|---|
| [`fleet_tracing.py`](../fleet_tracing.py) | `FleetTracer`, SSM cred load, `run_scope`, `build_fleet_tracer` |
| [`fleet_core.py`](../fleet_core.py) | optional-`tracer` fan-out; `_safe` wrapper; session ids |
| [`agent.py`](../agent.py) | worker-side `run_agent_with_tracing` + remote-parent linking |
| [`observability.py`](../observability.py) | OTEL env build, post-resume runtime config, session scope |
| [`scripts/agentic_console.py`](../scripts/agentic_console.py) | builds the tracer, surfaces `trace_url` / session id to the UI |

## Verify

```bash
pytest tests/test_fleet_tracing.py tests/test_observability_langfuse.py tests/test_fleet_core.py -q
```
