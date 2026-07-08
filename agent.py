"""NYC Taxi Analytics Agent — Strands Agent wiring.

Exports:
    create_agent(session_manager=None) -> Agent
    chat_with_agent(user_input, memory) -> dict
    stream_chat_with_agent(user_input, memory) -> AsyncGenerator[dict]
    run_agent_with_tracing(user_input, trace_id=None, parent_span_id=None) -> str

Memory type: all HTTP-facing code passes DualMemory (from agent_memory.py).
The Agent is created fresh per request so the DualMemory's
strands_session_manager scopes AgentCore Memory correctly — no module-level
agent instance.

Data-awareness: the system prompt is assembled from data_profile.json (written
by init_db.py at container build time). No silent fallback if the profile is
missing — _load_data_profile raises FileNotFoundError loudly.

Langfuse tracing is only active in DEV/TST when a trace_id is supplied.
PRD uses OTEL-only tracing via observability.py's init_tracing (wired from
main.py lifespan, not here).
"""
from __future__ import annotations
import json
import os
from typing import AsyncGenerator, TYPE_CHECKING

from strands import Agent
from strands.models import BedrockModel
from langfuse import get_client

from chdb_tools import analyze_taxi_data
from sql_tools import query_with_fresh_data
from weather_tools import analyze_weather_impact
from federation_tools import analyze_fleet_across_clouds, analyze_zone_tipping
from timing import get_timings

if TYPE_CHECKING:
    from agent_memory import DualMemory


_PROFILE_PATH = "data_profile.json"


def _load_data_profile() -> dict:
    """Read data_profile.json from CWD. Raises FileNotFoundError loudly if absent."""
    with open(_PROFILE_PATH, "r") as f:
        return json.load(f)


def _build_system_prompt(profile: dict) -> str:
    """Assemble the data-aware system prompt from the baked profile.

    Includes row_count, date_range, fare_stats, top pickup/dropoff zones,
    payment_distribution, baked_cutoff, delta_start, tool-selection rules,
    AgentCore Memory awareness, and a Manhattan-zone clarification
    note (canonical Manhattan zone list is not in the
    spec; we seed from top_pickup_zones + ask for clarification on broader
    Manhattan queries).
    """
    fare = profile["fare_stats"]
    dates = profile["date_range"]
    pay = profile["payment_distribution"]
    pickup_zones = profile["top_pickup_zones"]
    dropoff_zones = profile["top_dropoff_zones"]
    top_pickup_ids = ", ".join(str(z["zone_id"]) for z in pickup_zones)
    top_dropoff_ids = ", ".join(str(z["zone_id"]) for z in dropoff_zones)

    return f"""You are an analytical agent for NYC Yellow Taxi trip data.

DATA PROFILE:
- Baked rows: {profile["row_count"]}
- Date range: {dates["min"]} to {dates["max"]}
- Baked cutoff: {profile["baked_cutoff"]} (delta layer starts {profile["delta_start"]})
- Fare stats: min=${fare["min"]}, max=${fare["max"]}, mean=${fare["mean"]}, median=${fare["median"]}
- Top pickup zones (zone_id): {top_pickup_ids}
- Top dropoff zones (zone_id): {top_dropoff_ids}
- Payment mix: credit {pay["credit"]}, cash {pay["cash"]}, other {pay["other"]}

COLUMNS:
pickup_datetime, dropoff_datetime, passenger_count, trip_distance,
pickup_location_id, dropoff_location_id, fare_amount, tip_amount,
total_amount, payment_type (1=Credit, 2=Cash, 3=NoCharge, 4=Dispute),
congestion_surcharge, airport_fee.

TOOL SELECTION:
- Questions inside the baked date range ({dates["min"]} to {profile["baked_cutoff"]})
  → analyze_taxi_data
- Questions covering dates after {profile["baked_cutoff"]} (delta layer)
  → query_with_fresh_data
- Questions about weather impact on rides or fares
  → analyze_weather_impact
- Questions about long-run / multi-year trends "across the years" or "over the
  decade" — especially tipping or fares — OR questions that ask where the data
  lives / how it spans clouds
  → analyze_fleet_across_clouds
- Questions about which pickup zones (by name/borough) tip best
  → analyze_zone_tipping

FEDERATION (analyze_fleet_across_clouds):
This tool answers one declarative chDB SQL statement that federates NYC taxi
trips across the clouds where each year natively lives, with NO connection pools
or credential brokering:
  2015 → GCS (ClickHouse public archive) | 2018 → Azure (Open Datasets) |
  2023 → ClickHouse Cloud (warehouse)    | 2024 → local baked chDB |
  2025 → AWS S3 (NYC TLC CloudFront).
The first call reaches across the clouds (a few seconds, network-bound) and
materializes the result into the local chDB store; an identical follow-up is
served from that local cache in milliseconds. This is "federate to reach,
localize to think." If a user asks to re-run fresh, pass refresh=true.

ZONE TIPPING (analyze_zone_tipping):
For "which zones tip best?" questions, this tool issues ONE chDB statement that
JOINs local baked taxi trips to a PostgreSQL taxi-zone lookup (the postgresql()
table function — a third-party RDBMS treated as just another table). Returns the
top pickup zones by revenue-weighted tip rate with borough + zone names.

RESPONSE FORMAT:
When reporting results from analyze_weather_impact, always include the data
source in your response. The tool returns a "source" field that cites the
weather data origin — mention it explicitly. For example: "According to NOAA
GSOD weather data from LaGuardia Airport station..." or "Based on NOAA weather
records from LaGuardia Airport...". Include both the NOAA source and the
station name (LaGuardia Airport) in your narrative.

When reporting results from analyze_fleet_across_clouds, name the clouds each
year came from (the "sources_used" / per-row "cloud" fields) and the "mode"
("single-statement" cross-cloud reach vs "local cache (materialized)") plus the
"elapsed_ms" — the point is that one SQL statement spanned multiple clouds and
the result is then cached locally for instant re-query.

MANHATTAN ZONES:
The top-pickup IDs above include the most frequent Manhattan zones in this
dataset. For broader "Manhattan" queries the user may refer to additional
zone IDs not in the top list — ask for zone clarification if the question is
ambiguous. Do NOT invent zone IDs.

MEMORY:
Your conversation history is stored across two layers: fast session memory
(in-process) and persistent AgentCore Memory (cross-session). When prior
analytical discoveries or user preferences are relevant to the current
question, AgentCore Memory will surface them as XML tags before you see the
user input — use them to maintain continuity.

Always call a tool to answer factual questions about trips, fares, or
weather. Never fabricate numbers.
"""


