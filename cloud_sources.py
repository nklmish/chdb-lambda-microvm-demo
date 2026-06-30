"""cloud_sources.py — vetted federation source registry.

Encodes the allow-list of named data planes the federation tool may reach. The
LLM never supplies URLs, credentials, or SQL — it selects from these named
sources by key, and this module turns each into a *normalized* SELECT fragment
that yields exactly the columns:

    (cloud String, era String, trips Int64, avg_tip_pct Float64, avg_fare Float64)

so `federation_tools.analyze_fleet_across_clouds` can `UNION ALL` them into one
cross-cloud statement.

Design notes
------------
- One row per plane. Each `Source` owns the SQL needed to read *its* native
  schema and project it onto the common shape — schema heterogeneity is hidden
  here, not in the tool.
- No raw input from the model. URLs are constants; ClickHouse Cloud credentials
  are resolved from the environment / SSM (never hardcoded, never from the LLM).
- Verified anonymous / secure reachability (2026-06, chDB 4.1.x):
    GCS   clickhouse-public-datasets/nyc-taxi  TSVWithNames (~2015)  url()  anon
    Azure azureopendatastorage/nyctlc          Parquet      (2018)   url()  anon
    S3    NYC TLC CloudFront delta              Parquet      (2025)   url()  + UA
    CHC   workshop.nyc_taxi_trips (26.7M)       native       (2023)   remoteSecure()
    local baked nyc_taxi.yellow_trips           in-process   (2024)
"""
from __future__ import annotations

import os
import re
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable

# -- Constants ----------------------------------------------------------------

# Local baked taxi table (matches db.py / weather_tools.py convention).
_LOCAL_TABLE = "nyc_taxi.yellow_trips"

# Public NYC TLC CloudFront CDN (shared with sql_tools.py). The default
# ClickHouse User-Agent is blocked by the distribution's WAF Bot Control, so the
# S3/CDN leg passes a browser-shaped UA via headers() — same fix as
# sql_tools.DELTA_FETCH_USER_AGENT.
_CDN_BASE = "https://d37ci6vzurychx.cloudfront.net/trip-data"
_CDN_USER_AGENT = "Mozilla/5.0 (compatible; NYC-Taxi-Agent/1.0)"

# ClickHouse's own public GCS bucket (the classic tutorial trips files —
# TSV-with-header, gzip). trips_0.gz is Q3-2015, ~1.0M rows.
_GCS_HISTORICAL = (
    "https://storage.googleapis.com/clickhouse-public-datasets/nyc-taxi/trips_0.gz"
)

# Azure Open Datasets NYC TLC (Microsoft-hosted, anonymous Parquet).
_AZURE_ENDPOINT = "https://azureopendatastorage.blob.core.windows.net"
_AZURE_CONTAINER = "nyctlc"

# ClickHouse Cloud "Historical Lake" — native-secure port for remoteSecure().
# 8443 is the HTTPS interface; chDB's remoteSecure() speaks the native protocol
# on 9440. Credentials come from the environment (CLICKHOUSE_*), never the LLM.
_CHC_NATIVE_PORT = "9440"
_CHC_TABLE = "workshop.nyc_taxi_trips"


# -- Azure blob-name resolution (anonymous REST list, cached) -----------------

_azure_cache: dict[tuple[int, int], str] = {}
_AZURE_LIST_TTL = 3600.0
_azure_cache_expires = 0.0


def _azure_first_part_url(year: int, month: int) -> str:
    """Resolve the first `part-*.parquet` blob URL for a (year, month).

    The Azure Open Datasets container allows anonymous container *listing* via
    the Blob REST API, which is how we discover the opaque part-file GUIDs that
    `url()` then reads directly. Cached for an hour at module scope (mirrors the
    sql_tools delta cache).
    """
    global _azure_cache_expires
    now = time.time()
    if now >= _azure_cache_expires:
        _azure_cache.clear()
        _azure_cache_expires = now + _AZURE_LIST_TTL

    key = (year, month)
    if key in _azure_cache:
        return _azure_cache[key]

    prefix = f"yellow/puYear={year}/puMonth={month}/part"
    api = (
        f"{_AZURE_ENDPOINT}/{_AZURE_CONTAINER}"
        f"?restype=container&comp=list&prefix={prefix}&maxresults=1"
    )
    with urllib.request.urlopen(api, timeout=15) as resp:
        xml = resp.read().decode()
    m = re.search(r"<Name>(.*?\.parquet)</Name>", xml)
    if not m:
        raise RuntimeError(
            f"Azure: no parquet blob found for puYear={year}/puMonth={month}"
        )
    url = f"{_AZURE_ENDPOINT}/{_AZURE_CONTAINER}/{m.group(1)}"
    _azure_cache[key] = url
    return url


