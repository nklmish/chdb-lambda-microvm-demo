"""Tests for chdb_memory — append-only agent memory with time-travel."""
from __future__ import annotations

import pytest

import chdb_memory
from chdb_memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(path=str(tmp_path / "mem"))
    yield s
    # Release the process-global chDB Session so it doesn't leak into other tests
    # (chDB embedded allows one connection per process).
    chdb_memory.close_session()


@pytest.mark.integration
def test_remember_then_recall(store):
    store.remember("build-tool", "this repo uses Poetry")
    current = {b.key: b.content for b in store.recall()}
    assert current["build-tool"] == "this repo uses Poetry"


@pytest.mark.integration
def test_revise_is_append_only_and_recall_returns_latest(store):
    v1 = store.remember("build-tool", "this repo uses Poetry")
    v2 = store.revise("build-tool", "this repo standardizes on uv")
    assert (v1, v2) == (1, 2)
    current = {b.key: b.content for b in store.recall()}
    assert current["build-tool"] == "this repo standardizes on uv"
    # History preserves the full evolution, not just the latest belief.
    hist = store.history("build-tool")
    assert [b.content for b in hist] == [
        "this repo uses Poetry",
        "this repo standardizes on uv",
    ]


@pytest.mark.integration
def test_time_travel_as_of(store):
    store.remember("build-tool", "this repo uses Poetry")          # v1
    store.revise("build-tool", "this repo standardizes on uv")     # v2
    as_of_v1 = {b.key: b.content for b in store.as_of(1)}
    assert as_of_v1["build-tool"] == "this repo uses Poetry"       # the past belief
    current = {b.key: b.content for b in store.recall()}
    assert current["build-tool"] == "this repo standardizes on uv"


@pytest.mark.integration
def test_forget_drops_from_recall_but_keeps_history(store):
    store.remember("temp-fact", "ephemeral note")
    store.forget("temp-fact")
    assert "temp-fact" not in {b.key for b in store.recall()}
    # The deletion is itself a row — history is intact and auditable.
    hist = store.history("temp-fact")
    assert len(hist) == 2 and hist[-1].is_deleted is True


@pytest.mark.integration
def test_recall_drops_deleted_without_resurrecting_prior_version(store):
    store.remember("k", "v1")
    store.revise("k", "v2")
    store.forget("k")
    # Filtering is_deleted before taking the latest version would wrongly
    # resurrect v2; recall() must order-then-filter.
    assert "k" not in {b.key for b in store.recall()}