def create_agent(session_manager=None) -> Agent:
    """Create a Strands Agent with data-aware system prompt and 3 tools.

    session_manager: optional AgentCoreMemorySessionManager (from DualMemory)
    that enables automatic AgentCore Memory retrieval and save around each
    invocation. Omit for plain chat without persistent memory.
    """
    profile = _load_data_profile()
    system_prompt = _build_system_prompt(profile)

    kwargs = {
        "system_prompt": system_prompt,
        "tools": [
            analyze_taxi_data,
            query_with_fresh_data,
            analyze_weather_impact,
            analyze_fleet_across_clouds,
            analyze_zone_tipping,
        ],
        "model": BedrockModel(
            model_id=os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"),
            # BEDROCK_REGION wins when set (Lambda MicroVMs reserves AWS_REGION, so
            # the deploy bakes BEDROCK_REGION instead); else fall back to the
            # standard AWS region env vars, then us-east-1.
            region_name=(
                os.getenv("BEDROCK_REGION")
                or os.getenv("AWS_REGION")
                or os.getenv("AWS_DEFAULT_REGION")
                or "us-east-1"
            ),
        ),
    }
    if session_manager is not None:
        kwargs["session_manager"] = session_manager
    return Agent(**kwargs)


def chat_with_agent(user_input: str, memory: "DualMemory") -> dict:
    """Synchronous chat. Augments with local context, invokes the agent with
    session-manager-enabled memory, saves both sides to chDB, returns
    {"response": str, "timings": list}.
    """
    local_context = memory.get_local_context(limit=5)
    augmented = f"{local_context}\n\nUser: {user_input}" if local_context else user_input

    agent = create_agent(session_manager=memory.strands_session_manager)
    response = agent(augmented)
    response_text = response.message["content"][0]["text"]

    memory.save_conversation("user", user_input)
    memory.save_conversation("assistant", response_text)

    return {"response": response_text, "timings": get_timings()}


