#!/usr/bin/env python3
"""Bake a small local chDB sample so the agent has data to query.

Downloads one month of NYC Yellow Taxi trips from the public TLC CDN into an
embedded chDB store and writes ``data_profile.json`` (which the agent reads at
startup). This is the fast path for local development — a few seconds, ~3M rows.
The full multi-month bake used for container images lives in ``init_db.py``.

Usage:
  python scripts/bake_sample.py                 # 2024-01 into ./local_chdb_data
  python scripts/bake_sample.py --month 2024-03 --db-path ./local_chdb_data
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import chdb

CDN = "https://d37ci6vzurychx.cloudfront.net/trip-data"


def _q(db: str, sql: str) -> None:
    chdb.query(sql, path=db)


def _rows(db: str, sql: str) -> list[dict]:
    res = chdb.query(sql, "JSON", path=db)
    raw = res.bytes() if res else b""
    return json.loads(raw)["data"] if raw else []


def bake(db: str, month: str) -> int:
    _q(db, "CREATE DATABASE IF NOT EXISTS nyc_taxi ENGINE = Atomic")
    _q(db, "CREATE DATABASE IF NOT EXISTS agent_state ENGINE = Atomic")
    _q(db, """CREATE TABLE IF NOT EXISTS nyc_taxi.yellow_trips (
        pickup_datetime DateTime, dropoff_datetime DateTime, passenger_count UInt8,
        trip_distance Float64, pickup_location_id UInt16, dropoff_location_id UInt16,
        fare_amount Float64, tip_amount Float64, total_amount Float64, payment_type UInt8,
        congestion_surcharge Float64, airport_fee Float64
    ) ENGINE = MergeTree() ORDER BY (pickup_datetime, pickup_location_id)""")
    _q(db, """CREATE TABLE IF NOT EXISTS agent_state.conversations (
        role String, content String, created_at DateTime DEFAULT now()
    ) ENGINE = MergeTree() ORDER BY created_at""")
    _q(db, """CREATE TABLE IF NOT EXISTS agent_state.analysis_log (
        description String, parameters String, result_summary String,
        execution_ms UInt32, created_at DateTime DEFAULT now()
    ) ENGINE = MergeTree() ORDER BY created_at""")
    url = f"{CDN}/yellow_tripdata_{month}.parquet"
    _q(db, f"""INSERT INTO nyc_taxi.yellow_trips
        SELECT tpep_pickup_datetime, tpep_dropoff_datetime, passenger_count, trip_distance,
               PULocationID, DOLocationID, fare_amount, tip_amount, total_amount,
               payment_type, congestion_surcharge, Airport_fee
        FROM url('{url}', 'Parquet')""")
    rows = _rows(db, "SELECT count() AS c FROM nyc_taxi.yellow_trips")
    return int(rows[0]["c"]) if rows else 0


def write_profile(db: str, cutoff_year: int) -> None:
    dr = _rows(db, "SELECT toString(min(pickup_datetime)) mn, toString(max(pickup_datetime)) mx "
                   "FROM nyc_taxi.yellow_trips")[0]
    fr = _rows(db, "SELECT min(fare_amount) mn, max(fare_amount) mx, avg(fare_amount) mean, "
                   "median(fare_amount) med FROM nyc_taxi.yellow_trips")[0]
    pu = _rows(db, "SELECT pickup_location_id zone_id, count() trips FROM nyc_taxi.yellow_trips "
                   "GROUP BY zone_id ORDER BY trips DESC LIMIT 5")
    do = _rows(db, "SELECT dropoff_location_id zone_id, count() trips FROM nyc_taxi.yellow_trips "
                   "GROUP BY zone_id ORDER BY trips DESC LIMIT 5")
    pay = _rows(db, "SELECT payment_type, count() cnt FROM nyc_taxi.yellow_trips GROUP BY payment_type")
    rc = int(_rows(db, "SELECT count() c FROM nyc_taxi.yellow_trips")[0]["c"])
    total = sum(int(r["cnt"]) for r in pay) or 1
    pm = {int(r["payment_type"]): int(r["cnt"]) / total for r in pay}
    profile = {
        "row_count": rc,
        "date_range": {"min": dr["mn"][:10], "max": dr["mx"][:10]},
        "fare_stats": {"min": fr["mn"], "max": fr["mx"],
                       "mean": round(fr["mean"], 2), "median": round(fr["med"], 2)},
        "top_pickup_zones": [{"zone_id": int(r["zone_id"]), "trips": int(r["trips"])} for r in pu],
        "top_dropoff_zones": [{"zone_id": int(r["zone_id"]), "trips": int(r["trips"])} for r in do],
        "payment_distribution": {"credit": round(pm.get(1, 0), 2), "cash": round(pm.get(2, 0), 2),
                                 "other": round(1 - pm.get(1, 0) - pm.get(2, 0), 2)},
        "baked_cutoff": f"{cutoff_year}-12-31",
        "delta_start": f"{cutoff_year + 1}-01",
    }
    Path("data_profile.json").write_text(json.dumps(profile, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description="Bake a one-month local chDB sample")
    ap.add_argument("--month", default="2024-01", help="YYYY-MM (default 2024-01)")
    ap.add_argument("--db-path", default=os.getenv("CHDB_DATA_PATH", "./local_chdb_data"))
    args = ap.parse_args()
    db = str(Path(args.db_path).resolve())
    print(f"baking {args.month} into {db} ...")
    n = bake(db, args.month)
    write_profile(db, int(args.month.split("-")[0]))
    print(f"done: {n:,} rows. Wrote data_profile.json. Start the app with:")
    print(f"  CHDB_DATA_PATH={db} uvicorn main:app --host 127.0.0.1 --port 8080")
    return 0 if n > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
