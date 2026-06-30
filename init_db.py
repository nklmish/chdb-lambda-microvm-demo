"""init_db.py — Build-time database initializer. Run during container build."""
import os
import json
import chdb

DB_PATH = os.getenv("CHDB_DATA_PATH", "/app/local_chdb_data")
CDN_BASE = "https://d37ci6vzurychx.cloudfront.net/trip-data"

def _q(sql: str) -> None:
    """Execute SQL against the local chDB data directory."""
    chdb.query(sql, path=DB_PATH)

def create_schema() -> None:
    """Create databases and tables."""
    _q("CREATE DATABASE IF NOT EXISTS nyc_taxi ENGINE = Atomic")
    _q("CREATE DATABASE IF NOT EXISTS agent_state ENGINE = Atomic")
    _q("""
        CREATE TABLE IF NOT EXISTS nyc_taxi.yellow_trips (
            pickup_datetime DateTime, dropoff_datetime DateTime,
            passenger_count UInt8, trip_distance Float64,
            pickup_location_id UInt16, dropoff_location_id UInt16,
            fare_amount Float64, tip_amount Float64, total_amount Float64,
            payment_type UInt8, congestion_surcharge Float64, airport_fee Float64
        ) ENGINE = MergeTree() ORDER BY (pickup_datetime, pickup_location_id)
    """)
    _q("""
        CREATE TABLE IF NOT EXISTS agent_state.conversations (
            role String, content String, created_at DateTime DEFAULT now()
        ) ENGINE = MergeTree() ORDER BY created_at
    """)
    _q("""
        CREATE TABLE IF NOT EXISTS agent_state.analysis_log (
            description String, parameters String, result_summary String,
            execution_ms UInt32, created_at DateTime DEFAULT now()
        ) ENGINE = MergeTree() ORDER BY created_at
    """)

def load_data(mode: str, start_year: int, end_year: int) -> None:
    """Load taxi data from NYC TLC CDN Parquet files."""
    months = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            months.append(f"{year}-{month:02d}")
    
    if mode == "sample":
        months = months[:3]  # First 3 months for fast builds
    
    import time

    loaded = 0
    consecutive_failures = 0
    for m in months:
        url = f"{CDN_BASE}/yellow_tripdata_{m}.parquet"
        # The TLC CloudFront CDN intermittently returns 403 ("too much traffic")
        # under load, and months not yet published return 403/404. Retry each
        # month a couple times to ride out transient throttling, then skip it.
        last_err = None
        for attempt in range(3):
            try:
                _q(f"""
                    INSERT INTO nyc_taxi.yellow_trips
                    SELECT tpep_pickup_datetime  AS pickup_datetime,
                           tpep_dropoff_datetime AS dropoff_datetime,
                           passenger_count,
                           trip_distance,
                           PULocationID          AS pickup_location_id,
                           DOLocationID          AS dropoff_location_id,
                           fare_amount,
                           tip_amount,
                           total_amount,
                           payment_type,
                           congestion_surcharge,
                           Airport_fee           AS airport_fee
                    FROM url('{url}', 'Parquet')
                """)
                loaded += 1
                consecutive_failures = 0
                print(f"Loaded {m}")
                last_err = None
                break
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(10 * (attempt + 1))  # backoff: 10s, 20s
        if last_err is not None:
            print(f"Skipped {m} after retries: {str(last_err)[:120]}")
            if loaded == 0:
                # Nothing loaded yet — likely a real problem, not a missing month.
                raise
            # Already loaded data and now hitting failures at the tail → assume we
            # reached the end of published data and stop (avoids burning retries on
            # every not-yet-published future month).
            consecutive_failures += 1
            if consecutive_failures >= 2:
                print(f"Stopping: reached end of available data after {loaded} months")
                break

def _q_json(sql: str) -> list[dict]:
    """Execute SQL, return parsed JSON data rows."""
    result = chdb.query(sql, "JSON", path=DB_PATH)
    if not result:
        return []
    raw = result.bytes()
    return json.loads(raw)["data"] if raw else []

