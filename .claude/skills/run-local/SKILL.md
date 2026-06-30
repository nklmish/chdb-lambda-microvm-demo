---
name: run-local
description: Launch and drive the NYC Taxi Analytics Agent locally for a fast smoke test — bake a tiny chDB sample, start the FastAPI/uvicorn server, and hit /chat. Use when asked to run, start, or verify this app on a dev machine without the full Finch/AWS container flow.
---

# Run the NYC Taxi Agent locally (fast smoke test)

This is the **fast local smoke-test path** — a single-month chDB bake so the app
boots in seconds and you can drive a real `/chat` query against Bedrock. It is
NOT the canonical data bake (see `init_db.py` / README "Local development" for the
full ~9.5M-row Finch flow). Use this to confirm the app works end-to-end; use the
README flow for representative data.

## Prerequisites (verify first)

```bash
.venv/bin/python -c "import fastapi, uvicorn, chdb, strands; print('deps OK', chdb.__version__)"
aws sts get-caller-identity        # must return a valid identity — agent calls Bedrock
```

- The project targets **chDB ≥ 4.1.8** (fixes the CloudFront 403 that broke the live
  CDN reads used by `query_with_fresh_data`, and by the in-container bake). If your
  `.venv` shows an older chDB, `.venv/bin/pip install -U 'chdb>=4.1.8'`.
- If `.venv` is missing: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt`
- AWS creds must have Bedrock access to `us.anthropic.claude-sonnet-4-20250514-v1:0`
  in us-east-1/us-east-2/us-west-2 (cross-region inference profile). Without this,
  `/chat` returns a Bedrock AccessDenied error (the server still boots and `/health` works).

## Step 1 — Bake a one-month chDB sample (~2s, ~3M rows)

The app reads chDB at `$CHDB_DATA_PATH` and needs `data_profile.json` at the repo root.

> ⚠️ chDB embedded bug: do NOT issue multiple `chdb.query(..., path=...)` calls in
> one process — the second `connect()` fails with `Error initializing EmbeddedServer`
> / `recursive_mutex lock failed` (see `chdb-ds-bug.md`). Bake with one INSERT, and
> generate the profile through a single persistent `chdb.session.Session`.

```bash
CHDB_DATA_PATH="$PWD/local_chdb_data" .venv/bin/python - <<'PY'
import os, chdb, json
DB = os.environ["CHDB_DATA_PATH"]
def q(sql): chdb.query(sql, path=DB)
q("CREATE DATABASE IF NOT EXISTS nyc_taxi ENGINE = Atomic")
q("CREATE DATABASE IF NOT EXISTS agent_state ENGINE = Atomic")
q("""CREATE TABLE IF NOT EXISTS nyc_taxi.yellow_trips (
 pickup_datetime DateTime, dropoff_datetime DateTime, passenger_count UInt8,
 trip_distance Float64, pickup_location_id UInt16, dropoff_location_id UInt16,
 fare_amount Float64, tip_amount Float64, total_amount Float64, payment_type UInt8,
 congestion_surcharge Float64, airport_fee Float64
) ENGINE = MergeTree() ORDER BY (pickup_datetime, pickup_location_id)""")
q("""CREATE TABLE IF NOT EXISTS agent_state.conversations (role String, content String,
 created_at DateTime DEFAULT now()) ENGINE = MergeTree() ORDER BY created_at""")
q("""CREATE TABLE IF NOT EXISTS agent_state.analysis_log (description String, parameters String,
 result_summary String, execution_ms UInt32, created_at DateTime DEFAULT now())
 ENGINE = MergeTree() ORDER BY created_at""")
url = "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2024-01.parquet"
q(f"""INSERT INTO nyc_taxi.yellow_trips SELECT tpep_pickup_datetime, tpep_dropoff_datetime,
 passenger_count, trip_distance, PULocationID, DOLocationID, fare_amount, tip_amount,
 total_amount, payment_type, congestion_surcharge, Airport_fee FROM url('{url}','Parquet')""")
print("bake done")
PY
```

Then generate `data_profile.json` via a single Session (one connection, no re-init):

```bash
CHDB_DATA_PATH="$PWD/local_chdb_data" .venv/bin/python - <<'PY'
import os, json
from chdb import session as chs
sess = chs.Session(os.environ["CHDB_DATA_PATH"])
def j(sql): 
    s = str(sess.query(sql, "JSON")); return json.loads(s)["data"] if s.strip() else []