async def stream_chat_with_agent(
    user_input: str, memory: "DualMemory"
) -> AsyncGenerator[dict, None]:
    """Async SSE streaming variant of chat_with_agent.

    Yields:
      - {"type":"token","text":<delta>} for each text delta
      - {"type":"tool","name":<name>,"status":"start"|"end"} for tool lifecycle
      - {"type":"done","timings":[...],"metrics":{...}} as the final event

    Token deltas come from event["data"]; tool starts come from
    event["event"]["contentBlockStart"]["start"]["toolUse"]["name"]; tool ends
    come from event["event"]["contentBlockStop"] on a non-zero contentBlockIndex.
    Token usage is summed from event["event"]["metadata"]["usage"] across cycles.
    Trace ID is extracted from the first event carrying event_loop_cycle_span.
    """
    local_context = memory.get_local_context(limit=5)
    augmented = f"{local_context}\n\nUser: {user_input}" if local_context else user_input

    agent = create_agent(session_manager=memory.strands_session_manager)

    buffer: list[str] = []
    tokens_in = 0
    tokens_out = 0
    tools_used: list[str] = []
    open_tool_indices: dict[int, str] = {}  # contentBlockIndex → tool name

    # Capture trace_id from the ambient OTEL request context (FastAPI HTTP
    # autoinstrumentation's span), NOT from Strands' inner event_loop_cycle_span.
    # The latter lives in a tracer that didn't accept the OTLP exporter override
    # so its spans never reach Langfuse — clicking that trace_id 404s.
    trace_id: str | None = None
    try:
        trace_id = get_client().get_current_trace_id()
    except Exception:
        pass

    async for event in agent.stream_async(augmented):
        if not isinstance(event, dict):
            continue

        # Token deltas live at top-level "data" key.
        delta = event.get("data", "")
        if delta:
            buffer.append(delta)
            yield {"type": "token", "text": delta}
            continue

        inner = event.get("event")
        if not isinstance(inner, dict):
            continue

        # Tool-use start.
        cbs = inner.get("contentBlockStart")
        if isinstance(cbs, dict):
            start = cbs.get("start") or {}
            tool_use = start.get("toolUse") if isinstance(start, dict) else None
            if isinstance(tool_use, dict) and tool_use.get("name"):
                name = tool_use["name"]
                idx = cbs.get("contentBlockIndex", -1)
                tools_used.append(name)
                if isinstance(idx, int):
                    open_tool_indices[idx] = name
                yield {"type": "tool", "name": name, "status": "start"}
            continue

        # Tool-use end (contentBlockStop on a tool index).
        cbstop = inner.get("contentBlockStop")
        if isinstance(cbstop, dict):
            idx = cbstop.get("contentBlockIndex", 0)
            if isinstance(idx, int) and idx in open_tool_indices:
                yield {"type": "tool", "name": open_tool_indices.pop(idx), "status": "end"}
            continue

        # Bedrock metadata → token counts.
        metadata = inner.get("metadata")
        if isinstance(metadata, dict):
            usage = metadata.get("usage") or {}
            if isinstance(usage, dict):
                tokens_in += int(usage.get("inputTokens", 0) or 0)
                tokens_out += int(usage.get("outputTokens", 0) or 0)

    response_text = "".join(buffer)

    memory.save_conversation("user", user_input)
    memory.save_conversation("assistant", response_text)

    from observability import trace_url as build_trace_url

    trace_url = build_trace_url(trace_id)

    yield {
        "type": "done",
        "timings": get_timings(),
        "metrics": {
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tools_used": tools_used,
            "model": os.environ.get("BEDROCK_MODEL_ID", "unknown"),
            "trace_id": trace_id,
            "trace_url": trace_url,
        },
    }


def run_agent_with_tracing(
    user_input: str,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
) -> str:
    """Run the agent, optionally linking to an external Langfuse trace (DEV/TST only).

    In PRD (or when trace_id is absent), runs a plain agent invocation. In
    DEV/TST with a trace_id, reopens the span via Langfuse v4's
    start_as_current_observation so the agent's Strands OTEL spans become
    children of the caller's observation. Uses v4 key "parent_span_id" —
    NOT the deprecated "parent_observation_id".
    """
    env = os.getenv("LANGFUSE_TRACING_ENVIRONMENT", "PRD")
    traced = env in ("DEV", "TST") and bool(trace_id)

    # On a MicroVM the run/resume hook configures Langfuse export; ensure it's up
    # (idempotent; returns False and no-ops off a MicroVM / without the flag).
    runtime_configured = False
    if traced:
        try:
            from observability import configure_langfuse_runtime
            runtime_configured = configure_langfuse_runtime()
        except Exception:  # noqa: BLE001 — telemetry must never break the agent
            runtime_configured = False

    agent = create_agent()

    if traced and runtime_configured:
        # MicroVM worker: link via explicit OTEL remote-parent context so the
        # agent's spans nest under the coordinator's fan-out trace, exported by the
        # single OTLP processor configure_langfuse_runtime attached. No Langfuse SDK
        # here → exactly one export path, no duplicate spans.
        response = _run_agent_otel_linked(agent, user_input, trace_id, parent_span_id)
    elif traced:
        # AgentCore DEV/TST: the proven Langfuse-SDK linking path.
        with get_client().start_as_current_observation(
            name="strands-agent",
            as_type="span",
            trace_context={
                "trace_id": trace_id,
                "parent_span_id": parent_span_id,
            },
        ):
            response = agent(user_input)
    else:
        response = agent(user_input)

    return response.message["content"][0]["text"]


def _run_agent_otel_linked(agent, user_input: str, trace_id: str,
                           parent_span_id: str | None):
    """Run the agent under an OTEL span parented to a REMOTE (coordinator) span.

    Builds a non-recording remote SpanContext from the caller's hex trace_id /
    parent_span_id and attaches it, so the agent's spans join that trace and nest
    under the coordinator's agent-i span. Falls back to a plain run if the ids are
    malformed — tracing must never break the answer.
    """
    from opentelemetry import context as _c
    from opentelemetry import trace as _t
    from opentelemetry.trace import (
        NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context,
    )

    try:
        tid = int(trace_id, 16)
        sid = int(parent_span_id, 16) if parent_span_id else 0
    except (TypeError, ValueError):
        return agent(user_input)

    parent_ctx = set_span_in_context(NonRecordingSpan(SpanContext(
        trace_id=tid, span_id=sid, is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )))
    token = _c.attach(parent_ctx)
    try:
        tracer = _t.get_tracer("nyc-taxi-agent")
        with tracer.start_as_current_span("strands-agent"):
            return agent(user_input)
    finally:
        _c.detach(token)
