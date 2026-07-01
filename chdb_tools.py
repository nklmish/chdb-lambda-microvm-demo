import json

from strands import tool

from query_helpers import execute_query_pipeline


@tool
def analyze_taxi_data(
    filters: str = "",
    group_by: str = "",
    aggregations: str = "",
    sort_by: str = "",
    ascending: bool = False,
    limit: int = 50,
) -> str:
    """Query the baked NYC Yellow Taxi trip database (2002-2024).

    Use this tool for any analytical question about taxi trips within the
    baked data range. Supports filtering, time-bucketing, and aggregations.

    Args:
        filters: AND-separated conditions.
                 Operators: >, <, >=, <=, ==, !=, between, in.
                 Example: "fare_amount > 50 AND pickup_location_id in [161,162]"
        group_by: Comma-separated columns or time-bucket function calls.
                  Time functions — call with column in parens, NOT dot syntax:
                    hour(col), month(col), year(col), dayofweek(col), quarter(col)
                  Examples:
                    "hour(pickup_datetime)"                 -- hour-of-day (0..23)
                    "month(pickup_datetime), payment_type"  -- month and payment
                    "dayofweek(pickup_datetime)"            -- 0=Mon .. 6=Sun
        aggregations: Comma-separated column:function pairs.
                      Functions: mean, sum, count, min, max, median, std.
                      Result columns are aliased as "<col>_<func>" — e.g.
                      "trip_distance:count" produces a column named
                      "trip_distance_count".
                      Example: "fare_amount:mean, total_amount:sum"
        sort_by: Column or aggregation alias to sort by. For aggregated queries
                 use the "<col>_<func>" alias from the aggregations argument.
        ascending: Sort ascending if True, descending if False (default False).
        limit: Maximum rows to return (1-1000, default 50).

    Common patterns:
      Busiest hour of day:
        group_by="hour(pickup_datetime)"
        aggregations="trip_distance:count"
        sort_by="trip_distance_count"
        limit=24

      Average fare by payment type:
        group_by="payment_type"
        aggregations="fare_amount:mean"

      Monthly ridership trend:
        group_by="year(pickup_datetime), month(pickup_datetime)"
        aggregations="trip_distance:count"
        sort_by="trip_distance_count"

    Returns:
        JSON string with data array and row count.
    """
    from db import chdb_connection
    from query_helpers import (
        ALLOWED_COLUMNS,
        AGG_MAP,
        ALLOWED_TIME_GROUPS,
        _coerce,
        _BETWEEN_RE,
        _IN_RE,
        _AND_RE,
    )
    import re as _re

    # ----- ClickHouse SQL function names (chDB embedded ClickHouse) -----
    _TIME_SQL = {
        "hour":      "toHour",
        "month":     "toMonth",
        "year":      "toYear",
        "dayofweek": "toDayOfWeek",
        "quarter":   "toQuarter",
    }
    _AGG_SQL = {
        "mean":   "avg",
        "sum":    "sum",
        "count":  "count",
        "min":    "min",
        "max":    "max",
        "median": "median",
        "std":    "stddevPop",
    }
    _OP_SQL = {
        ">=": ">=", "<=": "<=", ">": ">", "<": "<",
        "==": "=",  "!=": "!=",
    }

    def _sql_lit(v):
        # _coerce returns int / float / str; SQL-quote strings, leave numerics bare.
        return repr(v) if isinstance(v, str) else str(v)

    _TIME_RE = _re.compile(r"^\s*(\w+)\s*\(\s*(\w+)\s*\)\s*$")

    # ----- group_by → SELECT prefix + GROUP BY clause -----
    select_keys = []   # "toHour(pickup_datetime) AS hour_pickup_datetime"
    group_keys  = []   # "hour_pickup_datetime"
    for token in (group_by or "").split(","):
        token = token.strip()
        if not token:
            continue
        m = _TIME_RE.match(token)
        if m:
            fn, col = m.group(1), m.group(2)
            if fn not in ALLOWED_TIME_GROUPS:
                raise ValueError(f"unknown time function: {fn!r}")
            if col not in ALLOWED_COLUMNS:
                raise ValueError(f"unknown column: {col!r}")
            alias = f"{fn}_{col}"
            select_keys.append(f"{_TIME_SQL[fn]}({col}) AS {alias}")
            group_keys.append(alias)
        else:
            if token not in ALLOWED_COLUMNS:
                raise ValueError(f"unknown column: {token!r}")
            select_keys.append(token)
            group_keys.append(token)

    # ----- aggregations → SELECT suffix -----
    agg_select = []
    agg_aliases = []
    if (aggregations or "").strip():
        if not group_keys:
            raise ValueError("aggregations require a non-empty group_by")
        for pair in aggregations.split(","):
            if not pair.strip():
                continue
            col, fn = [x.strip() for x in pair.split(":")]
            if col not in ALLOWED_COLUMNS:
                raise ValueError(f"unknown column: {col!r}")
            if fn not in _AGG_SQL:
                raise ValueError(f"unknown aggregation: {fn!r}")
            alias = f"{col}_{fn}"
            expr = f"{_AGG_SQL[fn]}({col})"
            if fn == "count":
                expr = f"toInt64({expr})"   # nicer integer output
            agg_select.append(f"{expr} AS {alias}")
            agg_aliases.append(alias)

    select_clause = ", ".join(select_keys + agg_select) if (select_keys or agg_select) else "*"
    sql = f"SELECT {select_clause} FROM nyc_taxi.yellow_trips"

    # ----- filters → WHERE clause -----
    where_parts = []
    for raw in _AND_RE.split(filters or ""):
        s = raw.strip()
        if not s:
            continue
        m = _BETWEEN_RE.match(s)
        if m:
            col, lo, hi = m.group(1), _coerce(m.group(2)), _coerce(m.group(3))
            if col not in ALLOWED_COLUMNS:
                raise ValueError(f"unknown column: {col!r}")
            where_parts.append(f"{col} BETWEEN {_sql_lit(lo)} AND {_sql_lit(hi)}")
            continue
        m = _IN_RE.match(s)
        if m:
            col = m.group(1)
            if col not in ALLOWED_COLUMNS:
                raise ValueError(f"unknown column: {col!r}")
            vals = [_coerce(v) for v in m.group(2).split(",") if v.strip()]
            where_parts.append(f"{col} IN ({', '.join(_sql_lit(v) for v in vals)})")
            continue
        for op in sorted(_OP_SQL, key=len, reverse=True):
            if op in s:
                col, _, val = s.partition(op)
                col = col.strip()
                if col not in ALLOWED_COLUMNS:
                    raise ValueError(f"unknown column: {col!r}")
                where_parts.append(f"{col} {_OP_SQL[op]} {_sql_lit(_coerce(val))}")
                break
        else:
            raise ValueError(f"unparseable filter: {raw!r}")

    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)

    if group_keys:
        sql += " GROUP BY " + ", ".join(group_keys)

    # ----- sort_by + ascending -----
    if sort_by and sort_by.strip():
        sb = sort_by.strip()
        valid = set(ALLOWED_COLUMNS) | set(group_keys) | set(agg_aliases)
        if sb not in valid:
            raise ValueError(f"sort_by {sb!r} not in selected columns: {sorted(valid)}")
        sql += f" ORDER BY {sb} {'ASC' if ascending else 'DESC'}"

    limit = max(1, min(int(limit), 1000))
    sql += f" LIMIT {limit}"

    with chdb_connection() as conn:
        df = conn.execute(sql, output_format="Dataframe").to_df()

    rows = df.to_dict(orient="records")
    return json.dumps({"data": rows, "row_count": len(rows)})
