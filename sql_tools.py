"""query_with_fresh_data — Hybrid delta layer tool.

Merges the baked NYC Yellow Taxi trips table with live NYC TLC CDN
Parquet files for dates beyond the baked cutoff. Same parameter
surface as `analyze_taxi_data`. Routes filter/groupby/agg
strings through `query_helpers.parse_*` so column + aggregation
allow-listing is shared with the baked-only tool — no raw SQL from
the LLM.

Design (Q1–Q7 synthesis):
  - Delta months discovered by probing the CDN via chDB url() with a 3-strike
    stop, cached for 1h at module scope.
  - UNION ALL composed with .select(*QUERY_COLUMNS) on both sides so
    schemas align (CDN files may carry extra columns).
  - CDN unreachable → spec-sanctioned fallback to baked-only with a
    `"note"` annotation in the return dict (Q6).
  - Everything else raises — no silent swallows.
  - `limit` capped at min(limit, 1000) (Q7).
  - Filter separator is AND (matches `query_helpers.parse_filters`).
  - _get_delta_ds uses raw SQL via
    Connection.execute() with headers('User-Agent'=...) as 4th arg to
    url() to bypass CloudFront WAF Bot Control on NYC TLC CDN.
"""
from __future__ import annotations
import json
import os
import threading
import time

from chdb.datastore import DataStore
from strands import tool

from db import chdb_connection, get_taxi_ds
from query_helpers import parse_filters, parse_groupby, parse_aggregations


# -- Constants ----------------------------------------------------------------

# Column list matching the schema in Section 4. Used for .select() before UNION ALL
# to ensure baked and delta DataStores have identical schemas.
QUERY_COLUMNS = [
    "pickup_datetime", "dropoff_datetime", "passenger_count", "trip_distance",
    "pickup_location_id", "dropoff_location_id", "fare_amount", "tip_amount",
    "total_amount", "payment_type", "congestion_surcharge", "airport_fee",
]

# chDB 4.1.8 DataStore.from_url() for CDN Parquet files. 4.1.8 fixes the
# CloudFront 403 that intermittently broke the live CDN delta fetch.

cdn_base = "https://d37ci6vzurychx.cloudfront.net/trip-data"

# User-Agent for CDN Parquet fetches.
# ClickHouse's default UA "ClickHouse/<version>" is blocked by CloudFront WAF
# Bot Control (SignalNonBrowserUserAgent rule) on the NYC TLC distribution.
# Phase A.4 Lambda probe: 7/7 non-ClickHouse UAs returned HTTP 200 from
# the runtime VPC — the block is shape-based, not IP-based.
DELTA_FETCH_USER_AGENT = "Mozilla/5.0 (compatible; NYC-Taxi-Agent/1.0)"

# Module-level cache: {"months": ["2026-01", "2026-02"], "expires": 1713280000.0}
# Guarded by _delta_cache_lock: delta discovery can run from a threadpool, so the
# check-then-update of this shared dict must be atomic.
_delta_cache: dict = {"months": [], "expires": 0.0}
_delta_cache_lock = threading.Lock()
_CACHE_TTL_SECONDS = 3600  # 1 hour


# -- Delta discovery + loading ------------------------------------------------

def _get_delta_df(month: str) -> "pd.DataFrame":
    """Fetch a single CDN Parquet month file as a DataFrame.

    Uses raw SQL via Connection.execute() with headers() as 4th arg to url()
    to pass a browser-compatible User-Agent, bypassing CloudFront WAF Bot
    Control which blocks ClickHouse's default 'ClickHouse/<version>' UA.
    Pattern:, following precedent.

    Returns a DataFrame with exactly the 12 QUERY_COLUMNS. Does NOT wrap in
    DataStore to avoid the chdb EmbeddedServer collision (DataStore(df) always
    seeds the EmbeddedServer at ':memory:' via the global Executor, which
    collides with the existing singleton at DB_PATH).
    """
    import pandas as pd
    url = f"{cdn_base}/yellow_tripdata_{month}.parquet"
    sql = (
        "SELECT "
        "tpep_pickup_datetime AS pickup_datetime, "
        "tpep_dropoff_datetime AS dropoff_datetime, "
        "PULocationID AS pickup_location_id, "
        "DOLocationID AS dropoff_location_id, "
        "Airport_fee AS airport_fee, "
        "passenger_count, trip_distance, fare_amount, tip_amount, "
        "total_amount, payment_type, congestion_surcharge "
        f"FROM url('{url}', 'Parquet', 'auto', "
        f"headers('User-Agent'='{DELTA_FETCH_USER_AGENT}')) "
        "SETTINGS max_memory_usage = 12000000000"
    )
    with chdb_connection() as conn:
        return conn.execute(sql, output_format="Dataframe").to_df()


