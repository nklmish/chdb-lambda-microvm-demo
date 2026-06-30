#!/usr/bin/env python3
"""Turnkey HOST bake of a compact multi-year chDB store for the prebaked image.

This is the host-side counterpart to init_db.py (which bakes IN-CONTAINER for the
normal Dockerfile). Use this for large multi-year bakes (e.g. 2024+2025, ~90M rows)
that feed Dockerfile.prebaked. It encodes three hard-won lessons so a fresh deploy
does NOT need debugging:

  1. CloudFront RATE throttle: a long sequential pull with plain url() starts
     returning `Code: 86` after ~16 months. We send a browser User-Agent header
     AND space requests out, with retries + a final backfill pass.
  2. OPTIMIZE FINAL leaves the pre-merge source parts INACTIVE on disk
     (old_parts_lifetime), and chDB's embedded cleaner does NOT fire in-process —
     so the store stays ~2-3x bloated. We drop them by reopening the store in a
     FRESH process (this script re-execs itself with --gc), where startup GC with
     old_parts_lifetime=0 removes them (~6.8GB -> ~2.1GB for 2024+2025).
  3. data_profile.json must match the baked store (row_count, cutoff, delta_start).

Usage:
  CHDB_DATA_PATH="$PWD/full_chdb_data" BAKE_START_YEAR=2024 BAKE_END_YEAR=2025 \
    python3 scripts/bake_full.py
  # then (see deploy-agentcore Phase 4): comment out full_chdb_data/ in .dockerignore
  # and: finch build -f Dockerfile.prebaked -t nyc-taxi-agent:latest .

Env:
  CHDB_DATA_PATH   (required) output store dir
  BAKE_START_YEAR  default 2024
  BAKE_END_YEAR    default 2025   (cutoff = END-12-31, delta_start = END+1-01)
  MONTH_SPACING_S  default 15     (seconds between month pulls to dodge throttle)
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time

from chdb import session as chs

DB = os.environ["CHDB_DATA_PATH"]
CDN = "https://d37ci6vzurychx.cloudfront.net/trip-data"
# Browser UA — ClickHouse's default "ClickHouse/<ver>" UA is blocked by CloudFront
# WAF Bot Control on the TLC distribution.
UA = "Mozilla/5.0 (compatible; NYC-Taxi-Agent/1.0)"
SPACING_S = int(os.getenv("MONTH_SPACING_S", "15"))

_INSERT = """INSERT INTO nyc_taxi.yellow_trips
SELECT tpep_pickup_datetime, tpep_dropoff_datetime, passenger_count, trip_distance,
       PULocationID, DOLocationID, fare_amount, tip_amount, total_amount,
       payment_type, congestion_surcharge, Airport_fee
