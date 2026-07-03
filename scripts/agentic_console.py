#!/usr/bin/env python3
"""scripts/agentic_console.py — live browser view of the agentic fan-out fleet.

A third fan-out pattern, distinct from the two existing consoles:
  • fleet_console  — SAME question at every VM → consensus (snapshot fidelity)
  • scan_console   — split the DATA (file shards) → scatter/gather merge
  • agentic_console (this) — split the QUESTION. A coordinator decomposes ONE
    high-level question into distinct SUB-questions, fires a *different* one at
    each MicroVM's agent (each holds the same complete private chDB, so each
    answers a different facet correctly), then synthesizes one briefing.

The page streams plan → answer(per sub-question) → synthesis. As with the other
consoles the browser only *watches*: this server orchestrates the fleet and holds
the per-VM auth tokens, so no credentials reach the page. Teardown is guaranteed —
fleet_core terminates the fleet in a `finally`, and an atexit + signal backstop
terminates anything still registered if the server is killed mid-run.

Usage:
  python scripts/agentic_console.py --region us-west-2   # then open http://localhost:8080
  python scripts/agentic_console.py --port 8096
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
import fleet_tracing as ft  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENTIC_HTML = os.path.join(_ROOT, "static", "agentic.html")

# Crash-safety registry: ids launched by a *non-keep* run, terminated on exit if
# the run didn't finish cleanly (e.g. the server was killed mid-fan-out).
_live_lock = threading.Lock()
_live: dict[str, str] = {}  # microvm_id -> region

# Only one fleet may run at a time — belt-and-braces against a stray second /run.
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


def build_app(default_region: str, default_name: str,
              model_id: str = fc.DEFAULT_COORDINATOR_MODEL) -> FastAPI:
    app = FastAPI()

    # Build the Langfuse tracer once (SSM creds + auth check). None → untraced,
    # and the fan-out runs exactly as before. The demo never hard-depends on it.
    tracer = ft.build_fleet_tracer()
    if tracer:
        print("Langfuse tracing ON — the fan-out stitches into one distributed trace")
    else:
        print("Langfuse tracing OFF (no /langfuse/* creds or SDK) — running untraced")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_AGENTIC_HTML)

    @app.get("/config")
    async def config() -> JSONResponse:
        return JSONResponse({
            "defaultRegion": default_region,
            "defaultName": default_name,
            "maxFleet": fc.MAX_FLEET,
            "question": fc.AGENTIC_DEFAULT_QUESTION,
            "tracing": tracer is not None,
        })

    @app.get("/run")
    async def run(count: int = 4, region: str | None = None,
                  name: str | None = None, keep: bool = False,
                  question: str | None = None) -> StreamingResponse:
        region = region or default_region
        name = name or default_name
        n = fc.clamp_count(count)
        q = (question or fc.AGENTIC_DEFAULT_QUESTION)[:500]

        # A custom (non-default) question is decomposed live by the coordinator
        # LLM; the default question uses the curated, always-green plan.
        planner = fc.bedrock_planner(region, model_id)
        synthesizer = fc.bedrock_synthesizer(region, model_id)

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
            # Enrich the terminal event with a clickable Langfuse trace URL.
            if ev.get("type") == "done" and ev.get("trace_id"):
                ev = {**ev, "trace_url": ft.trace_url(ev["trace_id"])}
            loop.call_soon_threadsafe(queue.put_nowait, ev)

        def worker() -> None:
            try:
                fc.run_agentic_fleet_blocking(
                    q, region, name, count=n, on_event=emit, keep=keep,
                    planner=planner, synthesizer=synthesizer, tracer=tracer)
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
    ap = argparse.ArgumentParser(description="Live browser console for the agentic fan-out fleet")
    ap.add_argument("--region", default=fc.DEFAULT_REGION)
    ap.add_argument("--name", default=fc.DEFAULT_NAME)
    ap.add_argument("--model-id", default=fc.DEFAULT_COORDINATOR_MODEL,
                    help="Bedrock model id for the coordinator (plan + synthesize)")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    _install_signal_handlers()
    app = build_app(args.region, args.name, args.model_id)
    print(f"agentic console → region {args.region}, image {args.name}")
    print(f"coordinator model → {args.model_id}")
    print(f"open http://localhost:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