# -- ClickHouse Cloud credential resolution -----------------------------------

def chc_credentials() -> dict | None:
    """Resolve ClickHouse Cloud connection details from the environment.

    Returns None when no password is configured, so the federation tool can
    silently omit the warehouse leg (graceful absence — same posture as the
    optional AGENTCORE_MEMORY_ID / LANGFUSE_* wiring). Host is derived from
    CLICKHOUSE_URL with the scheme stripped; remoteSecure() uses the native
    secure port (9440), not the HTTPS interface port.
    """
    raw_host = os.getenv("CLICKHOUSE_URL", "").strip()
    user = os.getenv("CLICKHOUSE_USER", "default").strip()
    pwd = os.getenv("CLICKHOUSE_PASSWORD", "").strip()
    if not raw_host or not pwd:
        return None
    host = re.sub(r"^https?://", "", raw_host).rstrip("/")
    return {"host": host, "port": _CHC_NATIVE_PORT, "user": user, "password": pwd}


# -- PostgreSQL (third-party RDBMS leg — taxi-zone lookup) ---------------------

# The zone-enrichment leg (paper's Fig 3 + hero-SQL `postgresql()`). Defaults
# match compose.yml's postgres service; override via POSTGRES_* for other hosts.
_PG_TABLE = "taxi_zones"


def pg_config() -> dict:
    """Resolve PostgreSQL connection details (defaults match compose.yml).

    Always returns a config — the leg degrades gracefully at query time if the
    server is unreachable (the federation tool catches and annotates), so no
    presence check is needed here.
    """
    return {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": os.getenv("POSTGRES_PORT", "5432"),
        "db": os.getenv("POSTGRES_DB", "nyctaxi"),
        "user": os.getenv("POSTGRES_USER", "taxi"),
        "password": os.getenv("POSTGRES_PASSWORD", "taxi"),
        "table": _PG_TABLE,
    }


# -- Source registry ----------------------------------------------------------

@dataclass(frozen=True)
class Source:
    """One federation plane.

    key:         stable identifier the LLM selects by (allow-listed).
    cloud_label: human label surfaced in the result + system prompt.
    era:         the year/slice this plane contributes to the timeline.
    requires_cfg: True when the leg depends on runtime config (e.g. CH Cloud
                  credentials) and must be skipped when absent.
    build:       () -> SQL SELECT fragment projecting the common shape.
    """

    key: str
    cloud_label: str
    era: str
    requires_cfg: bool
    build: Callable[[], str]


# Minimum fare (USD) to count a trip. NYC's metered minimum is $2.50; trips
# below it are data junk ($0.01 fares with real tips) that wreck a tip rate.
FARE_FLOOR = 2.5


def _tip_pct(tip: str, fare: str) -> str:
    """Revenue-weighted tip rate: sum(tip)/sum(fare)*100.

    Aggregating the sums (rather than a row-wise avg(tip/fare)) is robust to the
    per-trip divide blow-ups that junk near-zero fares cause — and it is the
    conventional "tip rate" definition.
    """
    return f"round(sum({tip}) / nullIf(sum({fare}), 0) * 100, 2)"


def _fragment(cloud: str, era: str, table_expr: str, *, tip: str, fare: str,
              where: str = "") -> str:
    """Compose a normalized SELECT fragment for one plane."""
    where_sql = f"\n    WHERE {where}" if where else ""
    return (
        f"SELECT '{cloud}' AS cloud, '{era}' AS era,\n"
        f"       toInt64(count()) AS trips,\n"
        f"       {_tip_pct(tip, fare)} AS avg_tip_pct,\n"
        f"       round(avg({fare}), 2) AS avg_fare\n"
        f"    FROM {table_expr}{where_sql}"
    )