FROM url('{url}','Parquet','auto',headers('User-Agent'='{ua}'))
SETTINGS max_memory_usage=8000000000"""


def _q(sess, sql):
    return sess.query(sql)


def _qj(sess, sql):
    s = str(sess.query(sql, "JSON"))
    return json.loads(s)["data"] if s.strip() else []


def _load_month(sess, mth: str) -> bool:
    """Load one month with retries + backoff. Returns True on success."""
    url = f"{CDN}/yellow_tripdata_{mth}.parquet"
    for attempt in range(5):
        try:
            _q(sess, _INSERT.format(url=url, ua=UA))
            print(f"Loaded {mth}", flush=True)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"  {mth} attempt {attempt + 1} failed: {str(e)[:90]}", flush=True)
            time.sleep(20 * (attempt + 1))
    return False


def _create_schema(sess) -> None:
    _q(sess, "CREATE DATABASE IF NOT EXISTS nyc_taxi ENGINE = Atomic")
    _q(sess, "CREATE DATABASE IF NOT EXISTS agent_state ENGINE = Atomic")
    # old_parts_lifetime=0 so the --gc pass can drop OPTIMIZE's outdated parts at
    # the next fresh startup.
    _q(sess, """CREATE TABLE IF NOT EXISTS nyc_taxi.yellow_trips (
        pickup_datetime DateTime, dropoff_datetime DateTime, passenger_count UInt8,
        trip_distance Float64, pickup_location_id UInt16, dropoff_location_id UInt16,
        fare_amount Float64, tip_amount Float64, total_amount Float64, payment_type UInt8,
        congestion_surcharge Float64, airport_fee Float64
    ) ENGINE = MergeTree() ORDER BY (pickup_datetime, pickup_location_id)
      SETTINGS old_parts_lifetime = 0""")
    _q(sess, """CREATE TABLE IF NOT EXISTS agent_state.conversations (role String,
        content String, created_at DateTime DEFAULT now()) ENGINE = MergeTree()
        ORDER BY created_at""")
    _q(sess, """CREATE TABLE IF NOT EXISTS agent_state.analysis_log (description String,
        parameters String, result_summary String, execution_ms UInt32,
        created_at DateTime DEFAULT now()) ENGINE = MergeTree() ORDER BY created_at""")


def _generate_profile(sess, cutoff: str, delta_start: str) -> int:
    rc = int(_qj(sess, "SELECT count() c FROM nyc_taxi.yellow_trips")[0]["c"])
    dr = _qj(sess, "SELECT toString(min(pickup_datetime)) mn, toString(max(pickup_datetime)) mx "
                   "FROM nyc_taxi.yellow_trips")[0]
    fr = _qj(sess, "SELECT min(fare_amount) mn,max(fare_amount) mx,avg(fare_amount) mean,"
                   "median(fare_amount) med FROM nyc_taxi.yellow_trips")[0]
    pu = _qj(sess, "SELECT pickup_location_id zone_id,count() trips FROM nyc_taxi.yellow_trips "
                   "GROUP BY zone_id ORDER BY trips DESC LIMIT 5")
    do = _qj(sess, "SELECT dropoff_location_id zone_id,count() trips FROM nyc_taxi.yellow_trips "
                   "GROUP BY zone_id ORDER BY trips DESC LIMIT 5")
    pay = _qj(sess, "SELECT payment_type,count() cnt FROM nyc_taxi.yellow_trips GROUP BY payment_type")
    tot = sum(int(r["cnt"]) for r in pay) or 1
    pm = {int(r["payment_type"]): int(r["cnt"]) / tot for r in pay}
    profile = {
        "row_count": rc,
        "date_range": {"min": dr["mn"][:10], "max": dr["mx"][:10]},
        "fare_stats": {"min": fr["mn"], "max": fr["mx"], "mean": round(fr["mean"], 2),
                       "median": round(fr["med"], 2)},
        "top_pickup_zones": [{"zone_id": int(r["zone_id"]), "trips": int(r["trips"])} for r in pu],
        "top_dropoff_zones": [{"zone_id": int(r["zone_id"]), "trips": int(r["trips"])} for r in do],
        "payment_distribution": {"credit": round(pm.get(1, 0), 2), "cash": round(pm.get(2, 0), 2),
                                 "other": round(1 - pm.get(1, 0) - pm.get(2, 0), 2)},
        "baked_cutoff": cutoff,
        "delta_start": delta_start,
    }
    with open("data_profile.json", "w") as f:
        json.dump(profile, f, indent=2)
    return rc


def _gc() -> None:
    """Fresh-process pass: reopening the store triggers startup GC of the outdated
    parts that OPTIMIZE left behind (chDB's in-process cleaner does not run)."""
    sess = chs.Session(DB)
    try:
        print("GC: rows =", _qj(sess, "SELECT count() c FROM nyc_taxi.yellow_trips")[0]["c"], flush=True)
        time.sleep(45)  # let startup GC drop the 0-lifetime outdated parts
        parts = _qj(sess, "SELECT active, count() n, sum(bytes_on_disk) b FROM system.parts "
                          "WHERE database='nyc_taxi' AND table='yellow_trips' GROUP BY active ORDER BY active")
        for p in parts:
            print(f"GC parts active={p['active']} n={p['n']} {int(p['b'])/1048576:.0f}MB", flush=True)
    finally:
        sess.close()
    print("GC_DONE", flush=True)


def _bake() -> None:
    start = int(os.getenv("BAKE_START_YEAR", "2024"))
    end = int(os.getenv("BAKE_END_YEAR", "2025"))
    cutoff = f"{end}-12-31"
    delta_start = f"{end + 1}-01"
    # MONTHS env (comma-separated "YYYY-MM") overrides the computed range — handy for
    # targeted backfills or a fast smoke test (e.g. MONTHS=2026-01,2026-02).
    override = os.getenv("MONTHS", "").strip()
    if override:
        months = [m.strip() for m in override.split(",") if m.strip()]
    else:
        months = [f"{y}-{m:02d}" for y in range(start, end + 1) for m in range(1, 13)]

    sess = chs.Session(DB)
    try:
        _create_schema(sess)
        missing = []
        for mth in months:
            if not _load_month(sess, mth):
                missing.append(mth)
            time.sleep(SPACING_S)  # space requests to stay under the CDN rate limit
        # One backfill pass for months the first pass lost to throttling.
        if missing:
            print(f"Backfill pass for {missing}", flush=True)
            still = []
            for mth in missing:
                time.sleep(SPACING_S * 2)
                if not _load_month(sess, mth):
                    still.append(mth)
            if still:
                print(f"WARNING still-missing months: {still}", flush=True)
        print("OPTIMIZE FINAL ...", flush=True)
        _q(sess, "OPTIMIZE TABLE nyc_taxi.yellow_trips FINAL")
        rc = _generate_profile(sess, cutoff, delta_start)
        print(f"BAKE rows={rc} cutoff={cutoff} delta_start={delta_start}", flush=True)
    finally:
        sess.close()
    # Drop OPTIMIZE's outdated parts in a fresh process (see _gc docstring).
    subprocess.run([sys.executable, os.path.abspath(__file__), "--gc"], check=True)
    print("BAKE_DONE", flush=True)


if __name__ == "__main__":
    if "--gc" in sys.argv:
        _gc()
    else:
        _bake()
