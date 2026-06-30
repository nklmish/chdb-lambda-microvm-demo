"""analyze_fleet_across_clouds — the multi-cloud federation tool.

One declarative chDB statement assembles a decade of NYC yellow-taxi tipping
from data that physically lives on four different clouds plus the agent's own
in-process store:

    2015 -> GCS   (ClickHouse public archive)
    2018 -> Azure (Open Datasets)
    2023 -> ClickHouse Cloud (the "Historical Lake")
    2024 -> local baked chDB (the agent's hot brain)
    2025 -> AWS S3 (NYC TLC CloudFront)

This is the demo's embodiment of the whitepaper's Pillar 2 (federation hub):
"one declarative SQL across local files, S3/GCS/Azure, and remote ClickHouse —
no connection pools, no credential brokering." The win it showcases is developer
+ token simplicity, NOT microseconds: a cross-cloud reach is network-bound (~a
few seconds). Pillar 1/3 (speed/stability) is shown by the materialization path
below — the federated result is written back into the local chDB store, so the
*second* identical call is served in milliseconds (archive-on-compressed).

Security posture (consistent with chdb_tools / weather_tools):
  - The model never supplies URLs, credentials, or SQL. It selects from an
    allow-list of named sources (cloud_sources.py); unknown keys raise.
  - ClickHouse Cloud credentials resolve from the environment, never the LLM.
  - Federation + cache I/O run through ONE long-lived chDB Session on DB_PATH
    (the whitepaper's Pillar-1 pattern); the zone tool uses a per-call Connection.
    The chDB embedded server is process-global and pinned to one path, so both
    share DB_PATH (verified to coexist).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

from chdb import session as _chs
from datastore.connection import Connection
from strands import tool

import db
from db import DB_PATH
from cloud_sources import FARE_FLOOR, Source, pg_config, resolve_sources

logger = logging.getLogger(__name__)

# Scenario B — the federated result is materialized into a real chDB MergeTree in
# the local store, and the cross-cloud read runs through the SAME engine. Both go
# through ONE long-lived chDB Session (the whitepaper's Pillar-1 pattern:
# `sess = chdb.session.Session(path)` — a persistent MergeTree queried at CPU
# speed). A long-lived session loads the store exactly once, so there is no
# per-call reload race on the process-global embedded server — which is what made
# a per-call Connection/chdb.query *write* flaky (the D.26-CHDB-CONTENTION class).
# Verified: this session coexists with the other tools' per-call Connections on
# the same path.
_CACHE_TABLE = "nyc_taxi.fleet_cache"

# Local baked taxi table (matches db.py / cloud_sources convention).
_LOCAL_TABLE = "nyc_taxi.yellow_trips"


# -- Long-lived chDB session (process-global, lazy) ---------------------------

_fed_session: "object | None" = None
_fed_session_path: "str | None" = None

# The chDB embedded server is process-global and the Session object is shared, so
# all access is serialized. Federation runs in a threadpool (sync Strands tools),
# and concurrent requests would otherwise hit one Session object on one embedded
# server at once — a deadlock/corruption risk. A re-entrant lock also lets one
# tool call chain several session ops (ensure → read/federate → write) safely.
_session_lock = threading.RLock()


def _session():
    """Return the process-wide federation Session, (re)creating it if the
    configured DB path changed (path changes only happen under test).

    Caller must hold _session_lock.
    """
    global _fed_session, _fed_session_path
    if _fed_session is None or _fed_session_path != db.DB_PATH:
        if _fed_session is not None:
            try:
                _fed_session.close()
            except Exception:  # noqa: BLE001
                pass
        _fed_session = _chs.Session(db.DB_PATH)
        _fed_session_path = db.DB_PATH
    return _fed_session


def _sess_records(sql: str) -> list[dict]:
    """Run a query through the long-lived session, returning JSON rows as dicts."""
    with _session_lock:
        res = _session().query(sql, "JSON")
        raw = res.bytes() if (res is not None and hasattr(res, "bytes")) else b""
    if not raw:
        return []
    return json.loads(raw).get("data", [])


def _sess_exec(sql: str) -> None:
    """Run a DDL/DML statement through the long-lived session."""
    with _session_lock:
        _session().query(sql)


def _rows_from_df(df) -> list[dict]:
    """DataFrame → list[dict] (used by the Connection-based zone tool)."""
    return df.to_dict(orient="records")


def _split_keys(sources: str) -> list[str] | None:
    """Parse the comma/space-separated `sources` arg into a key list (or None)."""
    keys = [s for s in re.split(r"[,\s]+", (sources or "").strip()) if s]
    return keys or None


def _build_fragments(srcs: list[Source]) -> tuple[list[tuple[Source, str]], list[str]]:
    """Build each plane's SELECT fragment, collecting notes for skipped legs.

    A leg is skipped (not fatal) when its source can't be built — e.g. an
    optional ClickHouse Cloud leg with no configured credentials, or a transient
    listing failure. This keeps the federation resilient on stage.
    """
    fragments: list[tuple[Source, str]] = []
    notes: list[str] = []
    for s in srcs:
        try:
            fragments.append((s, s.build()))
        except Exception as exc:  # noqa: BLE001 — degrade per leg, never hard-fail
            notes.append(f"{s.cloud_label} skipped: {exc}")
            logger.info("federation leg skipped: %s — %s", s.cloud_label, exc)
    return fragments, notes


def _assemble_union(fragments: list[tuple[Source, str]]) -> str:
    """Compose the single UNION ALL statement, ordered into a timeline.

    The union is wrapped in a subquery so the ORDER BY applies globally —
    ClickHouse otherwise binds a trailing ORDER BY to the last UNION member only.
    """
    body = "\n    UNION ALL\n".join(frag for _, frag in fragments)
    return f"SELECT * FROM (\n{body}\n) ORDER BY era"


def _federate(fragments: list[tuple[Source, str]]) -> tuple[list[dict], str]:
    """Run the federated query: try one statement, fall back per leg.

    Executes through the stateless db.query_records (chdb.query(path=DB_PATH)) so
    the local `yellow_trips` table resolves and the remote table functions
    (url/s3/remoteSecure) run — all in ONE statement. The single-statement path is
    the headline ("one SQL, four clouds"); if it throws (one unreachable cloud
    takes the whole statement down) we re-run each leg independently so a transient
    outage degrades to partial results instead of a hard failure.
    """
    union_sql = _assemble_union(fragments)
    try:
        return _sess_records(union_sql), "single-statement"
    except Exception as exc:  # noqa: BLE001
        logger.info("single-statement federation failed, per-leg fallback: %s", exc)
        rows: list[dict] = []
        for s, frag in fragments:
            try:
                rows.extend(_sess_records(frag))
            except Exception as leg_exc:  # noqa: BLE001
                logger.info("federation leg failed: %s — %s", s.cloud_label, leg_exc)
        rows.sort(key=lambda r: str(r.get("era", "")))
        return rows, "per-leg fallback"


# -- Scenario B: materialize into a local chDB MergeTree (long-lived Session) --

def _ensure_cache_table() -> None:
    """Create the federation cache MergeTree if absent (idempotent)."""
    _sess_exec(
        f"CREATE TABLE IF NOT EXISTS {_CACHE_TABLE} ("
        "sig String, cloud String, era String, "
        "trips Int64, avg_tip_pct Float64, avg_fare Float64, "
        "cached_at DateTime DEFAULT now()"
        ") ENGINE = MergeTree ORDER BY (sig, era)"
    )


def _read_cache(sig: str) -> list[dict]:
    """Return the most recently materialized rows for this source signature.

    LIMIT 1 BY (cloud, era) on cached_at DESC keeps the latest run per row, so the
    table can be appended to without the reader ever seeing stale duplicates.
    """
    safe_sig = sig.replace("'", "''")
    return _sess_records(
        "SELECT cloud, era, trips, avg_tip_pct, avg_fare FROM ("
        "SELECT cloud, era, trips, avg_tip_pct, avg_fare "
        f"FROM {_CACHE_TABLE} WHERE sig = '{safe_sig}' "
        "ORDER BY cached_at DESC LIMIT 1 BY cloud, era"
        ") ORDER BY era"
    )


def _write_cache(sig: str, rows: list[dict]) -> None:
    """Trickle the small aggregated result into the local MergeTree."""
    if not rows:
        return
    safe_sig = sig.replace("'", "''")
    values = ", ".join(
        "('{sig}', '{cloud}', '{era}', {trips}, {tip}, {fare})".format(
            sig=safe_sig,
            cloud=str(r["cloud"]).replace("'", "''"),
            era=str(r["era"]).replace("'", "''"),
            trips=int(r["trips"]),
            tip=float(r["avg_tip_pct"]),
            fare=float(r["avg_fare"]),
        )
        for r in rows
    )
    _sess_exec(
        f"INSERT INTO {_CACHE_TABLE} "
        f"(sig, cloud, era, trips, avg_tip_pct, avg_fare) VALUES {values}"
    )


@tool
def analyze_fleet_across_clouds(sources: str = "", refresh: bool = False) -> str:
    """Federate a decade of NYC taxi tipping across S3, Azure, GCS and ClickHouse Cloud.

    Answers questions like "how has NYC taxi tipping changed over the years, and
    where does each year's data live?" by issuing ONE chDB SQL statement that
    reads trips from four different clouds plus the local baked store, normalizes
    them, and returns avg tip %, avg fare, and trip count per year.

    Each year is served from a different plane (provenance is the point):
      2015 GCS (ClickHouse public)  | 2018 Azure (Open Datasets)
      2023 ClickHouse Cloud         | 2024 local chDB | 2025 AWS S3 (TLC CDN)

    The first call reaches across the clouds (network-bound, a few seconds) and
    materializes the result into a local chDB MergeTree (nyc_taxi.fleet_cache); an
    identical follow-up is served from that local table in ~milliseconds —
    demonstrating "federate to reach, localize to think" (archive-on-compressed).
    Pass refresh=true to force a fresh cross-cloud read.

    Args:
        sources: Optional comma-separated subset of source keys to federate.
                 Allowed: gcs, azure, chc, local, s3. Empty = all of them.
        refresh: If True, bypass the local cache and re-run the cross-cloud query.

    Returns:
        JSON string: {"data": [...per-year rows...], "row_count": N, "mode": ...,
        "sources_used": [...], "elapsed_ms": N, "sql": "<the federated SQL>",
        "source": "...", and optional "notes": [...]}.
    """
    srcs = resolve_sources(_split_keys(sources))
    fragments, notes = _build_fragments(srcs)
    if not fragments:
        raise RuntimeError(
            "no federation sources available (all legs skipped): " + "; ".join(notes)
        )

    used = [s.cloud_label for s, _ in fragments]
    sig = ",".join(sorted(s.key for s, _ in fragments))
    union_sql = _assemble_union(fragments)

    _ensure_cache_table()

    # Scenario B — serve from the local chDB materialized cache when present.
    if not refresh:
        t0 = time.time()
        cached = _read_cache(sig)
        elapsed_ms = round((time.time() - t0) * 1000, 1)
        if cached:
            payload = {
                "data": cached,
                "row_count": len(cached),
                "mode": "local cache (materialized)",
                "sources_used": used,
                "elapsed_ms": elapsed_ms,
                "sql": union_sql,
                "source": (
                    "Served from a local chDB MergeTree (nyc_taxi.fleet_cache) — a "
                    "materialized prior federation across " + ", ".join(used) + ". "
                    "Re-queried in-process via chDB SQL (archive-on-compressed); "
                    "pass refresh=true to re-federate."
                ),
            }
            if notes:
                payload["notes"] = notes
            return json.dumps(payload)

    # Cross-cloud reach — one federated statement via stateless chDB query.
    t0 = time.time()
    rows, mode = _federate(fragments)
    elapsed_ms = round((time.time() - t0) * 1000, 1)

    # Materialize into the local chDB MergeTree for instant re-query.
    try:
        _write_cache(sig, rows)
    except Exception as exc:  # noqa: BLE001 — caching is best-effort
        notes.append(f"cache write skipped: {exc}")

    payload = {
        "data": rows,
        "row_count": len(rows),
        "mode": mode,
        "sources_used": used,
        "elapsed_ms": elapsed_ms,
        "sql": union_sql,
        "source": (
            "One chDB statement federated across " + ", ".join(used) + " — "
            "NYC TLC yellow-taxi tipping by year, each year read from the cloud "
            "where it natively lives (S3, Azure, GCS, ClickHouse Cloud, local). "
            "Result materialized into a local chDB MergeTree for instant re-query."
        ),
    }
    if notes:
        payload["notes"] = notes
    return json.dumps(payload)


# -- Zone enrichment: local chDB trips JOIN PostgreSQL zone lookup ------------

def _build_zone_sql(year: int, top_n: int) -> str:
    """One chDB statement: local taxi trips JOINed to a PostgreSQL zone lookup.

    This is the paper's third-party-RDBMS federation leg (Fig 3 / the hero SQL's
    `postgresql()`). chDB reads the local MergeTree and the remote Postgres table
    as two tables in a single declarative JOIN — no ORM, no connection pool.
    """
    pg = pg_config()
    pg_fn = (
        f"postgresql('{pg['host']}:{pg['port']}', '{pg['db']}', "
        f"'{pg['table']}', '{pg['user']}', '{pg['password']}')"
    )
    return (
        "SELECT z.borough AS borough, z.zone AS zone,\n"
        "       toInt64(count()) AS trips,\n"
        "       round(sum(t.tip_amount) / nullIf(sum(t.fare_amount), 0) * 100, 2) AS tip_pct,\n"
        "       round(avg(t.fare_amount), 2) AS avg_fare\n"
        f"FROM {_LOCAL_TABLE} AS t\n"
        f"INNER JOIN {pg_fn} AS z ON t.pickup_location_id = z.location_id\n"
        f"WHERE t.fare_amount >= {FARE_FLOOR} AND toYear(t.pickup_datetime) = {int(year)}\n"
        "GROUP BY borough, zone\n"
        "HAVING count() >= 500\n"
        f"ORDER BY tip_pct DESC\nLIMIT {int(top_n)}"
    )


@tool
def analyze_zone_tipping(year: int = 2024, top_n: int = 10) -> str:
    """Rank NYC pickup zones by tip rate, joining local trips to a Postgres lookup.

    Answers "which zones tip best?" by issuing ONE chDB statement that JOINs the
    local baked taxi trips (an in-process MergeTree) against a PostgreSQL
    `taxi_zones` lookup table — the federation hub treating a third-party RDBMS
    as just another table, with no ORM or connection pool. Tip rate is
    revenue-weighted (sum(tip)/sum(fare)); zones with under 500 trips are
    excluded as noise.

    Args:
        year: Calendar year of trips to rank (default 2024 — the baked year).
        top_n: Number of top zones to return (default 10).

    Returns:
        JSON string: {"data": [{borough, zone, trips, tip_pct, avg_fare}...],
        "row_count": N, "elapsed_ms": N, "sql": "<the JOIN>", "source": "..."}.
    """
    top_n = max(1, min(int(top_n), 100))
    sql = _build_zone_sql(year, top_n)

    conn = Connection(database=DB_PATH)
    conn.connect()
    try:
        t0 = time.time()
        df = conn.execute(sql, output_format="Dataframe").to_df()
        elapsed_ms = round((time.time() - t0) * 1000, 1)
    finally:
        conn.close()

    rows = _rows_from_df(df)
    return json.dumps({
        "data": rows,
        "row_count": len(rows),
        "elapsed_ms": elapsed_ms,
        "sql": sql,
        "source": (
            f"One chDB statement joining local baked NYC taxi trips ({year}) to a "
            "PostgreSQL taxi-zone lookup (postgresql() table function) — top "
            f"{top_n} pickup zones by revenue-weighted tip rate."
        ),
    })
