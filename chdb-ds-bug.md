# chdb-ds: `DataStore.groupby(ColumnExpr)` emits invalid SQL — `__groupby_temp_*` placeholder unresolved in SELECT

**Repository:** chdb-io/chdb (datastore package)
**File:** `datastore/core.py`, `DataStore.groupby()` method (lines 5161-5258, specifically the `process_field` closure at 5216-5249)
**Severity:** MEDIUM — the API explicitly handles `ColumnExpr`/`LazySeries` arguments (dedicated code path at lines 5222-5247), but the resulting query never executes; users must drop to raw SQL via `Connection.execute()` to get computed-expression groupby working on DB_PATH-backed DataStores.
**Environment:** Python 3.13, macOS arm64, `pip install chdb==4.1.6`

## Description

When `DataStore.groupby()` is called with a `ColumnExpr` argument (e.g. `ds['ts'].dt.month`), the implementation generates a temporary column name via `f"__groupby_temp_{counter}_{uuid.uuid4().hex[:8]}"` (`core.py:5229`) and adds a `LazyColumnAssignment` to a copied DataStore (line 5235). However, when the resulting query is compiled to SQL for chdb backend execution against a DB_PATH-backed DataStore, the temp column assignment is **not propagated into the SELECT clause**. The compiled SQL references the temp identifier in `SELECT`, `GROUP BY`, and `ORDER BY` clauses but never defines it — ClickHouse rejects the query with `Code: 47 UNKNOWN_IDENTIFIER`.

```sql
-- Actual emitted SQL (broken):
SELECT __groupby_temp_0_9c8be8d6, sum(value) AS value_sum
FROM test.events
GROUP BY __groupby_temp_0_9c8be8d6
ORDER BY __groupby_temp_0_9c8be8d6 ASC

-- Expected SQL (the temp alias materialized in SELECT):
SELECT toMonth(ts) AS __groupby_temp_0_9c8be8d6, sum(value) AS value_sum
FROM test.events
GROUP BY __groupby_temp_0_9c8be8d6
ORDER BY __groupby_temp_0_9c8be8d6 ASC
```

## Reproduction

Minimal standalone repro (no application dependencies):

```python
import os, tempfile, chdb
from chdb.datastore import DataStore

# Setup: temp chdb store with a DateTime + Float64 table.
tmpdir = tempfile.mkdtemp(prefix="chdb_bug_")
db_path = os.path.join(tmpdir, "store")

chdb.query("CREATE DATABASE IF NOT EXISTS test ENGINE = Atomic", path=db_path)
chdb.query("""
    CREATE TABLE test.events (ts DateTime, value Float64)
    ENGINE = MergeTree() ORDER BY ts
""", path=db_path)
chdb.query("""
    INSERT INTO test.events VALUES
    ('2024-01-15 08:00:00', 10.0),
    ('2024-02-15 12:00:00', 20.0),
    ('2024-02-20 14:00:00', 25.0),
    ('2024-03-15 16:00:00', 30.0)
""", path=db_path)

# Trigger the bug: groupby on a computed-expression key.
ds = DataStore(table="test.events", database=db_path)
ds.quote_char = ""
result = ds.groupby([ds["ts"].dt.month]).agg(value_sum=("value", "sum")).to_dict(orient="records")
print(result)
```

**Actual error (chdb 4.1.6):**

```
E [chDB] Query failed: Code: 47. DB::Exception: Unknown expression identifier
`__groupby_temp_0_9c8be8d6` in scope SELECT __groupby_temp_0_9c8be8d6,
sum(value) AS value_sum FROM test.events GROUP BY __groupby_temp_0_9c8be8d6
ORDER BY __groupby_temp_0_9c8be8d6 ASC. (UNKNOWN_IDENTIFIER)
```

**Expected:** rows like `[{'__groupby_temp_0_*': 1, 'value_sum': 10.0}, {'__groupby_temp_0_*': 2, 'value_sum': 45.0}, {'__groupby_temp_0_*': 3, 'value_sum': 30.0}]` — or, more usefully, with a stable user-facing alias instead of the internal `__groupby_temp_*` name (see "Suggested fix" below).

## Confirming this is not user error

Three controls executed in the same process show the bug is specific to the computed-expression groupby path on a DB_PATH-backed DataStore:

| Control | Result | Implication |
|---|---|---|
| `ds.groupby("ts").agg(value_sum=("value","sum"))` (bare column) | PASS | Plain groupby works — bug isn't general |
| `ds.assign(constant=1).to_df()` (literal `.assign()`) | PASS | `.assign()` itself isn't broken |
| `ds.assign(month=ds["ts"].dt.month).groupby("month").agg(value_sum=("value","sum"))` (the documented `.assign()`-then-bare-name workaround per `datastore/tests/test_exploratory_batch56_apply_window_fillna.py:741-744`) | PASS | The workaround works — confirming the bug is narrowly the direct-`ColumnExpr` path |

So the failing surface is specifically `groupby(ColumnExpr)` (or `groupby(LazySeries)`), not `groupby` in general, not `.assign()`, not the `.dt` accessor. The `LazyColumnAssignment` mechanism that `.assign()` uses works fine in isolation; the same mechanism invoked from within `groupby`'s `process_field` closure produces SQL that references the alias without defining it.