rc = int(j("SELECT count() c FROM nyc_taxi.yellow_trips")[0]["c"])
dr = j("SELECT toString(min(pickup_datetime)) mn,toString(max(pickup_datetime)) mx FROM nyc_taxi.yellow_trips")[0]
fr = j("SELECT min(fare_amount) mn,max(fare_amount) mx,avg(fare_amount) mean,median(fare_amount) med FROM nyc_taxi.yellow_trips")[0]
pu = j("SELECT pickup_location_id zone_id,count() trips FROM nyc_taxi.yellow_trips GROUP BY zone_id ORDER BY trips DESC LIMIT 5")
do = j("SELECT dropoff_location_id zone_id,count() trips FROM nyc_taxi.yellow_trips GROUP BY zone_id ORDER BY trips DESC LIMIT 5")
pay = j("SELECT payment_type,count() cnt FROM nyc_taxi.yellow_trips GROUP BY payment_type")
tot = sum(int(r["cnt"]) for r in pay) or 1
pm = {int(r["payment_type"]): int(r["cnt"])/tot for r in pay}
json.dump({
 "row_count": rc,
 "date_range": {"min": dr["mn"][:10], "max": dr["mx"][:10]},
 "fare_stats": {"min": fr["mn"], "max": fr["mx"], "mean": round(fr["mean"],2), "median": round(fr["med"],2)},
 "top_pickup_zones": [{"zone_id": int(r["zone_id"]), "trips": int(r["trips"])} for r in pu],
 "top_dropoff_zones": [{"zone_id": int(r["zone_id"]), "trips": int(r["trips"])} for r in do],
 "payment_distribution": {"credit": round(pm.get(1,0),2), "cash": round(pm.get(2,0),2), "other": round(1-pm.get(1,0)-pm.get(2,0),2)},
 "baked_cutoff": "2024-12-31", "delta_start": "2025-01",
}, open("data_profile.json","w"), indent=2)
sess.close()
print("profile written")
PY
```

The bake and the profile generation are two separate processes on purpose — chDB
embedded allows only one live connection per process.

## Step 2 — Launch the server

```bash
CHDB_DATA_PATH="$PWD/local_chdb_data" \
AWS_REGION=us-east-1 \
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0 \
IS_PROD=false \
DISABLE_ADOT_OBSERVABILITY=true \
LANGFUSE_TRACING_ENVIRONMENT=DEV \
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8080 --workers 1
```

(Run it in the background and tee logs to a file when driving it from an agent.)

Notes:
- Langfuse/OTEL tracing degrades gracefully when no creds are set — `trace_id` is
  null and the "View in Langfuse" link won't resolve. Non-fatal.
- A `WARNING ... opentelemetry-instrumentation-botocore not importable` line at
  startup is expected with this venv and does not affect the agent.
- Only ONE process may hold `local_chdb_data` at a time. Stop the server before
  re-running the bake/profile scripts, or you'll hit the EmbeddedServer init error.

## Step 3 — Drive it (don't just check it booted)

```bash
curl -s http://127.0.0.1:8080/ping                              # {"status":"Healthy"}  (zero-I/O)
curl -s http://127.0.0.1:8080/health                            # {"status":"healthy","row_count":...}  (queries chDB)
curl -s http://127.0.0.1:8080/info

# Sync chat — NOTE the field is "text", not "prompt"
curl -s -X POST http://127.0.0.1:8080/chat -H 'Content-Type: application/json' \
  -d '{"text":"What is the single busiest hour of the day by trip count? One sentence."}'

# SSE stream — emits {"type":"text","content":...} tokens then a {"type":"done",...} with metrics
curl -s -N -X POST http://127.0.0.1:8080/chat/stream -H 'Content-Type: application/json' \
  -d '{"text":"Average fare in one sentence."}'
```

The UI is served at `http://127.0.0.1:8080/` (single-file HTML, SSE streaming).

## Success criteria

- `/health` returns a non-zero `row_count`.
- `/chat` returns a `response` string grounded in the data (e.g. busiest hour ~19:00,
  avg fare ~$18 for the 2024-01 sample).
- `/chat/stream` streams text tokens followed by a `done` event with token counts.

## Caveats vs. production

- One month of 2024 data only — figures are NOT representative of the full bake.
- Raw TLC data contains outliers (negative fares, stray years like 2002 in min-date).
- Weather (`analyze_weather_impact`) hits the public NOAA S3 bucket directly; needs
  S3 egress and is not exercised by the smoke test above.
