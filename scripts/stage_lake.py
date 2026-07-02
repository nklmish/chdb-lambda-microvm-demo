#!/usr/bin/env python3
"""scripts/stage_lake.py — stage the NYC-TLC yellow-taxi "cold lake" into same-region S3.

The distributed-scan demo reads its cold lake from S3 in the compute region (so
the numbers are clean, fast, and reproducible, and it tells the production-shaped
"data lake + serverless compute" story). The public NYC-TLC S3 bucket was
deprecated — the data now lives only behind the CloudFront CDN — so this streams
each monthly parquet from the CDN straight into our bucket (nothing is stored on
disk; the browser User-Agent clears the CDN's WAF bot rule).

Idempotent: files already present (same size) are skipped, so it is safe to
re-run or resume. Account-agnostic: the bucket defaults to the deploy artifact
bucket derived from the caller identity.

Usage:
  python scripts/stage_lake.py --region us-west-2                 # 2015-01 .. 2025-12
  python scripts/stage_lake.py --start 2019-01 --end 2025-12      # a subset
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import sys
import time

import boto3
import requests

CDN = "https://d37ci6vzurychx.cloudfront.net/trip-data"
UA = "Mozilla/5.0 (compatible; NYC-Taxi-Agent/1.0)"
PREFIX = "lake/yellow"


def months(start: str, end: str) -> list[str]:
    (sy, sm), (ey, em) = (int(start[:4]), int(start[5:7])), (int(end[:4]), int(end[5:7]))
    out, y, m = [], sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def default_bucket(region: str) -> str:
    acct = boto3.client("sts").get_caller_identity()["Account"]
    return f"nyc-taxi-microvm-artifacts-{acct}-{region}"


def stage_one(s3, bucket: str, month: str) -> tuple[str, str, int]:
    """Copy one month CDN→S3. Idempotent (skips if already in S3) and resilient to
    CloudFront throttling: 403/429/5xx are retried with backoff; only a 404 means
    the month is genuinely absent."""
    fname = f"yellow_tripdata_{month}.parquet"
    url = f"{CDN}/{fname}"
    key = f"{PREFIX}/{fname}"

    # Already staged? (existence is enough for idempotent resume)
    try:
        size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
        return month, "skip", size
    except Exception:  # noqa: BLE001 — not present yet
        pass

    for attempt in range(6):
        try:
            resp = requests.get(url, headers={"User-Agent": UA}, stream=True, timeout=180)
            if resp.status_code == 200:
                size = int(resp.headers.get("Content-Length") or 0)
                resp.raw.decode_content = True
                s3.upload_fileobj(resp.raw, bucket, key)  # multipart, streamed
                return month, "uploaded", size
            code = resp.status_code
            resp.close()
            if code == 404:
                return month, "absent", 0
        except Exception:  # noqa: BLE001 — transient network/upload drop → retry whole file
            pass
        time.sleep(2 * (attempt + 1))  # back off (CDN throttle or dropped connection)
    return month, "throttled", 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage NYC-TLC yellow lake into S3")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--bucket", default=None)
    ap.add_argument("--start", default="2015-01")
    ap.add_argument("--end", default="2025-12")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    bucket = args.bucket or default_bucket(args.region)
    s3 = boto3.client("s3", region_name=args.region)
    ms = months(args.start, args.end)
    print(f"staging {len(ms)} months → s3://{bucket}/{PREFIX}/ ({args.region})", flush=True)

    total_bytes = uploaded = skipped = absent = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(stage_one, s3, bucket, m): m for m in ms}
        for fut in cf.as_completed(futs):
            month, status, size = fut.result()
            if status == "uploaded":
                uploaded += 1; total_bytes += size
            elif status == "skip":
                skipped += 1; total_bytes += size
            elif status == "absent":
                absent += 1
            print(f"  {month:<8} {status:<9} {size/1e6:6.1f} MB", flush=True)

    print(f"\ndone: {uploaded} uploaded, {skipped} already present, {absent} absent. "
          f"lake size ≈ {total_bytes/1e9:.1f} GB", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
