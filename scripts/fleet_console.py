#!/usr/bin/env python3
"""scripts/fleet_console.py — a live browser view of the MicroVM fleet fan-out.

Serves a single-page console at http://localhost:PORT that launches N Lambda
MicroVMs (each with its own private, snapshot-hot chDB), fires the SAME question
at all of them concurrently, and streams each VM's ready-time, latency, and
answer into a live grid — with a wall-clock headline and a consensus badge
(all N private stores, baked from one image, must return the same number).

The browser only *watches*: this server orchestrates the fleet and holds the
per-VM auth tokens, so no credentials ever reach the page. Teardown is
guaranteed — fleet_core terminates the fleet in a `finally`, and an atexit +
signal backstop terminates anything still registered if the server is killed
mid-run (the VMs' idle/max-duration policies are a final safety net).

Usage:
  python scripts/fleet_console.py --region us-west-2      # then open http://localhost:8080
  python scripts/fleet_console.py --port 8095
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
import signal
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet_core as fc  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FLEET_HTML = os.path.join(_ROOT, "static", "fleet.html")

# Crash-safety registry: ids launched by a *non-keep* run, terminated on exit if
# the run didn't finish cleanly (e.g. the server was killed mid-fan-out).
_live_lock = threading.Lock()
_live: dict[str, str] = {}  # microvm_id -> region

# Only one fleet may run at a time. Belt-and-braces against a client that opens
# a second /run (e.g. a stray reconnect) — without this, each concurrent /run
# would launch its own fleet and bill for it.
_run_gate = threading.Lock()


def _register(ids: list[str], region: str) -> None:
    with _live_lock:
        for i in ids:
            _live[i] = region


def _drop(ids: list[str]) -> None:
    with _live_lock:
        for i in ids:
            _live.pop(i, None)


def _terminate_registered() -> None:
    with _live_lock:
        items = list(_live.items())
        _live.clear()
    for vid, region in items:
        try:
            fc.terminate(vid, region)
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass


atexit.register(_terminate_registered)


def _install_signal_handlers() -> None:
    def _handler(*_a):
        _terminate_registered()
        os._exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # not in main thread / unsupported
            pass


def build_app(default_region: str, default_name: str) -> FastAPI:
    app = FastAPI()

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_FLEET_HTML)

    @app.get("/config")
    async def config() -> JSONResponse:
        return JSONResponse({
            "defaultRegion": default_region,
            "defaultName": default_name,
            "maxFleet": fc.MAX_FLEET,
            "question": fc.DEFAULT_QUESTION,
        })

    @app.get("/run")
    async def run(count: int = 5, region: str | None = None,
                  name: str | None = None, keep: bool = False,
                  question: str | None = None) -> StreamingResponse:
        region = region or default_region
        name = name or default_name
        n = fc.clamp_count(count)
        q = (question or fc.DEFAULT_QUESTION)[:500]

        # Refuse a second concurrent run rather than launch another fleet.
        if not _run_gate.acquire(blocking=False):
            async def busy():
                yield ("data: " + json.dumps(
                    {"type": "error", "message": "a fleet run is already in progress"}
                ) + "\n\n")
            return StreamingResponse(busy(), media_type="text/event-stream")

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(ev: dict) -> None:
            # Track ids for crash-safety cleanup only when we intend to reap them.
            if not keep and ev.get("type") == "launch":
                _register([v["id"] for v in ev["vms"]], region)
            if ev.get("type") == "terminated":
                _drop(ev.get("ids", []))
            loop.call_soon_threadsafe(queue.put_nowait, ev)

        def worker() -> None:
            try:
                fc.run_fleet_blocking(n, region, name, q, on_event=emit, keep=keep)
            finally:
                _run_gate.release()
                loop.call_soon_threadsafe(queue.put_nowait, {"type": "_end"})

        threading.Thread(target=worker, daemon=True).start()

        async def gen():
            # The worker owns the fleet lifecycle and always tears down in its
            # own finally, so a browser disconnect here never leaks VMs.
            while True:
                ev = await queue.get()
                if ev.get("type") == "_end":
                    break
                yield f"data: {json.dumps(ev)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/terminate")
    async def terminate_all() -> JSONResponse:
        with _live_lock:
            n = len(_live)
        _terminate_registered()
        return JSONResponse({"ok": True, "terminated": n})

    return app


def main() -> int:
    ap = argparse.ArgumentParser(description="Live browser console for the MicroVM fleet")
    ap.add_argument("--region", default=fc.DEFAULT_REGION)
    ap.add_argument("--name", default=fc.DEFAULT_NAME)
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    _install_signal_handlers()
    app = build_app(args.region, args.name)
    print(f"fleet console → region {args.region}, image {args.name}")
    print(f"open http://localhost:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
