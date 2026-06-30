"""tests/conftest.py — Shared fixtures for all test files.

Note on the mock_agent fixture:

  - mock_agent fixture no longer asserts Strands' pre-D.25 message shape
    ({"content": [{"text": ...}]}). The real stream_async event shape
    uses top-level "data" keys (see agent.py::stream_chat_with_agent).
    Individual tests configure return values as needed on the MagicMock.
"""
import os
import json
import pytest
import chdb
from unittest.mock import patch, MagicMock


# ─── chDB Fixtures ───

@pytest.fixture(scope="session")
def sample_db(tmp_path_factory):
    """Create a real chDB database with sample data. Shared across all tests in session."""
    db_path = str(tmp_path_factory.mktemp("chdb"))

    chdb.query("CREATE DATABASE IF NOT EXISTS nyc_taxi ENGINE = Atomic", path=db_path)
    chdb.query("CREATE DATABASE IF NOT EXISTS agent_state ENGINE = Atomic", path=db_path)
    chdb.query("""
        CREATE TABLE nyc_taxi.yellow_trips (
            pickup_datetime DateTime, dropoff_datetime DateTime,
            passenger_count UInt8, trip_distance Float64,
            pickup_location_id UInt16, dropoff_location_id UInt16,
            fare_amount Float64, tip_amount Float64, total_amount Float64,
            payment_type UInt8, congestion_surcharge Float64, airport_fee Float64
        ) ENGINE = MergeTree() ORDER BY (pickup_datetime, pickup_location_id)
    """, path=db_path)
    chdb.query("""
        CREATE TABLE agent_state.conversations (
            role String, content String, created_at DateTime DEFAULT now()
        ) ENGINE = MergeTree() ORDER BY created_at
    """, path=db_path)
    chdb.query("""
        CREATE TABLE agent_state.analysis_log (
            description String, parameters String, result_summary String,
            execution_ms UInt32, created_at DateTime DEFAULT now()
        ) ENGINE = MergeTree() ORDER BY created_at
    """, path=db_path)

    chdb.query("""
        INSERT INTO nyc_taxi.yellow_trips VALUES
        ('2024-06-15 08:30:00', '2024-06-15 09:00:00', 1, 3.5, 161, 237, 18.50, 3.70, 25.30, 1, 2.50, 0.00),
        ('2024-06-15 12:00:00', '2024-06-15 12:30:00', 2, 5.2, 162, 230, 22.00, 4.40, 29.90, 1, 2.50, 0.00),
        ('2024-06-15 18:00:00', '2024-06-15 18:45:00', 1, 8.1, 132, 138, 35.00, 0.00, 37.50, 2, 2.50, 0.00),
        ('2024-07-01 07:00:00', '2024-07-01 07:20:00', 1, 2.0, 161, 161, 12.00, 2.40, 17.40, 1, 2.50, 0.00),
        ('2024-07-01 09:00:00', '2024-07-01 09:45:00', 3, 15.0, 132, 1, 52.00, 0.00, 57.80, 2, 2.50, 1.75),
        ('2024-07-15 14:00:00', '2024-07-15 14:15:00', 1, 1.5, 237, 236, 8.50, 2.00, 13.00, 1, 2.50, 0.00),
        ('2024-08-01 10:00:00', '2024-08-01 10:30:00', 2, 4.0, 161, 162, 20.00, 4.00, 27.00, 1, 2.50, 0.00),
        ('2024-08-15 16:00:00', '2024-08-15 16:20:00', 1, 3.0, 138, 132, 15.00, 0.00, 17.50, 2, 2.50, 0.00),
        ('2024-09-01 11:00:00', '2024-09-01 11:30:00', 1, 6.0, 1, 132, 28.00, 5.60, 38.35, 1, 2.50, 1.75),
        ('2024-09-15 20:00:00', '2024-09-15 20:30:00', 4, 4.5, 162, 161, 19.00, 3.80, 25.30, 1, 2.50, 0.00)
    """, path=db_path)

    return db_path


@pytest.fixture
def db_env(sample_db, monkeypatch):
    """Set CHDB_DATA_PATH to the sample database for tests that import db.py."""
    monkeypatch.setenv("CHDB_DATA_PATH", sample_db)


@pytest.fixture
def data_profile(tmp_path):
    """Create a sample data_profile.json for agent tests."""
    profile = {
        "row_count": 10,
        "date_range": {"min": "2024-06-15", "max": "2024-09-15"},
        "fare_stats": {"min": 8.5, "max": 52.0, "mean": 23.0, "median": 19.75},
        "top_pickup_zones": [{"zone_id": 161, "name": "Midtown Center", "trips": 3}],
        "top_dropoff_zones": [],
        "payment_distribution": {"credit": 0.7, "cash": 0.3, "other": 0.0},
        "baked_cutoff": "2024-12-31",
        "delta_start": "2025-01",
    }
    path = tmp_path / "data_profile.json"
    path.write_text(json.dumps(profile))
    return str(path)


# ─── Mock Fixtures ───

@pytest.fixture
def mock_agent():
    """Mock Strands Agent for tests that don't need real LLM calls.

    Note: does NOT preconfigure a .message shape — individual tests set
    return values as needed because Strands' stream_async event shape
    changed in D.25 (see agent.py::stream_chat_with_agent).
    """
    with patch("agent.Agent") as mock:
        instance = MagicMock()
        mock.return_value = instance
        yield instance


@pytest.fixture
def mock_bedrock_model():
    """Mock BedrockModel for tests that don't need real AWS calls."""
    with patch("agent.BedrockModel") as mock:
        yield mock


@pytest.fixture
def mock_session_manager():
    """Mock AgentCoreMemorySessionManager for AgentCore Memory tests."""
    return MagicMock()


# ─── FastAPI Test Client ───

@pytest.fixture
def app_client(db_env, data_profile, monkeypatch):
    """httpx.AsyncClient for FastAPI endpoint tests."""
    import httpx
    from main import app
    monkeypatch.setenv("DATA_PROFILE_PATH", data_profile)
    return httpx.AsyncClient(app=app, base_url="http://test")