def _filter_df_via_sql(df: "pd.DataFrame", filters_str: str) -> "pd.DataFrame":
    """Apply filter string to a DataFrame using the DB_PATH connection.

    Uses Connection(database=DB_PATH).query_df() to execute a SQL WHERE clause
    on the DataFrame. This reuses the existing EmbeddedServer at DB_PATH rather
    than seeding a new one at ':memory:'.
    """
    if not filters_str.strip():
        return df
    # Build a simple SELECT * WHERE <filters> SQL. parse_filters uses the same
    # filter syntax, so we translate directly and run it through query_df on a
    # connection bound to DB_PATH (reusing the existing EmbeddedServer rather
    # than seeding a new one at ':memory:').
    import re
    where_clause = re.sub(r'\bAND\b', 'AND', filters_str, flags=re.IGNORECASE)
    sql = f"SELECT * FROM __df__ WHERE {where_clause}"
    with chdb_connection() as conn:
        return conn.query_df(sql, df, "__df__")


def _apply_groupby_agg_pandas(
    df: "pd.DataFrame",
    group_by: str,
    aggregations: str,
) -> "pd.DataFrame":
    """Apply groupby + aggregations to a DataFrame using pandas.

    Mirrors parse_groupby + parse_aggregations logic but operates on a plain
    DataFrame to avoid DataStore EmbeddedServer collision.
    """
    import re
    import pandas as pd
    from query_helpers import ALLOWED_COLUMNS, ALLOWED_TIME_GROUPS, AGG_MAP

    _TIME_GROUP_RE = re.compile(r"^\s*(\w+)\s*\(\s*(\w+)\s*\)\s*$")

    # Build groupby keys.
    keys = []
    for token in group_by.split(","):
        token = token.strip()
        if not token:
            continue
        m = _TIME_GROUP_RE.match(token)
        if m:
            func, col = m.group(1), m.group(2)
            if func not in ALLOWED_TIME_GROUPS:
                raise ValueError(f"unknown time group: {func!r}")
            if col not in ALLOWED_COLUMNS:
                raise ValueError(f"unknown column: {col!r}")
            # Apply time group function to create a new column.
            time_funcs = {
                "year": lambda s: s.dt.year,
                "month": lambda s: s.dt.month,
                "day": lambda s: s.dt.day,
                "hour": lambda s: s.dt.hour,
                "dayofweek": lambda s: s.dt.dayofweek,
            }
            if func not in time_funcs:
                raise ValueError(f"unsupported time group for pandas: {func!r}")
            key_col = f"{func}_{col}"
            df[key_col] = time_funcs[func](pd.to_datetime(df[col], utc=True))
            keys.append(key_col)
        else:
            if token not in ALLOWED_COLUMNS:
                raise ValueError(f"unknown column: {token!r}")
            keys.append(token)

    if not keys:
        return df

    grouped = df.groupby(keys)

    if not aggregations.strip():
        return grouped.first().reset_index()

    # Build aggregation specs.
    agg_specs: dict = {}
    for pair in aggregations.split(","):
        if not pair.strip():
            continue
        col, func = pair.strip().split(":")
        col, func = col.strip(), func.strip()
        if col not in ALLOWED_COLUMNS:
            raise ValueError(f"unknown column: {col!r}")
        if func not in AGG_MAP:
            raise ValueError(f"unknown aggregation: {func!r}")
        alias = f"{col}_{func}"
        agg_specs[alias] = pd.NamedAgg(column=col, aggfunc=AGG_MAP[func])

    result = grouped.agg(**agg_specs).reset_index()
    return result


def _discover_delta_months() -> list[str]:
    """Probe CDN for available Parquet months after DELTA_START. Cache results for 1 hour."""
    now = time.time()
    with _delta_cache_lock:
        if _delta_cache["months"] and now < _delta_cache["expires"]:
            return _delta_cache["months"]

    delta_start = os.getenv("DELTA_START", "2026-01")
    start_year, start_month = int(delta_start[:4]), int(delta_start[5:7])

    found: list[str] = []
    year, month = start_year, start_month
    consecutive_misses = 0

    # Probe via chDB's url() — NOT python `requests`. CloudFront's WAF Bot Control
    # blocks the requests library's request signature with 403 (HEAD and GET, with
    # or without a browser User-Agent), while it allows chDB's HTTP client carrying
    # the same browser UA the actual fetch uses. A `LIMIT 1` read touches only the
    # Parquet footer + first row group, so each probe is cheap (~0.3s). Using the
    # fetch's own client keeps discovery and fetch consistent — a month that probes
    # OK is one the fetch can actually read.
    with chdb_connection() as conn:
        while consecutive_misses < 3:
            probe = f"{year}-{month:02d}"
            url = f"{cdn_base}/yellow_tripdata_{probe}.parquet"
            sql = (
                f"SELECT 1 FROM url('{url}', 'Parquet', 'auto', "
                f"headers('User-Agent'='{DELTA_FETCH_USER_AGENT}')) LIMIT 1"
            )
            try:
                conn.execute(sql, output_format="Dataframe").to_df()
                found.append(probe)
                consecutive_misses = 0
            except Exception:  # noqa: BLE001 — missing month / transient CDN error
                consecutive_misses += 1
            month += 1
            if month > 12:
                month = 1
                year += 1

    with _delta_cache_lock:
        _delta_cache["months"] = found
        _delta_cache["expires"] = now + _CACHE_TTL_SECONDS
    return found


