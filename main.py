"""NYC Taxi Analytics Agent — FastAPI application.

Seven endpoints:
    GET  /                Serve static/index.html
    POST /chat            Sync chat → JSON response
    POST /chat/stream     SSE streaming chat
    GET  /health          Deep health check (chDB row count)
    GET  /ping            AgentCore platform health probe (fast, no I/O)
    POST /invocations     AgentCore Runtime entry point
    GET  /info            API metadata

Key design rules:
  - /ping MUST be zero-I/O; /health MAY query chDB. Do not conflate them.
  - FastAPI(lifespan=lifespan), NOT @app.on_event (deprecated ≥ 0.93).
  - AnalyticalMemory is constructed ONLY inside this module; every caller sees DualMemory.
  - SSE wire format uses {"type":"text","content":X}. The agent yields
    {"type":"token","text":X} internally — main.py translates at the stream boundary.
  - /invocations in DEV/TST with trace_id routes through run_agent_with_tracing
    using Langfuse v4 key 'parent_span_id' (never 'parent_observation_id').
  - Transcribe-within-synthesize islands: lifespan,
    /chat and /invocations handler bodies are transcribed verbatim
    from the spec's partial python blocks, then extended only where requires.
"""
from __future__ import annotations
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.base import BaseHTTPMiddleware

from db import query_records
from memory import AnalyticalMemory
from agent_memory import DualMemory
from agent import chat_with_agent, stream_chat_with_agent, run_agent_with_tracing
from observability import init_tracing
from timing import init_timing


# FIX-APP-LOGGING: explicit stdout handler for ADOT forwarder capture.
# Without this, app-level loggers (main/observability/agent_memory) fall back to
# Python's lastResort handler which emits WARNING+ only to stderr — leaving
# INFO-level events (including init_tracing success lines) silently dropped and
# the otel-rt-logs canonical stream empty. M-1 fix.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)


# --- Pydantic models --

class ChatRequest(BaseModel):
    text: str = Field(..., max_length=10000)


class ChatResponse(BaseModel):
    response: str
    request_id: str
    timings: Optional[list[dict]] = None


class InvocationRequest(BaseModel):
    prompt: Optional[str] = None
    text: Optional[str] = None  # AgentCore sends either field
    # Langfuse v4 distributed tracing (DEV/TST only).
    # Use 'parent_span_id' — never 'parent_observation_id' (v2/v3 key, silently ignored by v4).
    trace_id: Optional[str] = None
    parent_span_id: Optional[str] = None


# --- Per-request memory builder ---------------------------------------------

def _build_memory(request: Request, *, default_user: str = "anonymous") -> DualMemory:
    """Construct a per-request DualMemory wrapping a fresh AnalyticalMemory.

    This is the sole AnalyticalMemory instantiation site in the codebase
. Called by /chat, /chat/stream, and /invocations —
    never at module scope.
    """
    user_id = request.headers.get("X-User-ID", default_user)
    session_id = request.headers.get("X-Session-ID", str(uuid.uuid4()))
    session_memory = AnalyticalMemory()
    return DualMemory(session_memory, user_id=user_id, session_id=session_id)


# --- Middleware -------------------------------------------------------------

