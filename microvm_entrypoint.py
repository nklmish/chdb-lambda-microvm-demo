"""MicroVM container entrypoint: run the app and the lifecycle hooks together.

Lambda MicroVMs serves application traffic on one port (default 8080, routed by
the proxy) and calls lifecycle hooks on a separate port (default 9000). We run
both ASGI apps as **two uvicorn servers inside a single Python process**, on one
event loop. That single-process design is deliberate and load-bearing:

  the ``/ready`` hook (served on :9000) warms the chDB store *in this process*,
  so when Lambda snapshots the VM the warmed engine state belongs to the same
  process that the app (:8080) serves from. The first real query after
  ``RunMicrovm`` is therefore warm — no chDB init, no store load.

Run it directly (``python microvm_entrypoint.py``) or, to keep OpenTelemetry
auto-instrumentation, ``opentelemetry-instrument python microvm_entrypoint.py``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("microvm_entrypoint")


def _app_port() -> int:
    """Application port the MicroVM proxy targets (default 8080)."""
    return int(os.getenv("PORT", "8080"))


def _build_servers() -> list[uvicorn.Server]:
    """Construct the app server (:8080) and the hooks server (:9000)."""
    from main import app as taxi_app  # FastAPI app (chat, /health, /invocations)
    from microvm_hooks import create_hooks_app, hooks_port
    from microvm_runtime import build_callbacks

    hooks_app = create_hooks_app(build_callbacks())

    app_cfg = uvicorn.Config(
        taxi_app, host="0.0.0.0", port=_app_port(), workers=1, log_level="info"  # noqa: S104
    )
    hooks_cfg = uvicorn.Config(
        hooks_app, host="0.0.0.0", port=hooks_port(), log_level="info"  # noqa: S104
    )
    return [uvicorn.Server(app_cfg), uvicorn.Server(hooks_cfg)]


def _install_unified_signals(servers: list[uvicorn.Server]) -> None:
    """Install one signal handler that asks *both* servers to exit.

    uvicorn would otherwise have each server install its own handler for
    SIGINT/SIGTERM; the second clobbers the first, so on shutdown only one server
    stops. We disable uvicorn's per-server handlers and install a single handler
    that flips ``should_exit`` on every server.
    """
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        logger.info("shutdown signal received; stopping app + hooks servers")
        for server in servers:
            server.should_exit = True

    for server in servers:
        server.install_signal_handlers = lambda: None  # type: ignore[assignment]

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # pragma: no cover — non-Unix fallback
            signal.signal(sig, lambda *_: _request_stop())


async def _serve() -> None:
    servers = _build_servers()
    _install_unified_signals(servers)
    logger.info(
        "starting MicroVM entrypoint: app on :%s, hooks on :%s",
        _app_port(),
        int(os.getenv("MICROVM_HOOKS_PORT", "9000")),
    )
    await asyncio.gather(*(server.serve() for server in servers))


if __name__ == "__main__":
    asyncio.run(_serve())
