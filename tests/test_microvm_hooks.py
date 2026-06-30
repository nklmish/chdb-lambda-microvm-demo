"""Tests for the MicroVM lifecycle hook server and taxi-app wiring.

Two layers:
  * unit  — the generic hook server (gating, memoization, error-swallowing) with
            injected fake callbacks; no chDB, no network.
  * integration — the real taxi warm/validate callbacks against a sample chDB
            store (the shared ``sample_db`` fixture from conftest).
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from microvm_hooks import (
    HOOK_PREFIX,
    HookCallbacks,
    create_hooks_app,
    default_reseed,
    hooks_port,
)


# ─── unit: generic hook server ───────────────────────────────────────────────


def _client(callbacks: HookCallbacks) -> TestClient:
    return TestClient(create_hooks_app(callbacks))


@pytest.mark.unit
def test_ready_returns_503_until_warm_then_200_and_memoizes():
    calls = {"n": 0}

    def warm() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2  # not ready on first poll, ready on second

    client = _client(HookCallbacks(warm=warm))
    url = f"{HOOK_PREFIX}/ready"

    assert client.post(url).status_code == 503  # first poll: not ready
    assert client.post(url).status_code == 200  # second poll: ready
    # Memoized: predicate not called again, still 200.
    assert client.post(url).status_code == 200
    assert calls["n"] == 2


@pytest.mark.unit
def test_ready_treats_exception_as_not_ready():
    def warm() -> bool:
        raise RuntimeError("store not loaded yet")

    resp = _client(HookCallbacks(warm=warm)).post(f"{HOOK_PREFIX}/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"


@pytest.mark.unit
def test_validate_gate_reports_ok():
    resp = _client(HookCallbacks(validate=lambda: True)).post(f"{HOOK_PREFIX}/validate")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.unit
@pytest.mark.parametrize("hook", ["run", "resume", "suspend", "terminate"])
def test_notification_hooks_fire_callback_and_return_200(hook):
    fired = {"run": False, "resume": False, "suspend": False, "terminate": False}
    callbacks = HookCallbacks(
        on_run=lambda: fired.__setitem__("run", True),
        on_resume=lambda: fired.__setitem__("resume", True),
        on_suspend=lambda: fired.__setitem__("suspend", True),
        on_terminate=lambda: fired.__setitem__("terminate", True),
    )
    resp = _client(callbacks).post(f"{HOOK_PREFIX}/{hook}")
    assert resp.status_code == 200
    assert fired[hook] is True


@pytest.mark.unit
def test_notification_hook_swallows_errors_and_still_returns_200():
    def boom() -> None:
        raise RuntimeError("checkpoint failed")

    resp = _client(HookCallbacks(on_suspend=boom)).post(f"{HOOK_PREFIX}/suspend")
    assert resp.status_code == 200  # a failed notification must not wedge lifecycle
    assert resp.json()["status"] == "error"


@pytest.mark.unit
def test_default_callbacks_give_valid_surface():
    client = _client(HookCallbacks())
    assert client.post(f"{HOOK_PREFIX}/ready").status_code == 200
    assert client.get("/health").json()["status"] == "hooks-alive"


@pytest.mark.unit
def test_default_reseed_returns_unique_hex():
    a, b = default_reseed(), default_reseed()
    assert len(a) == 32 and len(b) == 32
    assert a != b


@pytest.mark.unit
def test_hooks_port_default(monkeypatch):
    monkeypatch.delenv("MICROVM_HOOKS_PORT", raising=False)
    assert hooks_port() == 9000
    monkeypatch.setenv("MICROVM_HOOKS_PORT", "9999")
    assert hooks_port() == 9999


# ─── integration: real taxi warm/validate against a sample chDB store ─────────


@pytest.mark.integration
def test_taxi_warm_loads_store_and_reports_ready(sample_db, monkeypatch):
    monkeypatch.setenv("CHDB_DATA_PATH", sample_db)
    import importlib

    import db

    importlib.reload(db)  # pick up the patched CHDB_DATA_PATH
    import microvm_runtime

    assert microvm_runtime._warm() is True
    metrics = microvm_runtime.warm_metrics()
    assert metrics["warm_rows"] == 10  # sample_db inserts 10 rows
    assert metrics["warm_ms"] is not None


@pytest.mark.integration
def test_taxi_validate_runs_aggregate(sample_db, monkeypatch):
    monkeypatch.setenv("CHDB_DATA_PATH", sample_db)
    import importlib

    import db

    importlib.reload(db)
    import microvm_runtime

    assert microvm_runtime._validate() is True
    assert microvm_runtime.warm_metrics()["validate_ms"] is not None
