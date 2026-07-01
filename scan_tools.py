"""scan_tools — raw chDB analytical scan over a sharded S3 dataset (no LLM).

Powers the /scan worker. Allow-listed datasets:

  - taxi      : the PRIVATE NYC-taxi lake in our S3 (read with the MicroVM's own
                least-privilege role; creds via IMDS, never in the SQL).
  - buildings : Overture Maps buildings — ~2.5 billion rows, PUBLIC (anonymous
                NOSIGN), read straight from s3://overturemaps-us-west-2 in-region.
  - segments  : Overture transportation/segment — the global road network.

Each MicroVM runs ONE chDB s3() aggregate over its shard and returns a mergeable
per-group partial, so the coordinator can gather partials across the fleet and
merge them. Security: the coordinator sends only a vetted dataset key + release +
parquet *basenames*; this module builds every URL from the baked registry, so no
arbitrary URL/SQL (or bucket) ever crosses the wire.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

import boto3
import chdb
from botocore import UNSIGNED
from botocore.config import Config

from db import DB_PATH

LAKE_BUCKET = os.getenv("LAKE_BUCKET", "")
LAKE_PREFIX = os.getenv("LAKE_PREFIX", "lake/yellow")
OVERTURE_BUCKET = "overturemaps-us-west-2"

MAX_FILES = 600
_RELEASE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
_FILE_RE = re.compile(r"^[A-Za-z0-9._=-]+\.parquet$")  # basenames only — no slashes


@dataclass(frozen=True)
class Dataset:
    key: str
    label: str          # human label (fleet size line)
    answer_label: str   # what the merged chart shows
    bucket: str         # "" → resolve LAKE_BUCKET at runtime (private)
    prefix: str         # may contain "{release}"
    auth: str           # "role" (signed) | "nosign" (public/anonymous)
    group_sql: str      # GROUP BY key expression
    metric_sql: str     # extra aggregate columns, comma-prefixed (beyond rows_read)
    needs_release: bool


DATASETS: dict[str, Dataset] = {
    "taxi": Dataset(
        "taxi", "NYC yellow-taxi lake (~0.8B rows)", "tip rate by year",
        "", LAKE_PREFIX, "role",
        "toYear(tpep_pickup_datetime)",
        (", toInt64(countIf(fare_amount >= 2.5)) AS cnt"
         ", sumIf(tip_amount, fare_amount >= 2.5) AS tip_sum"
         ", sumIf(fare_amount, fare_amount >= 2.5) AS fare_sum"),
        False,
    ),
    "buildings": Dataset(
        "buildings", "Overture buildings (~2.5B rows)", "buildings by class",
        OVERTURE_BUCKET, "release/{release}/theme=buildings/type=building", "nosign",
        "coalesce(nullIf(class, ''), '(unclassified)')",
        ", toInt64(count()) AS cnt",
        True,
    ),
    "segments": Dataset(
        "segments", "Overture road network (~0.3B rows)", "road segments by class",
        OVERTURE_BUCKET, "release/{release}/theme=transportation/type=segment", "nosign",
        "coalesce(nullIf(class, ''), '(unclassified)')",
        ", toInt64(count()) AS cnt",
        True,
    ),
}


def _dataset(key: str) -> Dataset:
    ds = DATASETS.get(key)
    if ds is None:
        raise ValueError(f"unknown dataset: {key!r} (allowed: {', '.join(DATASETS)})")
    return ds


def _bucket(ds: Dataset) -> str:
    if ds.bucket:
        return ds.bucket
    if not LAKE_BUCKET:
        raise RuntimeError("LAKE_BUCKET not configured")
    return LAKE_BUCKET


def _validate(ds: Dataset, release: str | None, files: list[str]) -> tuple[str, list[str]]:
    if not files:
        raise ValueError("no files supplied")
    if len(files) > MAX_FILES:
        raise ValueError(f"too many files ({len(files)} > {MAX_FILES})")
    for f in files:
        if not _FILE_RE.match(f):
            raise ValueError(f"invalid file name: {f!r}")
    prefix = ds.prefix
    if ds.needs_release:
        if not release or not _RELEASE_RE.match(release):
            raise ValueError(f"invalid release: {release!r}")
        prefix = ds.prefix.format(release=release)
    return prefix, files


def _s3_client(ds: Dataset):
    if ds.auth == "nosign":
        return boto3.client("s3", config=Config(signature_version=UNSIGNED))
    return boto3.client("s3")


def _frozen_creds() -> tuple[str, str, str | None]:
    c = boto3.Session().get_credentials().get_frozen_credentials()
    return c.access_key, c.secret_key, c.token


def _source(ds: Dataset, bucket: str, prefix: str, files: list[str]) -> tuple[str, str]:
    """(live s3() source with auth, redacted source)."""
    if len(files) > 1:
        url = f"s3://{bucket}/{prefix}/{{{','.join(files)}}}"
    else:
        url = f"s3://{bucket}/{prefix}/{files[0]}"
    if ds.auth == "nosign":
        live = f"s3('{url}', NOSIGN, 'Parquet')"
        return live, live  # nothing secret to redact
    ak, sk, tok = _frozen_creds()
    if tok:
        live = f"s3('{url}', '{ak}', '{sk}', '{tok}', 'Parquet')"
    else:
        live = f"s3('{url}', '{ak}', '{sk}', 'Parquet')"
    return live, f"s3('{url}', '***', 'Parquet')"


def _shard_bytes(ds: Dataset, bucket: str, prefix: str, files: list[str]) -> int:
    s3 = _s3_client(ds)
    total = 0
    for f in files:
        try:
            total += s3.head_object(Bucket=bucket, Key=f"{prefix}/{f}")["ContentLength"]
        except Exception:  # noqa: BLE001
            pass
    return total


def run_scan(dataset: str = "taxi", release: str | None = None,
             files: list[str] | None = None) -> dict:
    """Scan one shard of a dataset: mergeable per-group partial + scan volume."""
    ds = _dataset(dataset)
    prefix, files = _validate(ds, release, files or [])
    bucket = _bucket(ds)
    source, redacted = _source(ds, bucket, prefix, files)

    sql = (
        f"SELECT {ds.group_sql} AS grp, toInt64(count()) AS rows_read{ds.metric_sql} "
        f"FROM {source} GROUP BY grp"
    )
    # Same embedded-server path the app is pinned to (chDB's server is process-global).
    t0 = time.time()
    raw = json.loads(chdb.query(sql, "JSON", path=DB_PATH).bytes())
    elapsed_ms = round((time.time() - t0) * 1000)

    data = raw.get("data", [])
    rows_scanned = sum(int(r.get("rows_read", 0) or 0) for r in data)
    if dataset == "taxi":  # drop dirty-timestamp years from the answer
        data = [r for r in data if str(r.get("grp", "")).isdigit() and 2009 <= int(r["grp"]) <= 2026]
    return {
        "dataset": dataset,
        "partial": data,
        "rows_scanned": rows_scanned,
        "bytes_read": _shard_bytes(ds, bucket, prefix, files),
        "file_count": len(files),
        "elapsed_ms": elapsed_ms,
        "sql": redacted,
    }
