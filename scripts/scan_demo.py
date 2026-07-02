#!/usr/bin/env python3
"""scripts/scan_demo.py — the serverless distributed-scan reference workload.

    "ClickHouse Cloud is where your data lives, chDB is what your agent thinks
     with, and Lambda MicroVMs is where it gets to think, in private."

Launches N Lambda MicroVMs (each a private, Firecracker-isolated chDB engine),
suspends the fleet to $0, resumes it snapshot-hot, then fans one file-shard of a
public/private S3 dataset at each VM's /scan. Each VM computes a mergeable partial
in-process; the coordinator gathers and merges them — scatter/gather where the
nodes are ephemeral and billed per second. Prints one illustrative run (not a
benchmark). Datasets:

  buildings : Overture Maps buildings — ~2.5 billion rows, PUBLIC (no staging).
  segments  : Overture road network — the global roads taxis drive on.
  taxi      : our private NYC-taxi lake (needs scripts/stage_lake.py first).

Usage:
  python scripts/scan_demo.py --dataset buildings --count 50 --region us-west-2
  python scripts/scan_demo.py --dataset segments  --count 30
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet_core as fc  # noqa: E402


def _bar(pct: float, width: int = 24) -> str:
    fill = max(0, min(int(round(pct / 100 * width)), width))
    return "█" * fill + "·" * (width - fill)


def main() -> int:
    ap = argparse.ArgumentParser(description="Serverless distributed-scan reference workload")
    ap.add_argument("--dataset", default=fc.DEFAULT_SCAN_DATASET, choices=list(fc.SCAN_DATASETS))
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--region", default=fc.DEFAULT_REGION)
    ap.add_argument("--name", default=fc.DEFAULT_NAME)
    ap.add_argument("--max-files", type=int, default=None, help="cap files scanned (default: all)")
    ap.add_argument("--no-burst", action="store_true")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    acct = fc.account(args.region)
    bucket = f"nyc-taxi-microvm-artifacts-{acct}-{args.region}"

    def on_event(ev: dict) -> None:
        t = ev.get("type")
        if t == "preflight":
            rel = f" · release {ev['release']}" if ev.get("release") else ""
            print(f"dataset: {ev['label']} ({ev['rows_hint']} rows){rel} · {ev['files']} parquet files")
            print(f"fleet:   {ev['n']} MicroVMs · image v{ev['version']} · {ev['region']}\n")
            print(f"[1/5] launching {ev['n']} private Firecracker chDB engines ...")
        elif t == "launch":
            print(f"       {len(ev['vms'])} launched")
            print("[2/5] waiting for snapshot-hot readiness ...")
        elif t == "suspend":
            print(f"[3/5] {ev['note']}")
            print("[4/5] resuming fleet snapshot-hot (timed) ...")
        elif t == "scatter":
            print(f"[5/5] scatter: {len(ev['shards'])} shards — firing /scan at every engine CONCURRENTLY ...")
        elif t == "scanned":
            print(f"       #{ev['idx']:<2} scanned {ev['rows']:>12,} rows  in {ev['scan_ms']:>6} ms")
        elif t == "error":
            print(f"\nERROR: {ev['message']}", file=sys.stderr)

    s = fc.run_scan_fleet_blocking(
        args.count, args.region, args.name, dataset=args.dataset, lake_bucket=bucket,
        max_files=args.max_files, on_event=on_event, keep=args.keep, burst=not args.no_burst)

    if s.get("type") == "error":
        return 1

    c = s["cost"]
    print("\n" + "═" * 68)
    print("  SERVERLESS DISTRIBUTED SCAN — one illustrative run")
    print("  (single-shot, no warm-up/repeats — order-of-magnitude, not a benchmark)")
    print("═" * 68)
    print(f"  fleet ............ {s['n']} Firecracker MicroVMs, each a private chDB engine")
    if s.get("resume_ms") is not None:
        print(f"  snapshot-hot ..... resumed from $0 in ~{s['resume_ms']/1000:.0f}s (rate-limited fleet resume)")
    print(f"  scanned .......... {s['total_rows']:,} rows in {s['wall_ms']/1000:.1f} s wall-clock")
    print(f"  throughput ....... {s['rows_per_s']:,} rows/s (aggregate)")
    print(f"  cost/run ......... ${c['burst_usd']} (compute ${c['compute_usd']} + snapshot ${c['snapshot_usd']})")
    print(f"  at rest .......... $0 compute · ~${c['at_rest_usd_per_hour']}/hr snapshot storage")
    print(f"  vm shape ......... {c['mem_gb']:.0f} GB (image) · ~{c['vcpu']:.0f} vCPU (assumed; not exposed by API)")
    print("  " + "-" * 64)
    print(f"  {s['answer_label']} (merged from all shards):")
    ans = s["answer"]
    unit = ans[0]["unit"] if ans else ""
    scale = max((r["value"] for r in ans), default=1) or 1
    for r in ans[:12]:
        val = f"{r['value']:.2f}%" if unit == "%" else f"{r['count']:,}"
        print(f"    {str(r['label'])[:22]:<22} {_bar(r['value'] / scale * 100)}  {val:>14}")
    print("═" * 68)
    print('  "ClickHouse Cloud is where your data lives, chDB is what your agent')
    print('   thinks with, and Lambda MicroVMs is where it gets to think, in private."')
    print("  → private per-request engines · $0 compute at rest · pay per second.")
    return 0 if s.get("ok") == s["n"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
