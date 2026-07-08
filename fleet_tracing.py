"""fleet_tracing — Langfuse tracing for the agentic fan-out fleet.

The point: stitch one logical operation that spans *isolated processes* — a local
coordinator plus N Firecracker MicroVMs, each its own private chDB with no shared
backend — into a SINGLE Langfuse trace tree:

    agentic-fanout (root, coordinator)
    ├── plan                 coordinator decomposes the question
    ├── agent-0  ─┐          each worker answers a *different* sub-question…
    ├── agent-1   │          …on its own MicroVM; the worker's Strands agent spans
    ├── agent-2   │          (Bedrock + chDB tools) nest under agent-i via W3C
    ├── agent-3  ─┘          trace-context propagation (trace_id + parent_span_id)
    └── synthesis            coordinator folds the partials into one briefing

This module is a thin, dependency-isolated wrapper: fleet_core stays free of the
Langfuse/boto imports and takes an *optional* tracer (None → tracing off, behaviour
identical to before). Credentials come from SSM /langfuse/* (us-east-1 by default,
mirroring CLICKHOUSE_SSM_REGION) so no secret is ever hard-coded.

Verified contract (2026-07-03): Langfuse Cloud OTLP + SDK; span.id / span.trace_id
carry the OTEL ids the workers need; cross-process linking uses the v4 TraceContext
{trace_id, parent_span_id} — the same primitive agent.run_agent_with_tracing already
consumes on the worker side.
"""
from __future__ import annotations

import os

LANGFUSE_SSM_REGION = os.getenv("LANGFUSE_SSM_REGION", "us-east-1")
_SSM_PREFIX = "/langfuse"


def _ssm_get(name: str, region: str, *, decrypt: bool = False) -> str:
    """Fetch one SSM parameter value. Isolated so tests can monkeypatch it."""
    import boto3  # local import: fleet_core importers don't pay for boto3

    ssm = boto3.client("ssm", region_name=region)
    return ssm.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]


def load_langfuse_env_from_ssm(region: str = LANGFUSE_SSM_REGION) -> bool:
    """Populate LANGFUSE_* env from SSM /langfuse/*; return True iff all three load.

    Uses setdefault so an explicit env override (e.g. a local .env) always wins.
    Never raises — a missing param / absent AWS simply yields False (tracing off).
    """
    try:
        host = _ssm_get(f"{_SSM_PREFIX}/LANGFUSE_HOST", region)
        pk = _ssm_get(f"{_SSM_PREFIX}/LANGFUSE_PUBLIC_KEY", region)
        sk = _ssm_get(f"{_SSM_PREFIX}/LANGFUSE_SECRET_KEY", region, decrypt=True)
    except Exception:  # noqa: BLE001 — no AWS / param / permission → tracing off
        return False
    os.environ.setdefault("LANGFUSE_HOST", host)
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", pk)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", sk)
    return True


def trace_url(trace_id: str | None) -> str | None:
    """Canonical Langfuse UI URL for a trace id, or None when host/id is unknown.

    Delegates to observability.trace_url (project-scoped `/project/{id}/traces/…`
    path). Imported lazily so fleet_tracing's top level stays dependency-light.
    """
    from observability import trace_url as _trace_url

    return _trace_url(trace_id)


class FleetTracer:
    """Wraps a Langfuse client to build the fan-out trace tree.

    Duck-typed: fleet_core only calls start_root / start_worker / context / flush,
    so tests can drive it with a fake client and the production path needs no
    special-casing.
    """

    def __init__(self, client):
        self.client = client

    def start_root(self, question: str, n: int):
        """Open the coordinator's root observation (the whole fan-out)."""
        return self.client.start_observation(
            name="agentic-fanout", as_type="agent",
            input=question, metadata={"fleet_size": n, "pattern": "decompose-delegate-synthesize"})

    @staticmethod
    def child(parent, name: str, *, as_type: str = "span", input=None):
        """Open a coordinator-side child span (e.g. plan, synthesis)."""
        return parent.start_observation(name=name, as_type=as_type, input=input)

    def start_worker(self, root, idx: int, subquestion: str):
        """Open the span that one MicroVM worker's own agent trace nests under."""
        return root.start_observation(
            name=f"agent-{idx}", as_type="agent", input=subquestion)

    @staticmethod
    def context(span) -> dict:
        """The cross-process linkage a worker needs to continue this trace.

        Matches the body agent.run_agent_with_tracing consumes on the MicroVM:
        {"trace_id", "parent_span_id"}.
        """
        return {"trace_id": span.trace_id, "parent_span_id": span.id}

    def run_scope(self, *, session_id: str, user_id: str | None = None,
                  tags: list[str] | None = None, environment: str | None = None,
                  metadata: dict | None = None):
        """Context manager that stamps session_id / user_id / tags / environment on
        every observation — and thus the trace — created within it.

        Langfuse's canonical v4 way to group a run's traces into a Session
        (verified against v4 docs + a live trace). Because these are trace-level
        attributes, the whole distributed trace — including the remote MicroVM
        worker spans that nest in via trace_context — is grouped under the session.
        """
        from langfuse import propagate_attributes

        kwargs: dict = {"session_id": session_id}
        if user_id:
            kwargs["user_id"] = user_id
        if tags:
            kwargs["tags"] = tags
        if environment:
            kwargs["environment"] = environment
        if metadata:
            kwargs["metadata"] = metadata
        return propagate_attributes(**kwargs)

    def flush(self) -> None:
        self.client.flush()


def new_session_id(prefix: str = "agentic") -> str:
    """A short, unique Langfuse session id for one fleet run."""
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def build_fleet_tracer(region: str = LANGFUSE_SSM_REGION) -> "FleetTracer | None":
    """Construct a FleetTracer from SSM creds, or None if Langfuse is unavailable.

    Graceful degradation is the whole point: if creds/SDK are absent the fan-out
    still runs untraced (identical to the pre-tracing behaviour) — the demo never
    hard-depends on observability.
    """
    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        load_langfuse_env_from_ssm(region=region)
    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        return None
    try:
        from langfuse import get_client

        client = get_client()
        if not client.auth_check():
            return None
        return FleetTracer(client)
    except Exception:  # noqa: BLE001 — any SDK/auth failure → tracing off, run continues
        return None
