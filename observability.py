"""
observability.py — Langfuse tracing via Strands OTEL exporter.

Environment variables (set at AgentCore deploy time, NOT in code):
  OTEL_EXPORTER_OTLP_ENDPOINT — Langfuse OTEL endpoint
  OTEL_EXPORTER_OTLP_HEADERS  — Base64-encoded Langfuse auth
  DISABLE_ADOT_OBSERVABILITY   — Must be "true" to use Langfuse instead of ADOT

The OTEL exporter reads these env vars automatically.
No Langfuse SDK initialization needed in the container for PRD.

IMPORTANT — DEV/TST environments: Section 11.6 uses langfuse.get_client() for
distributed tracing, which requires LANGFUSE_SECRET_KEY, LANGFUSE_PUBLIC_KEY, and
LANGFUSE_HOST env vars. These are injected into the agent runtime's environmentVariables
map at CreateAgentRuntime time by scripts/create_runtime.py (boto3-direct;
no @aws/agentcore CLI involved). PRD uses OTEL-only tracing and does NOT need them.

: BotocoreInstrumentor response_hook registered in
init_tracing() to surface AgentCore Memory retrieval content as span attributes.
The OTEL auto-instrumentation for RetrieveMemoryRecords captures only RPC metadata
(memory.id, namespace, http.status_code) by design; the hook extends each span with
gen_ai.memory.retrieved_count and gen_ai.memory.retrieved_content so Langfuse shows
what the agent actually recalled. Wraps in try/except — never breaks the agent.
"""
import json
import logging
import os
from strands.telemetry import StrandsTelemetry

logger = logging.getLogger(__name__)

_initialized = False


def _agentcore_memory_response_hook(span, service_name, operation_name, result):
    """BotocoreInstrumentor response_hook: extend AgentCore Memory spans with payload.

    Intercepts RetrieveMemoryRecords and CreateEvent responses and adds
    gen_ai.memory.* attributes so Langfuse shows memory content, not just
    RPC metadata. Silent fallthrough on any error — observability must never
    break the agent.
    """
    logger.debug(
        "memory_response_hook fired: op=%s recording=%s",
        operation_name,
        span.is_recording() if span else "no-span",
    )
    if not span or not span.is_recording():
        return
    try:
        if operation_name == "RetrieveMemoryRecords":
            records = result.get("memoryRecordSummaries") or []
            span.set_attribute("gen_ai.memory.retrieved_count", len(records))
            if records:
                excerpts = []
                for r in records[:3]:
                    content = r.get("content", {})
                    text = content.get("text") if isinstance(content, dict) else str(content)
                    if text:
                        excerpts.append(text[:200])
                if excerpts:
                    span.set_attribute(
                        "gen_ai.memory.retrieved_content",
                        json.dumps(excerpts)[:500],
                    )
        elif operation_name == "CreateEvent":
            event = result.get("event") or {}
            event_id = event.get("eventId", "")
            if event_id:
                span.set_attribute("gen_ai.memory.stored_event_id", event_id)
    except Exception as e:
        logger.debug("memory_response_hook error: %s", e)
        pass  # Never break agent on instrumentation error


def _install_memory_response_hook() -> None:
    """Register BotocoreInstrumentor response_hook for AgentCore Memory spans.

    aws-opentelemetry-distro auto-instruments botocore at process startup.
    We uninstrument and re-instrument with our response_hook to capture
    memory retrieval content as span attributes.
    """
    try:
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
        instrumentor = BotocoreInstrumentor()
        # Uninstrument first (ADOT may have already instrumented without a hook)
        try:
            instrumentor.uninstrument()
            logger.info("BotocoreInstrumentor uninstrumented")
        except Exception as e_uninstr:
            logger.info("BotocoreInstrumentor uninstrument skipped: %s", e_uninstr)
        # Re-instrument with our response_hook
        try:
            instrumentor.instrument(response_hook=_agentcore_memory_response_hook)
            logger.info("BotocoreInstrumentor re-instrumented with memory response_hook")
        except Exception as e_instr:
            logger.warning("BotocoreInstrumentor re-instrument failed: %s", e_instr)
    except ImportError as e_import:
        logger.warning("opentelemetry-instrumentation-botocore not importable: %s", e_import)


def init_tracing() -> None:
    """Initialize OTEL tracing for Langfuse. Safe to call multiple times."""
    global _initialized
    if _initialized:
        return
    # Only stand up the OTLP exporter when there is somewhere to send traces.
    # Without this guard StrandsTelemetry defaults to http://localhost:4318 and
    # floods the logs with connection-refused errors on any machine that isn't
    # running a collector (i.e. every fresh local clone). Prod/DEV/TST set an
    # endpoint (via create_runtime.py); local clones don't, and stay quiet.
    traces_exporter = os.getenv("OTEL_TRACES_EXPORTER", "").lower()
    endpoint = (
        os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    )
    if traces_exporter == "none" or not endpoint:
        logger.info("OTEL trace export disabled (no OTLP endpoint configured)")
    else:
        try:
            telemetry = StrandsTelemetry()
            telemetry.setup_otlp_exporter()
            logger.info("Langfuse OTEL tracing initialized")
        except Exception as e:
            logger.warning("Failed to initialize OTEL tracing: %s", e)
            # Non-fatal: agent works without tracing
    # Install memory response_hook once. Must be inside the _initialized guard
    # to prevent multiple registrations (each call to init_tracing would add
    # another hook instance, causing duplicate attribute writes).
    _install_memory_response_hook()
    _initialized = True