## Root cause (source-line anchored)

`datastore/core.py:5161-5258` — `DataStore.groupby()` method.

The `process_field` closure at lines 5216-5249 has explicit handling for `ColumnExpr` and `LazySeries` arguments. For both, it:

1. Generates a temp column name (line 5229 / 5240): `f"__groupby_temp_{temp_column_counter}_{uuid.uuid4().hex[:8]}"`.
2. Creates a copy of the DataStore on first temp-column need (lines 5232-5234 / 5243-5245).
3. Adds a `LazyColumnAssignment(temp_name, f._expr)` to the copy (lines 5235 / 5246).
4. Returns `Field(temp_name)` so the GROUP BY clause references the alias.

The bug: step 3's `LazyColumnAssignment` is added to `target_ds._lazy_ops`, but the SQL builder for `LazyGroupByAgg` (`datastore/lazy_ops.py:1067` — `class LazyGroupByAgg(LazyOp)`) does not include the lazy-assignment-derived columns in the rendered SELECT projection. Result: the alias appears in GROUP BY (from `groupby_fields`) and ORDER BY (when `sort=True`, the default per line 5184), but is missing from SELECT — invalid SQL per ClickHouse's identifier-resolution rules.

The `__groupby_temp_*` placeholder visible in the error message is verbatim what `core.py:5229` generates — confirms the code path is taken.

## Impact

Any chdb-ds user attempting computed-expression groupby on a DB_PATH-backed DataStore — for example time-bucket aggregations like "trips per hour" or "events per month" — hits this bug on the first call. The API surface advertises support for `ColumnExpr` arguments (per the dedicated code path), so users follow the natural API call shape, get the failure, and then need to discover the `.assign()`-then-bare-name workaround documented inside the test suite (`datastore/tests/test_exploratory_batch56_apply_window_fillna.py:741-744`) but not in any user-facing docs.

In our project, this bug forced a rewrite of the primary data-analysis tool from the chdb-ds DataStore API to raw SQL via `Connection.execute()` — losing the lazy-evaluation, type-checking, and composition benefits chdb-ds is designed to provide.

## Workaround

Two equivalent paths, both empirically verified on chdb 4.1.6:

**1. Use `.assign()` then `groupby('alias')` (chdb-ds idiomatic):**

```python
ds.assign(month=ds["ts"].dt.month).groupby("month").agg(value_sum=("value", "sum"))
```

Works on DB_PATH-backed DataStores. This pattern is documented inside the test suite at `datastore/tests/test_exploratory_batch56_apply_window_fillna.py:741-744` as "DataStore limitation: groupby does not support direct ColumnExpr/Series parameter."

**2. Drop to raw SQL via `Connection.execute()`:**

```python
from datastore.connection import Connection
conn = Connection(database=db_path)
conn.connect()
df = conn.execute(
    "SELECT toMonth(ts) AS month, sum(value) AS value_sum "
    "FROM test.events GROUP BY month ORDER BY month",
    output_format="Dataframe",
).to_df()
conn.close()
```

## Suggested fix

In `datastore/core.py:5161-5258`'s `groupby` (and the corresponding SQL renderer in `datastore/lazy_ops.py::LazyGroupByAgg`), ensure that `LazyColumnAssignment` lazy-ops added by the `process_field` closure are reflected in the rendered SELECT projection — either by:

(a) emitting `<expr> AS __groupby_temp_*` in the SELECT clause whenever a temp alias appears in `groupby_fields`, **or**
(b) rejecting the call shape with a clear `NotImplementedError` pointing users at the `.assign()` workaround, until (a) is implemented.

A second usability improvement (independent of the bug fix): when a user passes `groupby(ds[col].dt.month)`, the resulting column name is `__groupby_temp_0_<random-hex>` rather than something readable like `month_ts`. Consider deriving the alias from the expression structure (e.g. `f"{accessor}_{col}"`) so the result columns are identifiable in downstream code.

## Related

- chdb-ds test `datastore/tests/test_exploratory_batch56_apply_window_fillna.py:738-754` — `test_dt_year_then_groupby_direct`, marked with `@limit_groupby_series_param` (xfail-style) decorator. The test docstring describes the limitation but the implementation at `core.py:5222-5247` attempts to support the call shape — there's an internal inconsistency between "test says unsupported" and "code says try-to-support".
- Prior PR to this repo: chdb-io/chdb#563 — `UrlTableFunction.to_sql()` HEADERS placement bug (different code path; same author).

## Verification

```
chdb version:              4.1.6
chdb-ds version:           4.1.6 (bundled)
Python:                    3.13
Platform:                  macOS arm64
Date verified:             2026-04-28

Repro emits exact error:
  Code: 47. DB::Exception: Unknown expression identifier
  `__groupby_temp_0_9c8be8d6` in scope SELECT __groupby_temp_0_9c8be8d6,
  sum(value) AS value_sum FROM test.events GROUP BY __groupby_temp_0_9c8be8d6
  ORDER BY __groupby_temp_0_9c8be8d6 ASC. (UNKNOWN_IDENTIFIER)

Three controls in the same process all PASS, scoping the bug to the
groupby(ColumnExpr) path specifically.
```
