"""Parameter validation and DataStore expression builder for LLM tools.

This is the critical security boundary — no raw SQL from the LLM. All tool
parameters flow through here and are validated against ALLOWED_COLUMNS,
ALLOWED_AGGREGATIONS, and ALLOWED_TIME_GROUPS before touching DataStore.

Design:
  - Dispatch tables, not if-else ladders.
  - Filter strings use "AND" as top-level separator (case-insensitive).
  - Groupby tokens separated by ",". Tokens may be plain columns or
    time-group expressions like "month(pickup_datetime)".
  - Aggregations: "col1:func1, col2:func2" → named aggregation tuples.
  - Sort allowlist computed explicitly from ALLOWED_COLUMNS plus any
    aggregation aliases produced by parse_aggregations.
"""
from __future__ import annotations
import re
from chdb.datastore import DataStore

import db


# -- Allowlists ---------------------------------------------------------------

# 12-column schema from; also QUERY_COLUMNS in sql_tools.py.
ALLOWED_COLUMNS: set[str] = {
    "pickup_datetime", "dropoff_datetime",
    "passenger_count", "trip_distance",
    "pickup_location_id", "dropoff_location_id",
    "fare_amount", "tip_amount", "total_amount",
    "payment_type", "congestion_surcharge", "airport_fee",
}

# chDB 4.1.6 DataStore comparison operators (VALIDATED against source).
OPERATORS = {
    ">=": lambda col, val: col >= val,
    "<=": lambda col, val: col <= val,
    ">":  lambda col, val: col > val,
    "<":  lambda col, val: col < val,
    "==": lambda col, val: col == val,
    "!=": lambda col, val: col != val,
}

# chDB 4.1.6 .dt accessor (VALIDATED in datastore/datetime.py lines 35-85).
# Aliased so both the spec's export name and the internal name refer to the
# same object.
TIME_GROUPS = ALLOWED_TIME_GROUPS = {
    "month":     lambda col: col.dt.month,
    "year":      lambda col: col.dt.year,
    "hour":      lambda col: col.dt.hour,
    "dayofweek": lambda col: col.dt.dayofweek,
    "quarter":   lambda col: col.dt.quarter,
}

# chDB 4.1.6 named aggregations (VALIDATED in datastore/groupby.py 220-228).
# Aliased: AGG_MAP internal name, ALLOWED_AGGREGATIONS is the spec's export.
AGG_MAP = ALLOWED_AGGREGATIONS = {
    "mean": "mean", "sum": "sum", "count": "count",
    "min": "min", "max": "max", "median": "median", "std": "std",
}


# -- Filter parsing -----------------------------------------------------------

_BETWEEN_RE = re.compile(r"^\s*(\w+)\s+between\s+(\S+)\s+and\s+(\S+)\s*$", re.I)
_IN_RE      = re.compile(r"^\s*(\w+)\s+in\s+\[([^\]]*)\]\s*$", re.I)
_AND_RE     = re.compile(r"\s+AND\s+", re.I)


def _coerce(v: str):
    v = v.strip().strip("'\"")
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _require_column(col: str) -> None:
    if col not in ALLOWED_COLUMNS:
        raise ValueError(f"unknown column: {col!r}")


def _apply_single_filter(ds: DataStore, filter_str: str) -> DataStore:
    """Parse one filter expression and return a filtered DataStore."""
    s = filter_str.strip()

    m = _BETWEEN_RE.match(s)
    if m:
        col, lo, hi = m.group(1), _coerce(m.group(2)), _coerce(m.group(3))
        _require_column(col)
        return ds.filter((ds[col] >= lo) & (ds[col] <= hi))

    m = _IN_RE.match(s)
    if m:
        col = m.group(1)
        _require_column(col)
        values = [_coerce(p) for p in m.group(2).split(",") if p.strip()]
        return ds.filter(ds[col].isin(values))

    # Comparison operators, longest match first so ">=" beats ">".
    for op in sorted(OPERATORS, key=len, reverse=True):
        if op in s:
            col, _, val = s.partition(op)
            col = col.strip()
            _require_column(col)
            return ds.filter(OPERATORS[op](ds[col], _coerce(val)))

    raise ValueError(f"unparseable filter: {filter_str!r}")


def parse_filters(ds: DataStore, filters_str: str) -> DataStore:
    """Apply an AND-joined filter string. Empty string returns ds unchanged."""
    if not filters_str.strip():
        return ds
    for part in _AND_RE.split(filters_str):
        if part.strip():
            ds = _apply_single_filter(ds, part)
    return ds


