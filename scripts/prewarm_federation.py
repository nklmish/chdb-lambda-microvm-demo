#!/usr/bin/env python3
"""prewarm_federation.py — smoke-check every federation leg and print the numbers.

Runs analyze_fleet_across_clouds once and prints the assembled per-cloud
timeline, so you can confirm each plane (GCS / Azure / ClickHouse Cloud / local /
AWS S3) is reachable and the creds resolve before a demo. The first cross-cloud
reach is network-cold (~35s) while every remote file is fetched for the first
time; a warm reach is ~2s.

Note: the federation tool caches in the *agent's own process* (Scenario B), so
this standalone run warms only its own cache — it cannot pre-warm a separately
running server. In a live demo the first federation question is the "reach"
(a few seconds) and the immediate re-ask is the "instant local" — which is the
materialization story on screen. Use this script to verify legs + capture
numbers, not to warm a server.

Usage:
    # creds + data path come from the environment / .env.local
    python3 scripts/prewarm_federation.py
    python3 scripts/prewarm_federation.py --sources gcs,azure,chc,local,s3

Exit codes:
    0 — all requested legs returned rows
    1 — federation returned no rows (all legs skipped)
"""
from __future__ import annotations

import argparse
import json
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="Warm the federation cache.")
    parser.add_argument(
        "--sources",
        default="",
        help="Comma-separated source keys (default: all). e.g. gcs,azure,chc,local,s3",
    )
    args = parser.parse_args()

    # Imported here so --help works without the chDB/strands import cost.
    from federation_tools import analyze_fleet_across_clouds

    t0 = time.time()
    result = json.loads(
        analyze_fleet_across_clouds(sources=args.sources, refresh=True)
    )
    wall = time.time() - t0

    rows = result.get("data", [])
    print(
        f"federation: mode={result.get('mode')} "
        f"sources={result.get('sources_used')} "
        f"rows={result.get('row_count')} wall={wall:.1f}s"
    )
    for r in rows:
        print(
            f"  {r['era']}  {r['cloud']:<26} "
            f"tip%={r['avg_tip_pct']:<6} fare={r['avg_fare']:<6} trips={r['trips']}"
        )
    for note in result.get("notes", []):
        print(f"  note: {note}")

    if not rows:
        print("ERROR: federation returned no rows (all legs skipped).", file=sys.stderr)
        return 1
    print(f"all {len(rows)} legs reachable — federation is healthy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