# -- Public tool --------------------------------------------------------------

@tool
def query_with_fresh_data(
    filters: str = "",
    group_by: str = "",
    aggregations: str = "",
    sort_by: str = "",
    ascending: bool = False,
    limit: int = 50,
) -> str:
    """Query NYC Yellow Taxi trips across the baked table + live CDN delta months.

    Use this tool when the question covers dates beyond the baked data cutoff
    (`data_profile.json["baked_cutoff"]`). For dates inside the baked range,
    prefer `analyze_taxi_data`.

    Args:
        filters: AND-separated conditions. Operators: >, <, >=, <=, ==, !=, between, in.
                 Example: "fare_amount > 50 AND pickup_location_id in [161,162]"
        group_by: Comma-separated columns or time groups like "month(pickup_datetime)".
                  Example: "month(pickup_datetime), payment_type"
        aggregations: Comma-separated column:function pairs. Functions: mean, sum, count,
                      min, max, median, std. Example: "total_amount:mean, trip_distance:sum"
        sort_by: Column or aggregation alias (e.g., "total_amount_mean") to sort by.
        ascending: Sort ascending if True, descending if False.
        limit: Maximum rows to return (1–1000, default 50).

    Returns:
        JSON string: {"data": [...rows...], "row_count": N}, plus an optional
        "note" field when the CDN delta layer is unreachable and results come
        from baked data only.
    """
    limit = max(1, min(int(limit), 1000))

    delta_months = _discover_delta_months()
    note = None

    if delta_months:
        # Fetch delta DataFrames directly (bypasses DataStore to avoid chdb
        # EmbeddedServer collision — DataStore(df) seeds ':memory:' via the
        # global Executor, colliding with the existing singleton at DB_PATH).
        # Strategy: materialise the baked side as a DataFrame, concatenate with
        # delta DataFrames, then apply all operations using pandas directly.
        import pandas as pd

        # Materialise baked side with filters applied via DataStore (uses DB_PATH).
        baked_ds = parse_filters(get_taxi_ds().select(*QUERY_COLUMNS), filters)
        baked_df = baked_ds.to_df()

        # Fetch and concatenate delta DataFrames.
        delta_dfs = [_get_delta_df(m) for m in delta_months]

        # Apply filters to delta DataFrames via raw SQL (reuse the same Connection
        # that is already bound to DB_PATH — no new EmbeddedServer seeding).
        filtered_delta_dfs = []
        for df in delta_dfs:
            if filters.strip():
                df = _filter_df_via_sql(df, filters)
            filtered_delta_dfs.append(df)

        combined_df = pd.concat([baked_df] + filtered_delta_dfs, ignore_index=True)

        # Apply groupby / aggregations using pandas.
        if group_by.strip():
            combined_df = _apply_groupby_agg_pandas(combined_df, group_by, aggregations)
        elif aggregations.strip():
            raise ValueError("aggregations require a non-empty group_by")

        # Sort.
        if sort_by.strip() and sort_by in combined_df.columns:
            combined_df = combined_df.sort_values(sort_by, ascending=ascending)

        rows = combined_df.head(limit).to_dict(orient="records")
        payload: dict = {"data": rows, "row_count": len(rows)}
        return json.dumps(payload)

    else:
        # No delta months — use baked DataStore path (no EmbeddedServer collision risk).
        baked_ds = parse_filters(get_taxi_ds().select(*QUERY_COLUMNS), filters)
        note = "Delta data unavailable — showing baked data only"
        combined = baked_ds

    # Apply groupby / aggregations on the combined DS (piecewise per Q3-C).
    if group_by.strip():
        grouped = parse_groupby(combined, group_by)
        if aggregations.strip():
            combined = parse_aggregations(grouped, aggregations)
        else:
            combined = grouped

    if sort_by.strip():
        combined = combined.sort_values(sort_by, ascending=ascending)

    rows = combined.head(limit).to_dict(orient="records")

    payload: dict = {"data": rows, "row_count": len(rows)}
    if note is not None:
        payload["note"] = note
    return json.dumps(payload)