def generate_profile(cutoff: str, delta_start: str) -> None:
    """Query stats and write data_profile.json. Must produce ALL fields that agent.py system prompt expects."""
    rows = _q_json("SELECT count() as cnt FROM nyc_taxi.yellow_trips")
    row_count = int(rows[0]["cnt"]) if rows else 0

    date_rows = _q_json("""
        SELECT toString(min(pickup_datetime)) as min_date,
               toString(max(pickup_datetime)) as max_date
        FROM nyc_taxi.yellow_trips
    """)
    date_range = {"min": date_rows[0]["min_date"][:10], "max": date_rows[0]["max_date"][:10]} if date_rows else {}

    fare_rows = _q_json("""
        SELECT min(fare_amount) as mn, max(fare_amount) as mx,
               avg(fare_amount) as mean, median(fare_amount) as med
        FROM nyc_taxi.yellow_trips
    """)
    fare_stats = {"min": fare_rows[0]["mn"], "max": fare_rows[0]["mx"],
                  "mean": round(fare_rows[0]["mean"], 2), "median": round(fare_rows[0]["med"], 2)} if fare_rows else {}

    pickup_rows = _q_json("""
        SELECT pickup_location_id as zone_id, count() as trips
        FROM nyc_taxi.yellow_trips
        GROUP BY zone_id ORDER BY trips DESC LIMIT 5
    """)
    dropoff_rows = _q_json("""
        SELECT dropoff_location_id as zone_id, count() as trips
        FROM nyc_taxi.yellow_trips
        GROUP BY zone_id ORDER BY trips DESC LIMIT 5
    """)

    pay_rows = _q_json("""
        SELECT payment_type, count() as cnt FROM nyc_taxi.yellow_trips GROUP BY payment_type
    """)
    total = sum(int(r["cnt"]) for r in pay_rows) or 1
    pay_map = {int(r["payment_type"]): int(r["cnt"]) / total for r in pay_rows}
    payment_dist = {"credit": round(pay_map.get(1, 0), 2), "cash": round(pay_map.get(2, 0), 2),
                    "other": round(1 - pay_map.get(1, 0) - pay_map.get(2, 0), 2)}

    profile = {
        "row_count": row_count,
        "date_range": date_range,
        "fare_stats": fare_stats,
        "top_pickup_zones": [{"zone_id": int(r["zone_id"]), "trips": int(r["trips"])} for r in pickup_rows],
        "top_dropoff_zones": [{"zone_id": int(r["zone_id"]), "trips": int(r["trips"])} for r in dropoff_rows],
        "payment_distribution": payment_dist,
        "baked_cutoff": cutoff,
        "delta_start": delta_start,
    }
    with open("data_profile.json", "w") as f:
        json.dump(profile, f, indent=2)
    print(f"Profile: {row_count} rows, cutoff={cutoff}")

if __name__ == "__main__":
    mode = os.getenv("DATA_MODE", "sample")
    start_year = int(os.getenv("BAKE_START_YEAR", "2024"))
    end_year = int(os.getenv("BAKE_END_YEAR", "2025"))
    cutoff = f"{end_year}-12-31"
    delta_start = f"{end_year + 1}-01"
    
    create_schema()
    load_data(mode, start_year, end_year)
    # Merge the per-month MergeTree parts into one. Loading N months as N separate
    # INSERTs leaves many unmerged parts; background merges rarely finish inside a
    # short build, so the on-disk store can be ~2x its compacted size (e.g. 1.7GB
    # vs ~0.9GB for full-2024). OPTIMIZE FINAL forces the merge now so the baked
    # store — and the image layer that COPYs it — stays small enough for the
    # 2048MB AgentCore Runtime image cap.
    print("Optimizing (merging parts)...")
    _q("OPTIMIZE TABLE nyc_taxi.yellow_trips FINAL")
    generate_profile(cutoff, delta_start)