def _build_local() -> str:
    # Baked chDB store (in-process). Columns: pickup_datetime, tip_amount, fare_amount.
    return _fragment(
        "local (chDB)", "2024", _LOCAL_TABLE,
        tip="tip_amount", fare="fare_amount",
        where=f"fare_amount >= {FARE_FLOOR} AND toYear(pickup_datetime) = 2024",
    )


def _build_gcs() -> str:
    # ClickHouse public GCS archive (TSV-with-header, gzip). All cols arrive as
    # String, hence the toFloat64OrZero casts. trips_0.gz is Q3-2015.
    table = f"url('{_GCS_HISTORICAL}', 'TabSeparatedWithNames')"
    return _fragment(
        "GCS (ClickHouse public)", "2015", table,
        tip="toFloat64OrZero(tip_amount)", fare="toFloat64OrZero(fare_amount)",
        where=f"toFloat64OrZero(fare_amount) >= {FARE_FLOOR}",
    )


def _build_azure() -> str:
    # Azure Open Datasets (anonymous Parquet). Native cols: tipAmount, fareAmount.
    url = _azure_first_part_url(2018, 6)
    table = f"url('{url}', 'Parquet')"
    return _fragment(
        "Azure (Open Datasets)", "2018", table,
        tip="tipAmount", fare="fareAmount",
        where=f"fareAmount >= {FARE_FLOOR}",
    )


def _build_s3() -> str:
    # AWS S3 via NYC TLC CloudFront CDN (Parquet + browser UA to clear WAF).
    url = f"{_CDN_BASE}/yellow_tripdata_2025-06.parquet"
    table = (
        f"url('{url}', 'Parquet', 'auto', "
        f"headers('User-Agent'='{_CDN_USER_AGENT}'))"
    )
    return _fragment(
        "AWS S3 (TLC CDN)", "2025", table,
        tip="tip_amount", fare="fare_amount",
        where=f"fare_amount >= {FARE_FLOOR}",
    )


def _build_chc() -> str:
    # ClickHouse Cloud "Historical Lake" via native-secure remoteSecure().
    cfg = chc_credentials()
    if cfg is None:
        raise RuntimeError("ClickHouse Cloud credentials not configured")
    table = (
        f"remoteSecure('{cfg['host']}:{cfg['port']}', '{_CHC_TABLE}', "
        f"'{cfg['user']}', '{cfg['password']}')"
    )
    return _fragment(
        "ClickHouse Cloud", "2023", table,
        tip="tip_amount", fare="fare_amount",
        where=f"fare_amount >= {FARE_FLOOR} AND toYear(tpep_pickup_datetime) = 2023",
    )


# Ordered by era so the assembled timeline reads 2015 -> 2025 left to right.
_SOURCES: tuple[Source, ...] = (
    Source("gcs", "GCS (ClickHouse public)", "2015", False, _build_gcs),
    Source("azure", "Azure (Open Datasets)", "2018", False, _build_azure),
    Source("chc", "ClickHouse Cloud", "2023", True, _build_chc),
    Source("local", "local (chDB)", "2024", False, _build_local),
    Source("s3", "AWS S3 (TLC CDN)", "2025", False, _build_s3),
)

_SOURCE_BY_KEY: dict[str, Source] = {s.key: s for s in _SOURCES}

# Default federation set, in era order.
DEFAULT_SOURCE_KEYS: tuple[str, ...] = tuple(s.key for s in _SOURCES)


def allowed_source_keys() -> tuple[str, ...]:
    """The full allow-list of selectable source keys."""
    return tuple(_SOURCE_BY_KEY)


def resolve_sources(keys: list[str] | None) -> list[Source]:
    """Map requested keys -> Source objects, rejecting anything off the list.

    Validates loudly (ValueError) on unknown keys — the LLM must pick from the
    allow-list, never pass a URL or arbitrary source. None / empty selects the
    default federation set.
    """
    if not keys:
        return [_SOURCE_BY_KEY[k] for k in DEFAULT_SOURCE_KEYS]
    resolved: list[Source] = []
    for k in keys:
        key = k.strip().lower()
        if key not in _SOURCE_BY_KEY:
            raise ValueError(
                f"unknown federation source: {k!r} "
                f"(allowed: {', '.join(allowed_source_keys())})"
            )
        resolved.append(_SOURCE_BY_KEY[key])
    return resolved
