"""tests/test_scan_tools.py — the /scan worker's dataset registry + input vetting.

Security-relevant: the coordinator supplies only an allow-listed dataset key, a
release, and parquet *basenames*; scan_tools must reject anything else and build
every S3 URL from the baked registry, with credentials redacted from any SQL.
"""
import pytest

import scan_tools as st


def test_unknown_dataset_rejected():
    with pytest.raises(ValueError):
        st._dataset("definitely-not-a-dataset")
    assert set(st.DATASETS) == {"taxi", "buildings", "segments"}


def test_validate_rejects_bad_file_basenames():
    ds = st.DATASETS["buildings"]
    for bad in [["../etc/passwd"], ["a/b.parquet"], ["x.txt"], ["s3://evil/x.parquet"]]:
        with pytest.raises(ValueError):
            st._validate(ds, "2026-06-17.0", bad)


def test_validate_rejects_empty_and_oversized():
    ds = st.DATASETS["segments"]
    with pytest.raises(ValueError):
        st._validate(ds, "2026-06-17.0", [])
    with pytest.raises(ValueError):
        st._validate(ds, "2026-06-17.0", [f"part-{i}.parquet" for i in range(st.MAX_FILES + 1)])


def test_overture_requires_valid_release():
    ds = st.DATASETS["buildings"]
    with pytest.raises(ValueError):
        st._validate(ds, None, ["part-0.parquet"])
    with pytest.raises(ValueError):
        st._validate(ds, "not-a-release", ["part-0.parquet"])
    prefix, _ = st._validate(ds, "2026-06-17.0", ["part-0.parquet"])
    assert "release/2026-06-17.0/theme=buildings" in prefix


def test_taxi_needs_no_release():
    ds = st.DATASETS["taxi"]
    prefix, _ = st._validate(ds, None, ["yellow_tripdata_2015-01.parquet"])
    assert prefix == st.LAKE_PREFIX


def test_source_nosign_is_anonymous_and_has_no_secret():
    ds = st.DATASETS["buildings"]
    live, red = st._source(ds, "overturemaps-us-west-2", "release/x/theme=buildings",
                           ["part-0.parquet", "part-1.parquet"])
    assert "NOSIGN" in live and live == red
    assert "{part-0.parquet,part-1.parquet}" in live      # brace-list for multi-file


def test_source_role_carries_then_redacts_creds(monkeypatch):
    ds = st.DATASETS["taxi"]
    monkeypatch.setattr(st, "_frozen_creds", lambda: ("AKIA", "secretkey", "tok123"))
    live, red = st._source(ds, "my-lake", "lake/yellow", ["yellow_tripdata_2015-01.parquet"])
    assert "secretkey" in live and "tok123" in live       # live SQL carries creds
    assert "secretkey" not in red and "tok123" not in red
    assert "'***'" in red
