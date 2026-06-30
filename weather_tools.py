"""analyze_weather_impact — S3 Files weather enrichment tool.

Joins NYC taxi trips against NOAA GSOD weather via a raw-SQL subquery-wrapped
JOIN executed through Connection.execute(). This approach is required because
chdb-ds's .assign().join() lazy API emits flat-FROM JOIN SQL that cannot
resolve computed SELECT-list aliases in the ON clause (ClickHouse Code: 47
UNKNOWN_IDENTIFIER). See for full rationale.

Design:
  - One @tool public function. No private helper (_get_weather_ds removed).
  - Raw SQL template with subquery-wrapped JOIN (left side wraps taxi table,
    right side wraps s3() table function with FRSHTT classifier).
  - weather_condition / metric validated at the top with ValueError.
  - month=0 means "all months" — WHERE clause omitted.
  - weather_condition="all" skips the weather_category WHERE clause.
  - FRSHTT classifier uses ClickHouse substring() (1-indexed):
      substring(FRSHTT, 2, 1) = '1' → Rain  (pandas str.get(1))
      substring(FRSHTT, 1, 1) = '1' → Fog   (pandas str.get(0))
      substring(FRSHTT, 5, 1) = '1' → Thunder (pandas str.get(4))
      otherwise                      → Clear
  - Priority order: Rain > Fog > Thunder > Clear (first matching CASE branch).
  - Connection(database=DB_PATH) consistent with pattern.
"""
from __future__ import annotations
import json
import logging
import os

from datastore.connection import Connection
from strands import tool

from db import DB_PATH

logger = logging.getLogger(__name__)


# -- Whitelists ---------------------------------------------------------------

STATIONS = {
    "72503014732": "LaGuardia Airport",
    "72505394728": "Central Park",
    "74486094789": "JFK Airport",
}

# Metric → (agg_expr, alias) for SQL generation.
_METRIC_SQL = {
    "trip_count":    "toInt64(count(t.pickup_datetime)) AS trip_count",
    "avg_fare":      "avg(t.fare_amount) AS avg_fare",
    "avg_tip":       "avg(t.tip_amount) AS avg_tip",
    "avg_distance":  "avg(t.trip_distance) AS avg_distance",
    "total_revenue": "sum(t.total_amount) AS total_revenue",
}

# lowercase user input → title-case label used in SQL WHERE clause.
_CONDITION_LABEL = {
    "rain":    "Rain",
    "fog":     "Fog",
    "thunder": "Thunder",
    "clear":   "Clear",
}

# Taxi table (fully-qualified, quote_char="" convention from db.py).
_TAXI_TABLE = "nyc_taxi.yellow_trips"

# NOAA GSOD S3 base URL (public, NOSIGN).
_NOAA_S3_BASE = "s3://noaa-gsod-pds"


# -- Public tool --------------------------------------------------------------

@tool
def analyze_weather_impact(
    year: int,
    month: int = 0,
    weather_condition: str = "all",
    metric: str = "trip_count",
) -> str:
    """Analyze weather's impact on NYC taxi ridership and fares.

    Joins NYC TLC yellow_trips against NOAA GSOD weather (LaGuardia station)
    on the trip pickup date and aggregates by weather category.

    IMPORTANT — data availability: the baked taxi dataset has volume only
    for year 2024 (~9.5M trips); other years (2023, 2009, 2008, 2002) are
    sparse stubs (under 20 trips total). Default to year=2024 unless the
    user explicitly asks about another year. Querying a stub year will
    return 0-20 rows and is not representative.

    Args:
        year: Calendar year to analyze. Use 2024 by default (only year with
              meaningful trip volume in the baked dataset).
        month: Month 1-12, or 0 for all months.
        weather_condition: "all", "rain", "clear", "fog", or "thunder".
        metric: "trip_count", "avg_fare", "avg_tip", "avg_distance", or "total_revenue".

    Returns:
        JSON string: {"data": [...rows...], "row_count": N}.
    """
    # Validate loud and early, before any SQL work.
    if weather_condition not in ("all", "rain", "clear", "fog", "thunder"):
        raise ValueError(
            f"weather_condition must be one of: all, rain, clear, fog, thunder "
            f"(got {weather_condition!r})"
        )
    if metric not in _METRIC_SQL:
        raise ValueError(
            f"metric must be one of: {', '.join(sorted(_METRIC_SQL))} (got {metric!r})"
        )

    # Default station: LaGuardia Airport.
    station = "72503014732"

    # Resolve weather data source: prefer S3 Files NFS mount, fall back to s3().
    mount_path = os.getenv("WEATHER_MOUNT_PATH", "/mnt/noaa-gsod")
    csv_path = f"{mount_path}/{year}/{station}.csv"
    if os.path.exists(csv_path):
        # S3 Files NFS mount — use file() table function.
        weather_source = f"file('{csv_path}', CSVWithNames)"
        logger.info("weather source: S3 Files mount via file() — %s", csv_path)
    else:
        # Public NOAA S3 — use s3() table function with NOSIGN.
        s3_url = f"{_NOAA_S3_BASE}/{year}/{station}.csv"
        weather_source = f"s3('{s3_url}', NOSIGN, 'CSVWithNames')"
        logger.info("weather source: direct S3 fallback via s3() — mount %s absent", csv_path)

    # Optional WHERE clause for month filter (applied inside the taxi subquery).
    month_where = f"WHERE toMonth(pickup_datetime) = {int(month)}" if month else ""

    # Optional WHERE clause for weather condition filter (applied after JOIN).
    if weather_condition != "all":
        label = _CONDITION_LABEL[weather_condition]
        condition_where = f"WHERE w.weather_category = '{label}'"
    else:
        condition_where = ""

    # Metric aggregation expression.
    agg_expr = _METRIC_SQL[metric]

    # Subquery-wrapped JOIN SQL.
    # Left side: taxi subquery computes trip_date alias inside the subquery scope.
    # Right side: weather subquery classifies FRSHTT into weather_category.
    # JOIN ON: t.trip_date = w.weather_date — both aliases are real columns in
    # their respective subquery scopes, so ClickHouse can resolve them.
    sql = f"""
SELECT
    w.weather_category,
    {agg_expr}
FROM (
    SELECT *, toDate(pickup_datetime) AS trip_date
    FROM {_TAXI_TABLE}
    {month_where}
) AS t
INNER JOIN (
    SELECT
        toDate(DATE) AS weather_date,
        multiIf(
            substring(leftPad(toString(FRSHTT), 6, '0'), 2, 1) = '1', 'Rain',
            substring(leftPad(toString(FRSHTT), 6, '0'), 1, 1) = '1', 'Fog',
            substring(leftPad(toString(FRSHTT), 6, '0'), 5, 1) = '1', 'Thunder',
            'Clear'
        ) AS weather_category
    FROM {weather_source}
) AS w
ON t.trip_date = w.weather_date
{condition_where}
GROUP BY w.weather_category
ORDER BY w.weather_category ASC
""".strip()

    # Execute via Connection — consistent with DB_PATH binding.
    conn = Connection(database=DB_PATH)
    conn.connect()
    try:
        result_df = conn.execute(sql, output_format="Dataframe").to_df()
    finally:
        conn.close()

    rows = result_df.to_dict(orient="records")
    source = (
        f"NOAA GSOD weather data from {STATIONS['72503014732']} (station 72503014732); "
        f"NYC TLC yellow_trips {year}"
    )
    return json.dumps({"data": rows, "row_count": len(rows), "source": source})
