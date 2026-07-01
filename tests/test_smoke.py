"""Smoke tests for tests/conftest.py — verify fixtures wire up before trusting
them in real test files."""
import os


def test_sample_db_path_exists(sample_db):
    """sample_db fixture creates a directory."""
    assert os.path.isdir(sample_db)


def test_sample_db_has_expected_row_count(sample_db):
    """The 10 sample rows we insert in conftest are queryable."""
    import chdb
    result = chdb.query(
        "SELECT count(*) FROM nyc_taxi.yellow_trips", "JSON", path=sample_db
    )
    import json
    rows = json.loads(result.bytes())["data"]
    assert rows[0]["count()"] == 10


def test_db_env_sets_chdb_data_path(db_env):
    """db_env fixture exports CHDB_DATA_PATH for tests that import db.py."""
    assert "CHDB_DATA_PATH" in os.environ
    assert os.path.isdir(os.environ["CHDB_DATA_PATH"])


def test_data_profile_fixture_writes_json(data_profile):
    """data_profile fixture writes a parseable JSON profile."""
    import json
    assert os.path.isfile(data_profile)
    with open(data_profile) as f:
        profile = json.load(f)
    assert profile["row_count"] == 10
    assert profile["baked_cutoff"] == "2024-12-31"


def test_chdb_connection_yields_connected_and_closes(sample_db):
    """The shared context manager connects, runs a query, and closes cleanly."""
    from db import chdb_connection
    with chdb_connection(sample_db) as conn:
        df = conn.execute(
            "SELECT count() AS c FROM nyc_taxi.yellow_trips",
            output_format="Dataframe",
        ).to_df()
    assert int(df["c"][0]) == 10