# -- Groupby parsing ----------------------------------------------------------

_TIME_GROUP_RE = re.compile(r"^\s*(\w+)\s*\(\s*(\w+)\s*\)\s*$")


def parse_groupby(ds: DataStore, groupby_str: str):
    """Return LazyGroupBy if groupby_str is non-empty, else ds unchanged.

    Tokens are comma-separated. Each token is either:
      - a bare column name (must be in ALLOWED_COLUMNS), or
      - a time-group expression like "month(pickup_datetime)".

    Time-group expressions are materialized via .assign() into a temp
    column and then grouped by that column name. This is the documented
    chDB-DS pattern (see datastore/tests/test_exploratory_batch56,
    line 740: "DataStore limitation: groupby does not support direct
    ColumnExpr/Series parameter. Must use column name instead.").
    Passing a computed expression directly to ds.groupby([expr]) emits
    invalid SQL (`Code: 47 UNKNOWN_IDENTIFIER` on a `__groupby_temp_*`
    placeholder).
    """
    if not groupby_str.strip():
        return ds
    keys: list[str] = []
    for token in groupby_str.split(","):
        token = token.strip()
        if not token:
            continue
        m = _TIME_GROUP_RE.match(token)
        if m:
            func, col = m.group(1), m.group(2)
            if func not in ALLOWED_TIME_GROUPS:
                raise ValueError(f"unknown time group: {func!r}")
            _require_column(col)
            alias = f"{func}_{col}"
            ds = ds.assign(**{alias: ALLOWED_TIME_GROUPS[func](ds[col])})
            keys.append(alias)
        else:
            _require_column(token)
            keys.append(token)
    return ds.groupby(keys)


# -- Aggregation parsing ------------------------------------------------------

def parse_aggregations(grouped, agg_str: str) -> DataStore:
    """Parse "col1:func1, col2:func2" → grouped.agg(col1_func1=("col1","func1"), ...)."""
    specs = {}
    for pair in agg_str.split(","):
        if not pair.strip():
            continue
        col, func = pair.strip().split(":")
        col, func = col.strip(), func.strip()
        if col not in ALLOWED_COLUMNS:
            raise ValueError(f"unknown column: {col!r}")
        if func not in AGG_MAP:
            raise ValueError(f"unknown aggregation: {func!r}")
        alias = f"{col}_{func}"
        specs[alias] = (col, AGG_MAP[func])
    return grouped.agg(**specs)


# -- Pipeline -----------------------------------------------------------------

def execute_query_pipeline(
    filters: str,
    group_by: str,
    aggregations: str,
    sort_by: str,
    ascending: bool,
    limit: int,
) -> list[dict]:
    """Compose filter → groupby/agg → sort → limit and materialize as list[dict]."""
    # Fail loud per: aggregations without groupby is meaningless.
    if aggregations.strip() and not group_by.strip():
        raise ValueError("aggregations require a non-empty group_by")

    ds = db.get_taxi_ds()
    ds = parse_filters(ds, filters)

    if group_by.strip():
        grouped = parse_groupby(ds, group_by)
        if aggregations.strip():
            ds = parse_aggregations(grouped, aggregations)
        else:
            ds = grouped  # groupby without agg is a no-op for select purposes
        # Surface group keys as columns. chDB-DS keeps groupby keys in the
        # DataFrame index by default (matches pandas); to_dict(orient="records")
        # only emits columns, so without this the group key disappears from
        # the agent's view of the result. See chdb-ds tests/test_exploratory_
        # batch24_datetime_accessor.py:656-661 for the canonical pattern.
        ds = ds.reset_index()

    # Compute the explicit allow-set for sort: base columns + aggregation
    # aliases + time-bucket group aliases.
    allowed_sort = set(ALLOWED_COLUMNS)
    if aggregations.strip():
        for pair in aggregations.split(","):
            if not pair.strip():
                continue
            col, func = pair.strip().split(":")
            allowed_sort.add(f"{col.strip()}_{func.strip()}")
    if group_by.strip():
        for token in group_by.split(","):
            token = token.strip()
            m = _TIME_GROUP_RE.match(token)
            if m:
                func, col = m.group(1), m.group(2)
                allowed_sort.add(f"{func}_{col}")

    if sort_by.strip():
        if sort_by not in allowed_sort:
            raise ValueError(f"unknown sort column: {sort_by!r}")
        ds = ds.sort_values(sort_by, ascending=ascending)

    limit = max(1, min(int(limit) if limit else 50, 1000))
    ds = ds.head(limit)
    return ds.to_dict(orient="records")
