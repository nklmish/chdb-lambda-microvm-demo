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
import base64
import json
import logging
import os
from strands.telemetry import StrandsTelemetry

logger = logging.getLogger(__name__)

_initialized = False
_langfuse_runtime_configured = False


def build_langfuse_otel_env(host: str, public_key: str, secret_key: str) -> dict:
    """The OTEL + LANGFUSE_* env that turns on trace export to Langfuse Cloud.

    Mirrors the proven EC2 mount-demo recipe: a generic OTLP endpoint (base path —
    the OTLP SDK appends /v1/traces) and a Basic-auth header carrying the ingestion
    version. Pure/side-effect-free so it is unit-testable.
    """
    host = host.rstrip("/")
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return {
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"{host}/api/public/otel",
        "OTEL_EXPORTER_OTLP_HEADERS": (
            f"Authorization=Basic {auth},x-langfuse-ingestion-version=4"),
        "OTEL_TRACES_EXPORTER": "otlp",
        "LANGFUSE_HOST": host,
        "LANGFUSE_PUBLIC_KEY": public_key,
        "LANGFUSE_SECRET_KEY": secret_key,
    }


def configure_langfuse_runtime(region: str | None = None) -> bool:
    """Post-resume (runtime) Langfuse setup for Lambda MicroVMs. Idempotent.

    Lambda MicroVMs snapshots the container at BUILD time and *resumes* it at
    run-microvm, so the image CMD runs under the build role — which must not hold
    prod secrets. Tracing is therefore stood up here instead: in the run/resume
    lifecycle hook (and, as a backstop, on the first traced request), which run
    in-process under the *execution* role. We resolve /langfuse/* from SSM, set the
    OTEL/LANGFUSE env, and attach an OTLP span processor to the *existing* global
    tracer provider (StrandsTelemetry would try to install a new global provider,
    which OTEL blocks when opentelemetry-instrument already set one). No secret is
    ever baked into the image or the snapshot's code artifact.

    Returns True once tracing is configured; best-effort — never raises.
    """
    global _langfuse_runtime_configured
    if _langfuse_runtime_configured:
        return True
    if os.getenv("LANGFUSE_RESOLVE_FROM_SSM", "").lower() != "true":
        return False
    region = region or os.getenv("LANGFUSE_SSM_REGION", "us-east-1")
    try:
        import boto3

        ssm = boto3.client("ssm", region_name=region)
        host = ssm.get_parameter(Name="/langfuse/LANGFUSE_HOST")["Parameter"]["Value"]
        pk = ssm.get_parameter(Name="/langfuse/LANGFUSE_PUBLIC_KEY")["Parameter"]["Value"]
        sk = ssm.get_parameter(
            Name="/langfuse/LANGFUSE_SECRET_KEY", WithDecryption=True)["Parameter"]["Value"]
    except Exception as e:  # noqa: BLE001 — no creds/AWS → stay untraced
        logger.warning("langfuse runtime resolve failed (%s); tracing off", e)
        return False

    for k, v in build_langfuse_otel_env(host, pk, sk).items():
        os.environ[k] = v

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = trace.get_tracer_provider()
        if not hasattr(provider, "add_span_processor"):
            # No SDK provider yet (e.g. a proxy) — install one now.
            provider = TracerProvider()
            trace.set_tracer_provider(provider)
        # OTLPSpanExporter reads OTEL_EXPORTER_OTLP_ENDPOINT/HEADERS we just set.
        # This single processor on the global provider is the ONLY export path on
        # the worker — the agent spans are linked to the caller's trace via explicit
        # OTEL remote-parent context (agent.run_agent_with_tracing), not the Langfuse
        # SDK, so there is exactly one export and no duplicate spans.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        _langfuse_runtime_configured = True
        logger.info("langfuse runtime tracing configured (region=%s)", region)
        return True
    except Exception as e:  # noqa: BLE001 — never break the agent on telemetry setup
        logger.warning("langfuse runtime exporter setup failed: %s", e)
        return False


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
