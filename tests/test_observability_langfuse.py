"""tests/test_observability_langfuse.py — Langfuse runtime env-builder (offline).

The MicroVM configures Langfuse trace export at runtime (run/resume hook →
observability.configure_langfuse_runtime); here we lock the pure env-builder that
turns SSM creds into the OTLP endpoint + Basic-auth header.
"""
import base64

import observability as obs


def test_build_langfuse_otel_env_matches_langfuse_contract():
    env = obs.build_langfuse_otel_env("https://cloud.langfuse.com/", "pk-lf-x", "sk-lf-y")
    # generic base endpoint (OTLP SDK appends /v1/traces) — mirrors the mount demo
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://cloud.langfuse.com/api/public/otel"
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
    expected_auth = base64.b64encode(b"pk-lf-x:sk-lf-y").decode()
    assert f"Authorization=Basic {expected_auth}" in env["OTEL_EXPORTER_OTLP_HEADERS"]
    assert "x-langfuse-ingestion-version=4" in env["OTEL_EXPORTER_OTLP_HEADERS"]
    assert env["LANGFUSE_HOST"] == "https://cloud.langfuse.com"   # trailing slash stripped
    assert env["LANGFUSE_PUBLIC_KEY"] == "pk-lf-x"
    assert env["LANGFUSE_SECRET_KEY"] == "sk-lf-y"


def test_configure_langfuse_runtime_noop_without_flag(monkeypatch):
    monkeypatch.setattr(obs, "_langfuse_runtime_configured", False)
    monkeypatch.delenv("LANGFUSE_RESOLVE_FROM_SSM", raising=False)
    # No flag → returns False, never touches AWS.
    assert obs.configure_langfuse_runtime() is False


def test_session_scope_nullcontext_without_id():
    # No session_id → a real no-op context manager (never raises, groups nothing).
    with obs.session_scope(None):
        pass
    with obs.session_scope("", "user"):
        pass


def test_session_scope_returns_usable_context_manager_with_id():
    # With an id, returns a usable context manager even if langfuse is unconfigured
    # (best-effort: falls back to nullcontext on any SDK error).
    with obs.session_scope("sess-1", "u"):
        pass
