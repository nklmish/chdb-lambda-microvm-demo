"""Taxi-agent-specific wiring for the MicroVM lifecycle hooks.

This binds the generic :mod:`microvm_hooks` server to *this* app's chDB store:

  * **warm** (``/ready``) loads the embedded ClickHouse store into the running
    process (a ``count()`` over the baked ``yellow_trips`` table) and touches the
    federation session, so the snapshot Lambda captures is *hot*. The first real
    query after ``RunMicrovm`` is then served warm — no engine init, no store
    load. That is the headline MicroVM win (Demo 1).
  * **validate** (``/validate``) runs a representative analytical aggregate
    (busiest-hour) so the platform samples and prefetches exactly the snapshot
    pages a real query touches.
  * **suspend/resume** keep the per-session chDB "agent brain" honest across the
    suspend/resume cycle (Demo 2): the federation cache is a MergeTree on the
    VM's persistent disk, so it survives suspend/resume for free; we just
    checkpoint and reseed RNG on the way back in.

Kept separate from :mod:`microvm_entrypoint` so the warm/validate logic is unit
testable without binding sockets.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from microvm_hooks import HookCallbacks, default_reseed

logger = logging.getLogger(__name__)

# The baked table the agent reads on the hot path (matches db.py / federation_tools).
_TAXI_TABLE = "nyc_taxi.yellow_trips"

# Last-observed warm metrics, surfaced for the lifecycle emulator / logs.
_metrics: dict[str, object] = {"warm_rows": 0, "warm_ms": None, "validate_ms": None}


def warm_metrics() -> dict[str, object]:
    """Return a copy of the most recent warm/validate timing metrics."""
    return dict(_metrics)


def _warm() -> bool:
    """Load the chDB store into this process; report ready when rows are present.

    Uses the same stateless ``chdb.query(path=...)`` access path the app uses on
    the hot path, so the pages this touches are exactly the ones the snapshot
    should capture. Also touches the long-lived federation session so its
    MergeTree store is mapped into the process before the snapshot.
    """
    from db import query_records  # local import: avoid loading chDB at module import

    t0 = time.time()
    rows = query_records(f"SELECT count() AS c FROM {_TAXI_TABLE}")
    count = int(rows[0]["c"]) if rows else 0
    _metrics["warm_ms"] = round((time.time() - t0) * 1000, 1)
    _metrics["warm_rows"] = count

    # Touch the federation session so the agent-brain MergeTree is hot too.
    try:
        import federation_tools

        federation_tools._ensure_cache_table()
    except Exception as exc:  # noqa: BLE001 — federation warm is best-effort
        logger.info("federation warm skipped: %s", exc)

    logger.info("microvm warm: %s rows in %s ms", count, _metrics["warm_ms"])
    return count > 0


def _validate() -> bool:
    """Exercise a representative aggregate so the platform prefetches hot pages."""
    from db import query_records

    t0 = time.time()
    rows = query_records(
        f"SELECT toHour(pickup_datetime) AS h, count() AS c "
        f"FROM {_TAXI_TABLE} GROUP BY h ORDER BY c DESC LIMIT 1"
    )
    _metrics["validate_ms"] = round((time.time() - t0) * 1000, 1)
    ok = bool(rows)
    logger.info("microvm validate: busiest-hour aggregate ok=%s in %s ms", ok, _metrics["validate_ms"])
    return ok


def _configure_tracing() -> None:
    """Stand up Langfuse tracing post-resume, under the exec role (best-effort).

    Must happen here (run/resume hook), NOT at container boot: Lambda MicroVMs runs
    the image CMD at build time under the build role and snapshots the result, so
    the exec-role SSM lookup for /langfuse/* only works from a runtime hook.
    """
    try:
        from observability import configure_langfuse_runtime

        configure_langfuse_runtime()
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the run hook
        logger.info("langfuse runtime config skipped: %s", exc)


def _on_run() -> None:
    """Run-from-snapshot: reseed RNG, then configure Langfuse tracing (exec role)."""
    default_reseed()
    _configure_tracing()
    logger.info("microvm run hook fired (fresh instance from snapshot)")


def _on_resume() -> None:
    """SUSPENDED -> RUNNING: reseed, (re)configure tracing, note the resume.

    The chDB store (including the federation cache MergeTree) lives on the VM's
    persistent disk and survives suspend/resume untouched, so there is nothing to
    reload here — the agent brain is already warm. We reseed RNG and ensure tracing
    is configured (idempotent).
    """
    default_reseed()
    _configure_tracing()
    logger.info("microvm resume hook fired (state restored from suspend)")


def _on_suspend() -> None:
    """RUNNING -> SUSPENDED: checkpoint before the VM is frozen.

    chDB MergeTree writes are already durable on disk (parts are fsync'd on
    insert), so the federation cache is safe without an explicit flush. This is a
    marker/log point; extend it if you add volatile in-memory state.
    """
    logger.info("microvm suspend hook fired (chDB MergeTree already durable on disk)")


def _on_terminate() -> None:
    """Graceful shutdown notification."""
    logger.info("microvm terminate hook fired")


def build_callbacks() -> HookCallbacks:
    """Build the :class:`HookCallbacks` for the taxi agent."""
    return HookCallbacks(
        warm=_warm,
        validate=_validate,
        on_run=_on_run,
        on_resume=_on_resume,
        on_suspend=_on_suspend,
        on_terminate=_on_terminate,
    )
