"""AWS Lambda MicroVMs lifecycle hook server (reusable).

This module is the extracted, app-agnostic piece of the MicroVM adaptation: a
tiny Starlette app that serves the six lifecycle hooks Lambda MicroVMs calls,
on the dedicated hooks port (default 9000), under the platform path prefix
``/aws/lambda-microvms/runtime/v1``.

Why each hook matters (from the aws-lambda-microvms skill):

  * ``/ready``     (build-time) — return 200 *only once the app is fully warm*,
                   so the platform snapshots a hot process. Return 503 to make
                   the platform keep polling. This is the whole game for chDB:
                   gate the snapshot until the embedded ClickHouse store is
                   loaded, so the first real query after RunMicrovm is warm.
  * ``/validate``  (build-time) — run mock payloads so the platform can sample
                   which snapshot pages are touched and prefetch them on future
                   launches. Return 200 when valid, 503 to ask for more time.
  * ``/run``       (runtime) — fires once after run-from-snapshot.
  * ``/resume``    (runtime) — fires on SUSPENDED -> RUNNING.
  * ``/suspend``   (runtime) — fires before RUNNING -> SUSPENDED (checkpoint here).
  * ``/terminate`` (runtime) — fires before termination (graceful close here).

Snapshot uniqueness (skill: snapshots-and-uniqueness): a snapshot shares memory
state across every MicroVM launched from it, so any RNG seeded *before* the
snapshot is shared. ``default_reseed`` reseeds Python's ``random`` and returns a
fresh per-instance boot id; wire it into ``on_run``/``on_resume``.

The module is deliberately framework-light and has no knowledge of chDB or the
taxi app — callers inject behavior via :class:`HookCallbacks`. That keeps it a
drop-in for any chDB-on-MicroVM workload.
"""
from __future__ import annotations

import logging
import os
import random
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

# Platform-defined hook path prefix and default hooks port (skill: getting-started).
HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1"
DEFAULT_HOOKS_PORT = 9000

# HTTP status the platform interprets as "ready/valid" vs "not yet, keep polling".
_OK = 200
_NOT_READY = 503


# Callback type aliases. ``warm`` and ``validate`` are predicates (True == proceed);
# the lifecycle notifications are side-effecting and return nothing.
GatePredicate = Callable[[], bool]
LifecycleHook = Callable[[], None]


def _noop() -> None:
    """Default lifecycle notification: do nothing."""


def _always_ready() -> bool:
    """Default gate: report ready immediately (overridden by real callers)."""
    return True


@dataclass(frozen=True)
class HookCallbacks:
    """Behavior injected into the hook server.

    Every field has a safe default so a minimal app can pass an empty
    ``HookCallbacks()`` and still get a valid (if trivial) hook surface.

    Attributes:
        warm: ``/ready`` predicate. Return True only when the app is fully
            booted and warm enough to snapshot; return False to be re-polled.
            Must not raise — exceptions are treated as "not ready".
        validate: ``/validate`` predicate. Return True when a snapshot-restored
            instance has been exercised and is serving correctly.
        on_run: ``/run`` notification (once, after run-from-snapshot).
        on_resume: ``/resume`` notification (SUSPENDED -> RUNNING).
        on_suspend: ``/suspend`` notification (checkpoint before suspend).
        on_terminate: ``/terminate`` notification (graceful shutdown).
    """

    warm: GatePredicate = _always_ready
    validate: GatePredicate = _always_ready
    on_run: LifecycleHook = _noop
    on_resume: LifecycleHook = _noop
    on_suspend: LifecycleHook = _noop
    on_terminate: LifecycleHook = _noop


def default_reseed() -> str:
    """Reseed process RNG and return a fresh per-instance boot id.

    Call from ``on_run``/``on_resume`` to defend against the snapshot-uniqueness
    pitfall: a snapshot freezes memory, so a ``random`` instance seeded before
    the snapshot would otherwise emit identical sequences across every MicroVM.
    ``random.seed()`` with no argument reseeds from ``os.urandom`` (per-instance).
    ``uuid.uuid4`` already reads ``os.urandom`` per call, so identifiers are safe;
    this is belt-and-suspenders for any module-level ``random`` use.
    """
    random.seed()
    boot_id = uuid.uuid4().hex
    logger.info("microvm reseed: new boot_id=%s", boot_id)
    return boot_id


