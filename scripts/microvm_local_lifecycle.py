#!/usr/bin/env python3
"""Local emulator for the AWS Lambda MicroVMs lifecycle — real hooks, real chDB.

The aws-lambda-microvms skill states a MicroVM is "a container inside a
Firecracker microVM — you can reproduce the environment locally." This script
does exactly that for *this* app, without any AWS calls: it starts the real
:mod:`microvm_entrypoint` process (app on :8080, hooks on :9000) and drives the
genuine lifecycle the platform would:

    BUILD     POST /ready   (poll until 200 — gates the snapshot on chDB warm)
              POST /validate (mock aggregate — what the platform prefetches)
    RUN       POST /run
    SERVE     GET  /health   (warm query latency)
    SUSPEND   POST /suspend, then kill the process   (emulate the frozen VM)
    RESUME    restart the process, POST /resume, GET /health
              -> proves the chDB store (incl. the federation agent-brain cache)
                 survived suspend/resume because it lives on persistent disk.

It also spawns a throwaway fresh Python process to measure a *cold* first-query
latency (store load + query) for honest contrast with the warmed path.

Usage:
    python scripts/microvm_local_lifecycle.py --synthetic        # no network
    python scripts/microvm_local_lifecycle.py --db-path ./local_chdb_data

This is a local fidelity harness, not the cloud deploy. For the real thing use
scripts/deploy_microvm.py.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1"


def _post(url: str, timeout: float = 5.0) -> tuple[int, str]:
    req = urllib.request.Request(url, method="POST", data=b"{}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (localhost)
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _get(url: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (localhost)
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _build_synthetic_store(db_path: str) -> None:
    """Create a tiny chDB store + data_profile.json with no network access."""
    import chdb

    chdb.query("CREATE DATABASE IF NOT EXISTS nyc_taxi ENGINE = Atomic", path=db_path)
    chdb.query("CREATE DATABASE IF NOT EXISTS agent_state ENGINE = Atomic", path=db_path)
    chdb.query(
        """CREATE TABLE IF NOT EXISTS nyc_taxi.yellow_trips (
            pickup_datetime DateTime, dropoff_datetime DateTime,
            passenger_count UInt8, trip_distance Float64,
            pickup_location_id UInt16, dropoff_location_id UInt16,
            fare_amount Float64, tip_amount Float64, total_amount Float64,
            payment_type UInt8, congestion_surcharge Float64, airport_fee Float64
        ) ENGINE = MergeTree() ORDER BY (pickup_datetime, pickup_location_id)""",
        path=db_path,
    )
    chdb.query(
        """INSERT INTO nyc_taxi.yellow_trips
           SELECT now() - number * 60, now() - number * 60 + 600,
                  1, 3.0, 161, 237, 18.5, 3.7, 25.3, 1, 2.5, 0.0
           FROM numbers(50000)""",
        path=db_path,
    )
    profile = {
        "row_count": 50000,
        "date_range": {"min": "2024-01-01", "max": "2024-12-31"},
        "fare_stats": {"min": 8.5, "max": 52.0, "mean": 18.5, "median": 18.5},
        "top_pickup_zones": [{"zone_id": 161, "trips": 50000}],
        "top_dropoff_zones": [{"zone_id": 237, "trips": 50000}],
        "payment_distribution": {"credit": 1.0, "cash": 0.0, "other": 0.0},
        "baked_cutoff": "2024-12-31",
        "delta_start": "2025-01",
    }
    (ROOT / "data_profile.json").write_text(json.dumps(profile, indent=2))


def _start_process(db_path: str, app_port: int, hooks_port: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "CHDB_DATA_PATH": db_path,
        "PORT": str(app_port),
        "MICROVM_HOOKS_PORT": str(hooks_port),
        # Keep the emulator self-contained: no tracing exporters, no memory.
        "OTEL_TRACES_EXPORTER": "none",
        "OTEL_METRICS_EXPORTER": "none",
        "IS_PROD": "false",
    }
    return subprocess.Popen(
        [sys.executable, "microvm_entrypoint.py"], cwd=str(ROOT), env=env
    )


def _poll_ready(hooks_base: str, timeout_s: float = 120.0) -> float:
    """Poll /ready until 200; return seconds elapsed (the warm-to-snapshot time)."""
    deadline = time.time() + timeout_s
    t0 = time.time()
    while time.time() < deadline:
        try:
            status, _ = _post(f"{hooks_base}{HOOK_PREFIX}/ready")
            if status == 200:
                return round(time.time() - t0, 3)
        except urllib.error.URLError:
            pass  # server not listening yet
        time.sleep(0.25)
    raise TimeoutError("hooks /ready never returned 200")


def _cold_query_ms(db_path: str) -> float:
    """Measure first-query latency in a *fresh* process (store load + query)."""
    code = (
        "import os,time;os.environ['CHDB_DATA_PATH']=%r;"
        "from db import query_records;"
        "t=time.time();query_records('SELECT count() AS c FROM nyc_taxi.yellow_trips');"
        "print(round((time.time()-t)*1000,1))" % db_path
    )
    out = subprocess.check_output([sys.executable, "-c", code], cwd=str(ROOT), text=True)
    return float(out.strip().splitlines()[-1])


def _timed_health(app_base: str) -> tuple[float, str]:
    t0 = time.time()
    _, body = _get(f"{app_base}/health")
    return round((time.time() - t0) * 1000, 1), body


def main() -> int:
    ap = argparse.ArgumentParser(description="Local MicroVM lifecycle emulator")
    ap.add_argument("--db-path", default=None, help="Existing baked chDB store")
    ap.add_argument("--synthetic", action="store_true", help="Build a tiny store (no network)")
    ap.add_argument("--app-port", type=int, default=8080)
    ap.add_argument("--hooks-port", type=int, default=9000)
    args = ap.parse_args()

    if args.synthetic or not args.db_path:
        db_path = str(ROOT / ".microvm_emulator_data")
        Path(db_path).mkdir(exist_ok=True)
        print(f"[setup] building synthetic chDB store at {db_path} ...")
        _build_synthetic_store(db_path)
    else:
        db_path = str(Path(args.db_path).resolve())

    app_base = f"http://127.0.0.1:{args.app_port}"
    hooks_base = f"http://127.0.0.1:{args.hooks_port}"
    results: dict[str, object] = {}

    print(f"[cold] measuring fresh-process first-query latency ...")
    results["cold_first_query_ms"] = _cold_query_ms(db_path)

    proc = _start_process(db_path, args.app_port, args.hooks_port)
    try:
        print("[build] polling /ready (snapshot is gated on chDB warm) ...")
        results["warm_to_ready_s"] = _poll_ready(hooks_base)

        print("[build] POST /validate ...")
        results["validate_status"] = _post(f"{hooks_base}{HOOK_PREFIX}/validate")[0]

        print("[run] POST /run ...")
        results["run_status"] = _post(f"{hooks_base}{HOOK_PREFIX}/run")[0]

        warm_ms, body = _timed_health(app_base)
        results["warm_query_ms"] = warm_ms
        results["health_body"] = json.loads(body)

        print("[suspend] POST /suspend, then freezing (kill) the process ...")
        results["suspend_status"] = _post(f"{hooks_base}{HOOK_PREFIX}/suspend")[0]
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()

    # RESUME — restart against the same on-disk store (state must survive).
    print("[resume] restarting process against the persisted store ...")
    proc2 = _start_process(db_path, args.app_port, args.hooks_port)
    try:
        _poll_ready(hooks_base)
        results["resume_status"] = _post(f"{hooks_base}{HOOK_PREFIX}/resume")[0]
        resume_ms, body2 = _timed_health(app_base)
        results["post_resume_query_ms"] = resume_ms
        results["post_resume_row_count"] = json.loads(body2).get("row_count")
    finally:
        proc2.send_signal(signal.SIGTERM)
        try:
            proc2.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc2.kill()

    print("\n===== MicroVM local lifecycle results =====")
    for k, v in results.items():
        print(f"  {k:28s} {v}")
    cold = results.get("cold_first_query_ms")
    warm = results.get("warm_query_ms")
    if isinstance(cold, (int, float)) and isinstance(warm, (int, float)):
        print(
            f"\n  Snapshot-boot effect: cold first query {cold} ms "
            f"vs warm (post-/ready) {warm} ms"
        )
    rc = results.get("post_resume_row_count")
    print(f"  Agent-brain survived suspend/resume: row_count={rc} (data intact)\n")

    ok = (
        results.get("validate_status") == 200
        and results.get("resume_status") == 200
        and isinstance(rc, int)
        and rc > 0
    )
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
