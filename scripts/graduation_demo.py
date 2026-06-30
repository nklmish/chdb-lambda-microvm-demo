#!/usr/bin/env python3
"""Zero-refactor graduation: the SAME chDB SQL, local then ClickHouse Cloud.

The blog's closing flourish — *"When the working set outgrows local capacity,
point the same SQL at the warehouse. Same SQL, same types, same engine. No other
embedded engine can offer this, because no other shares its query plane with a
managed cloud warehouse."*

This proves it literally: we define a view ``trips`` over a **local chDB** table,
run an analytical query, then redefine ``trips`` over **ClickHouse Cloud** via
``remoteSecure()`` and run the *byte-identical* query string. Only the data source
behind the view changes; the analytics don't move.

Credentials resolve from the environment (CLICKHOUSE_URL/USER/PASSWORD) or, as a
fallback, from SSM (/clickhouse/*). They never appear in output.

Usage:
  python scripts/graduation_demo.py --db-path ./local_chdb_data
  CLICKHOUSE_URL=... CLICKHOUSE_USER=... CLICKHOUSE_PASSWORD=... python scripts/graduation_demo.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time

from chdb import session as chs

CHC_TABLE = "workshop.nyc_taxi_trips"
CHC_NATIVE_PORT = "9440"
LOCAL_TABLE = "nyc_taxi.yellow_trips"

# The analytical query is IDENTICAL for both backends — only the `trips` view's
# source changes. This string is what "zero refactor" means.
ANALYTICAL_SQL = (
    "SELECT toHour(pickup_datetime) AS hour, count() AS trips, "
    "round(avg(fare_amount), 2) AS avg_fare, "
    "round(avg(tip_amount / nullIf(fare_amount, 0)) * 100, 2) AS tip_pct "
    "FROM trips GROUP BY hour ORDER BY trips DESC LIMIT 3 "
    # Connection timeouts so an idle ClickHouse Cloud service has time to wake;
    # harmless/ignored for the local run, so the query text stays identical.
    "SETTINGS connect_timeout_with_failover_ms = 30000, receive_timeout = 60000"
)


def _chc_creds(ssm_region: str) -> dict:
    url = os.getenv("CLICKHOUSE_URL", "").strip()
    user = os.getenv("CLICKHOUSE_USER", "default").strip()
    pwd = os.getenv("CLICKHOUSE_PASSWORD", "").strip()
    if not (url and pwd):  # fall back to SSM /clickhouse/*
        def ssm(name, decrypt=False):
            cmd = ["aws", "ssm", "get-parameter", "--name", name,
                   "--region", ssm_region, "--query", "Parameter.Value", "--output", "text"]
            if decrypt:
                cmd.append("--with-decryption")
            r = subprocess.run(cmd, capture_output=True, text=True)
            return r.stdout.strip() if r.returncode == 0 else ""
        url = url or ssm("/clickhouse/CLICKHOUSE_URL")
        user = ssm("/clickhouse/CLICKHOUSE_USER") or user
        pwd = pwd or ssm("/clickhouse/CLICKHOUSE_PASSWORD", decrypt=True)
    if not (url and pwd):
        raise SystemExit(
            "No ClickHouse Cloud credentials. Set CLICKHOUSE_URL/USER/PASSWORD "
            "or store them in SSM /clickhouse/* (see README)."
        )
    host = re.sub(r"^https?://", "", url).rstrip("/")
    return {"host": host, "user": user, "password": pwd}


def _rows(sess, sql: str) -> list[dict]:
    res = sess.query(sql, "JSON")
    raw = res.bytes() if (res is not None and hasattr(res, "bytes")) else b""
    return json.loads(raw).get("data", []) if raw else []


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("    (no rows)")
        return
    print(f"    {'hour':<6}{'trips':<14}{'avg_fare':<10}tip_pct")
    for r in rows:
        print(f"    {r['hour']:<6}{int(r['trips']):<14,}{r['avg_fare']:<10}{r['tip_pct']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Zero-refactor graduation demo")
    ap.add_argument("--db-path", default=os.getenv("CHDB_DATA_PATH", "./local_chdb_data"))
    ap.add_argument("--ssm-region", default="us-east-1")
    args = ap.parse_args()

    sess = chs.Session(args.db_path)

    # Guard: the local table must exist (bake it first — see the run-local skill).
    cnt = _rows(sess, f"SELECT count() AS c FROM {LOCAL_TABLE}")
    local_rows = int(cnt[0]["c"]) if cnt else 0
    if local_rows == 0:
        raise SystemExit(
            f"local table {LOCAL_TABLE} is empty at {args.db_path}. Bake a sample "
            "first (see .claude/skills/run-local/SKILL.md)."
        )

    print("The analytical query (identical for both backends):\n")
    print("  " + ANALYTICAL_SQL.replace(" SETTINGS", "\n  SETTINGS"))
    print()

    # --- Day 1: hot data is LOCAL -------------------------------------------
    print(f"[local chDB]  {LOCAL_TABLE}  ({local_rows:,} rows)")
    sess.query(
        f"CREATE OR REPLACE VIEW trips AS "
        f"SELECT pickup_datetime, fare_amount, tip_amount FROM {LOCAL_TABLE}"
    )
    t0 = time.time()
    local = _rows(sess, ANALYTICAL_SQL)
    print(f"    served in {round((time.time() - t0) * 1000)} ms")
    _print_table(local)

    # --- Day 100: the dataset grew -> graduate to ClickHouse Cloud ----------
    creds = _chc_creds(args.ssm_region)
    hp = f"{creds['host']}:{CHC_NATIVE_PORT}"
    print(f"\n[ClickHouse Cloud]  {CHC_TABLE}  via remoteSecure({creds['host']})")
    # Only the view's SOURCE changes; the analytical query below is unchanged.
    sess.query(
        "CREATE OR REPLACE VIEW trips AS SELECT "
        "tpep_pickup_datetime AS pickup_datetime, fare_amount, tip_amount "
        f"FROM remoteSecure('{hp}', '{CHC_TABLE}', "
        f"'{creds['user']}', '{creds['password']}')"
    )
    t0 = time.time()
    cloud = _rows(sess, ANALYTICAL_SQL)
    print(f"    served in {round((time.time() - t0) * 1000)} ms")
    _print_table(cloud)

    sess.close()
    print(
        "\nSame SQL, same engine, same types — only the view's source moved from a "
        "local chDB table to a ClickHouse Cloud table. Zero query refactoring."
    )
    return 0 if (local and cloud) else 1


if __name__ == "__main__":
    raise SystemExit(main())