class RequestIDMiddleware(BaseHTTPMiddleware):
    """Inject X-Request-ID on every request/response (UUID4)."""

    async def dispatch(self, request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# --- Rate limiter ------------------------------------------------------------

_limiter = Limiter(key_func=get_remote_address)


# --- Lifespan -------------

def _warm_chdb() -> None:
    """Pre-load the embedded chDB store through the agent's own DataStore path.

    chDB's embedded server is process-global and allows one connection per
    process; the first table load is what triggers the rare
    `recursive_mutex lock failed (ASYNC_LOAD_WAIT_FAILED)` race. Loading the
    table once at startup (single-threaded, before any request) means the first
    real tool call finds it already loaded — so the agent never answers from a
    failed first query. Best-effort: never block startup. (The Lambda MicroVMs
    path does the equivalent in its `/ready` hook.)
    """
    try:
        from query_helpers import execute_query_pipeline

        execute_query_pipeline("", "", "", "", False, 1)
        logger.info("chDB store warmed at startup")
    except Exception as e:  # noqa: BLE001 — warm-up must never break startup
        logger.warning("chDB warm-up skipped: %s", e)

    # Also pre-load the agent_state memory tables. Streaming runs tools on a
    # threadpool while the memory layer loads these on the request thread; if
    # they first load concurrently, the process-global embedded server can hit
    # `recursive_mutex lock failed (ASYNC_LOAD_WAIT_FAILED)`. Loading each once
    # here (single-threaded) means the first real request finds them ready.
    for table in ("agent_state.conversations", "agent_state.analysis_log"):
        try:
            query_records(f"SELECT 1 FROM {table} LIMIT 1")
        except Exception as e:  # noqa: BLE001 — best-effort; never break startup
            logger.warning("memory-table warm-up skipped (%s): %s", table, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize Langfuse OTEL exporter (from observability.py, Section 11.4)
    init_tracing()
    # Warm chDB so the first analytical query is served from a loaded store.
    _warm_chdb()
    yield
    # Shutdown: StrandsTelemetry registers its own atexit flush — nothing to do here


app = FastAPI(lifespan=lifespan)  # Do NOT use @app.on_event — deprecated in FastAPI ≥ 0.93

# Rate limiter wiring (standard slowapi integration).
app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Request-ID middleware (after SlowAPI so rate-limit responses also get the header).
app.add_middleware(RequestIDMiddleware)

# CORS — dev allows localhost; prod disables.
if os.getenv("IS_PROD", "false").lower() != "true":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


# --- Endpoints ---------------------------------------------------------------

@app.get("/")
async def index() -> FileResponse:
    """Serve the chat UI."""
    return FileResponse("static/index.html")


@app.get("/ping")
async def ping() -> dict:
    """AgentCore platform health probe. Zero I/O. Always returns 'Healthy'.

    Note: 'HealthyBusy' was considered, but there is no busy-state detection
    mechanism and the root contract only requires 'Healthy'.
    """
    return {"status": "Healthy"}


@app.get("/health")
async def health() -> JSONResponse:
    """Deep health check: chDB row count."""
    try:
        rows = query_records("SELECT count() as cnt FROM nyc_taxi.yellow_trips")
        count = int(rows[0]["cnt"]) if rows else 0
        return JSONResponse({"status": "healthy", "row_count": count})
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error": str(e)},
        )


@app.post("/chat")
@_limiter.limit("10/minute")
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    init_timing()
    memory = _build_memory(request)
    result = chat_with_agent(body.text, memory)
    return ChatResponse(
        response=result["response"],
        request_id=getattr(request.state, "request_id", str(uuid.uuid4())),
        timings=result.get("timings"),
    )


async def _event_generator(
    text: str, memory: DualMemory
) -> AsyncGenerator[str, None]:
    """Translate agent stream events to the SSE wire format.

    Agent yields {"type":"token","text":X}; wire format is {"type":"text","content":X}.
    See and test_chat_stream_translates_token_events_to_wire_text_events.
    """
    try:
        async for event in stream_chat_with_agent(text, memory):
            kind = event.get("type")
            if kind == "token":
                payload = {"type": "text", "content": event.get("text", "")}
                yield json.dumps(payload)
            elif kind == "done":
                payload = {"type": "done"}
                if "timings" in event:
                    payload["timings"] = event["timings"]
                if "metrics" in event:
                    payload["metrics"] = event["metrics"]
                yield json.dumps(payload)
            else:
                # Pass-through for tool/timing/error event types as defines them.
                yield json.dumps(event)
    except Exception as e:
        yield json.dumps({"type": "error", "message": str(e)})


@app.post("/chat/stream")
@_limiter.limit("10/minute")
async def chat_stream(request: Request, body: ChatRequest) -> EventSourceResponse:
    init_timing()
    memory = _build_memory(request)
    return EventSourceResponse(_event_generator(body.text, memory))


@app.get("/metrics/{trace_id}")
async def get_trace_metrics(trace_id: str) -> dict:
    """Fetch Langfuse cost + scores for a given trace_id.

    Called by the UI after the SSE 'done' event lands, to surface cost-USD and
    any factuality/safety scores in the response footer. Returns empty fields
    if Langfuse is unreachable or the trace isn't ingested yet — never blocks
    the chat stream itself.
    """
    try:
        from langfuse import get_client as _lf_get_client
        lf = _lf_get_client()
        # Langfuse v4 SDK: api.trace.get returns trace with calculatedTotalCost + scores.
        trace = lf.api.trace.get(trace_id)
        cost = getattr(trace, "totalCost", None) or getattr(trace, "calculatedTotalCost", None)
        scores_attr = getattr(trace, "scores", None) or []
        scores = [
            {"name": getattr(s, "name", "?"), "value": getattr(s, "value", None)}
            for s in scores_attr
        ]
        return {"trace_id": trace_id, "cost_usd": cost, "scores": scores, "available": True}
    except Exception as e:
        return {"trace_id": trace_id, "cost_usd": None, "scores": [], "available": False, "error": str(e)[:120]}


@app.post("/invocations")
@_limiter.limit("10/minute")
async def invocations(request: Request, body: InvocationRequest) -> ChatResponse:
    """AgentCore Runtime entry point. Accepts prompt or text; routes to tracing
    in DEV/TST when trace_id is supplied (Langfuse v4 parent_span_id key)."""
    init_timing()
    user_input = body.prompt or body.text or ""

    env = os.getenv("LANGFUSE_TRACING_ENVIRONMENT", "PRD")
    if env in ("DEV", "TST") and body.trace_id:
        response_text = run_agent_with_tracing(
            user_input,
            trace_id=body.trace_id,
            parent_span_id=body.parent_span_id,
        )
        return ChatResponse(
            response=response_text,
            request_id=getattr(request.state, "request_id", str(uuid.uuid4())),
            timings=None,
        )

    memory = _build_memory(request, default_user="agentcore")
    result = chat_with_agent(user_input, memory)
    return ChatResponse(
        response=result["response"],
        request_id=getattr(request.state, "request_id", str(uuid.uuid4())),
        timings=result.get("timings"),
    )


@app.get("/info")
async def info() -> dict:
    """Static API metadata per schema."""
    return {
        "name": "NYC Taxi Analytics Agent",
        "version": "2.0.0",
        "architecture": "3-layer hybrid (baked + delta + S3 Files weather)",
        "endpoints": ["/chat", "/chat/stream", "/health", "/ping", "/invocations"],
    }


# --- StaticFiles mount (after all explicit routes) --------------------------

app.mount("/static", StaticFiles(directory="static"), name="static")
