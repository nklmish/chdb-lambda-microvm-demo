import os
import json
from contextlib import contextmanager
from typing import Iterator

import chdb
from chdb.datastore import DataStore
from datastore.connection import Connection

DB_PATH: str = os.getenv("CHDB_DATA_PATH", "/app/local_chdb_data")


@contextmanager
def chdb_connection(database: str | None = None) -> Iterator[Connection]:
    """Yield a connected chDB Connection bound to the store, closed on exit.

    Centralizes the ``Connection(database=...); conn.connect(); try/finally
    conn.close()`` dance that every tool module used to repeat by hand — so the
    close protocol lives in exactly one place and a raising query can never leak
    a connection. ``database`` defaults to the current ``DB_PATH`` and is read at
    call time so tests that monkeypatch ``db.DB_PATH`` are honored.
    """
    conn = Connection(database=database if database is not None else DB_PATH)
    conn.connect()
    try:
        yield conn
    finally:
        conn.close()


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