def _gate_route(name: str, predicate: GatePredicate) -> Callable:
    """Build a build-time gate handler (``/ready`` or ``/validate``).

    Returns 200 when the predicate is truthy, else 503 so the platform keeps
    polling. The predicate is memoized after its first success: once ready, the
    platform may poll repeatedly, and re-running an expensive warm check each
    time is wasteful (and could flap).
    """
    state = {"ok": False}

    async def handler(_request: Request) -> JSONResponse:
        if state["ok"]:
            return JSONResponse({"hook": name, "status": "ok", "cached": True}, status_code=_OK)
        try:
            ready = bool(predicate())
        except Exception as exc:  # noqa: BLE001 — a failing probe means "not ready"
            logger.warning("microvm %s gate raised, reporting not-ready: %s", name, exc)
            return JSONResponse(
                {"hook": name, "status": "not_ready", "error": str(exc)[:200]},
                status_code=_NOT_READY,
            )
        if ready:
            state["ok"] = True
            logger.info("microvm %s gate: ready", name)
            return JSONResponse({"hook": name, "status": "ok"}, status_code=_OK)
        return JSONResponse({"hook": name, "status": "not_ready"}, status_code=_NOT_READY)

    return handler


def _notify_route(name: str, hook: LifecycleHook) -> Callable:
    """Build a runtime notification handler (run/resume/suspend/terminate).

    These hooks are fast-notification only (1-60s budget). We always return 200
    and never let an exception escape — a failed notification must not wedge a
    lifecycle transition.
    """

    async def handler(_request: Request) -> JSONResponse:
        try:
            hook()
            return JSONResponse({"hook": name, "status": "ok"}, status_code=_OK)
        except Exception as exc:  # noqa: BLE001 — best-effort notification
            logger.warning("microvm %s hook raised (ignored): %s", name, exc)
            return JSONResponse(
                {"hook": name, "status": "error", "error": str(exc)[:200]}, status_code=_OK
            )

    return handler


def create_hooks_app(callbacks: Optional[HookCallbacks] = None) -> Starlette:
    """Create the Starlette app that serves the six MicroVM lifecycle hooks.

    All routes are POST under :data:`HOOK_PREFIX`. A GET ``/health`` is also
    exposed for local probing / the lifecycle emulator.

    Args:
        callbacks: Behavior to inject. Defaults to a no-op :class:`HookCallbacks`.

    Returns:
        A Starlette ASGI app. Bind it to :data:`DEFAULT_HOOKS_PORT` (9000) unless
        you overrode the port at image-creation time.
    """
    cb = callbacks or HookCallbacks()

    routes = [
        Route(f"{HOOK_PREFIX}/ready", _gate_route("ready", cb.warm), methods=["POST"]),
        Route(f"{HOOK_PREFIX}/validate", _gate_route("validate", cb.validate), methods=["POST"]),
        Route(f"{HOOK_PREFIX}/run", _notify_route("run", cb.on_run), methods=["POST"]),
        Route(f"{HOOK_PREFIX}/resume", _notify_route("resume", cb.on_resume), methods=["POST"]),
        Route(f"{HOOK_PREFIX}/suspend", _notify_route("suspend", cb.on_suspend), methods=["POST"]),
        Route(f"{HOOK_PREFIX}/terminate", _notify_route("terminate", cb.on_terminate), methods=["POST"]),
        Route("/health", lambda _r: JSONResponse({"status": "hooks-alive"}), methods=["GET"]),
    ]
    return Starlette(routes=routes)


def hooks_port() -> int:
    """Resolve the hooks port from ``MICROVM_HOOKS_PORT`` (default 9000)."""
    return int(os.getenv("MICROVM_HOOKS_PORT", str(DEFAULT_HOOKS_PORT)))
