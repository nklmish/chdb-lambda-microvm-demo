import os
import json
import chdb
from chdb.datastore import DataStore

DB_PATH: str = os.getenv("CHDB_DATA_PATH", "/app/local_chdb_data")

def get_taxi_ds() -> DataStore:
    """Fresh DataStore for yellow_trips. Uses fully-qualified table name + quote_char=''."""
    ds = DataStore(table="nyc_taxi.yellow_trips", database=DB_PATH)
    ds.quote_char = ""
    return ds

def get_conversations_ds() -> DataStore:
    ds = DataStore(table="agent_state.conversations", database=DB_PATH)
    ds.quote_char = ""
    return ds

def get_analysis_log_ds() -> DataStore:
    ds = DataStore(table="agent_state.analysis_log", database=DB_PATH)
    ds.quote_char = ""
    return ds

def query_records(sql: str) -> list[dict]:
    """Execute SQL, return list of dicts. Uses path= for stateless access."""
    result = chdb.query(sql, "JSON", path=DB_PATH)
    return json.loads(result.bytes())["data"] if result else []

def execute(sql: str) -> None:
    """Execute DDL/DML, no return."""
    chdb.query(sql, path=DB_PATH)
