#!/usr/bin/env python3
"""scripts/scan_console.py — live browser view of the serverless distributed scan.

Serves a single-page console at http://localhost:PORT that launches N Firecracker
microVMs (each a private chDB), suspends the fleet to $0, resumes it snapshot-hot,
and scatters shards of the S3 cold lake at every VM's /scan — streaming each VM's
progress into a live grid with a throughput + real-cost headline and the merged
tip-by-year answer.

Numbers shown are a single illustrative run (no warm-up/repeats) — the UI says so.
The browser only watches; this server orchestrates and holds the auth tokens.
Teardown is guaranteed (fleet_core terminates in finally) with an atexit/signal
backstop. Prerequisites: the deployed image + a staged lake (scripts/stage_lake.py).

Usage:
  python scripts/scan_console.py --region us-west-2      # then open http://localhost:8080
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
_SCAN_HTML = os.path.join(_ROOT, "static", "scan.html")

_live_lock = threading.Lock()
_live: dict[str, str] = {}
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
        except Exception:  # noqa: BLE001
            pass


atexit.register(_terminate_registered)


def _install_signal_handlers() -> None:
    def _handler(*_a):
        _terminate_registered()
        os._exit(0)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


def build_app(default_region: str, default_name: str) -> FastAPI:
    app = FastAPI()

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_SCAN_HTML)

    @app.get("/config")
    async def config() -> JSONResponse:
        return JSONResponse({
            "defaultRegion": default_region,
            "defaultName": default_name,
            "maxFleet": fc.MAX_FLEET,
            "defaultDataset": fc.DEFAULT_SCAN_DATASET,
            "datasets": [{"key": k, "label": v["label"], "rows": v["rows_hint"],
                          "answer": v["answer"]} for k, v in fc.SCAN_DATASETS.items()],
        })

    @app.get("/run")
    async def run(count: int = 20, region: str | None = None,
                  name: str | None = None, keep: bool = False,
                  dataset: str = fc.DEFAULT_SCAN_DATASET) -> StreamingResponse:
        region = region or default_region
        name = name or default_name
        n = fc.clamp_count(count)

        if not _run_gate.acquire(blocking=False):
            async def busy():
                yield "data: " + json.dumps(
                    {"type": "error", "message": "a scan is already in progress"}) + "\n\n"
            return StreamingResponse(busy(), media_type="text/event-stream")

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(ev: dict) -> None:
            if not keep and ev.get("type") == "launch":
                _register([v["id"] for v in ev["vms"]], region)
            if ev.get("type") == "terminated":
                _drop(ev.get("ids", []))
            loop.call_soon_threadsafe(queue.put_nowait, ev)

        def worker() -> None:
            try:
                acct = fc.account(region)
                bucket = f"nyc-taxi-microvm-artifacts-{acct}-{region}"
                fc.run_scan_fleet_blocking(n, region, name, dataset=dataset,
                                           lake_bucket=bucket, on_event=emit,
                                           keep=keep, burst=True)
            except Exception as e:  # noqa: BLE001
                loop.call_soon_threadsafe(
                    queue.put_nowait, {"type": "error", "message": str(e)})
            finally:
                _run_gate.release()
                loop.call_soon_threadsafe(queue.put_nowait, {"type": "_end"})

        threading.Thread(target=worker, daemon=True).start()

        async def gen():
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
    ap = argparse.ArgumentParser(description="Live console for the serverless distributed scan")
    ap.add_argument("--region", default=fc.DEFAULT_REGION)
    ap.add_argument("--name", default=fc.DEFAULT_NAME)
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    _install_signal_handlers()
    app = build_app(args.region, args.name)
    print(f"scan console → region {args.region}, image {args.name}")
    print(f"open http://localhost:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
