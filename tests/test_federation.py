"""tests/test_federation.py — federation tool + cloud-source allow-list.

Two layers:
  - Pure offline unit tests for the allow-list, credential resolution, and the
    normalized SQL builder (no network, no chDB).
  - One end-to-end run of analyze_fleet_across_clouds over the *local* source
    only, against the sample_db fixture — exercises build → union → execute →
    materialize → cache-hit with zero network. The full multi-cloud path is a
    separate, network-marked test that is skipped by default.
"""
import json

import pytest

import cloud_sources as cs


@pytest.fixture(autouse=True)
def _disable_ssm_fallback(monkeypatch):
    """Unit tests must never reach real SSM. Disable the /clickhouse/* fallback
    by default (and clear the resolved-cred cache); tests that exercise the SSM
    path re-enable it explicitly."""
    monkeypatch.setattr(cs, "_chc_ssm_cache", None)
    monkeypatch.setattr(cs, "_chc_from_ssm", lambda: None)


# ─── allow-list ───

def test_default_sources_are_all_keys():
    keys = [s.key for s in cs.resolve_sources(None)]
    assert keys == list(cs.DEFAULT_SOURCE_KEYS)
    assert set(keys) == set(cs.allowed_source_keys())


def test_resolve_sources_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown federation source"):
        cs.resolve_sources(["gcs", "definitely-not-a-cloud"])


def test_resolve_sources_is_case_insensitive_subset():
    srcs = cs.resolve_sources(["GCS", " Azure "])
    assert [s.key for s in srcs] == ["gcs", "azure"]


def test_llm_cannot_inject_a_url_as_a_source():
    # The model selects by key; a raw URL is not a valid source.
    with pytest.raises(ValueError):
        cs.resolve_sources(["https://evil.example/data.parquet"])


# ─── credential resolution ───

def test_chc_credentials_absent_returns_none(monkeypatch):
    monkeypatch.delenv("CLICKHOUSE_URL", raising=False)
    monkeypatch.delenv("CLICKHOUSE_PASSWORD", raising=False)
    assert cs.chc_credentials() is None


def test_chc_credentials_strips_scheme_and_uses_native_port(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_URL", "https://abc.eu-central-1.aws.clickhouse.cloud/")
    monkeypatch.setenv("CLICKHOUSE_USER", "default")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "secret")
    cfg = cs.chc_credentials()
    assert cfg == {
        "host": "abc.eu-central-1.aws.clickhouse.cloud",
        "port": "9440",
        "user": "default",
        "password": "secret",
    }


def test_chc_credentials_missing_password_is_none(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_URL", "https://abc.clickhouse.cloud")
    monkeypatch.delenv("CLICKHOUSE_PASSWORD", raising=False)
    assert cs.chc_credentials() is None


# ─── normalized SQL builder ───

_NORMALIZED_COLS = ("cloud", "era", "trips", "avg_tip_pct", "avg_fare")


def test_local_fragment_has_normalized_shape():
    sql = cs._build_local()
    for col in _NORMALIZED_COLS:
        assert col in sql
    assert "nyc_taxi.yellow_trips" in sql
    assert "sum(tip_amount) / nullIf(sum(fare_amount), 0)" in sql  # revenue-weighted rate
    assert "fare_amount >= 2.5" in sql  # junk-fare floor


def test_gcs_fragment_casts_string_columns():
    sql = cs._build_gcs()
    assert "toFloat64OrZero(tip_amount)" in sql
    assert "TabSeparatedWithNames" in sql
    assert "'GCS (ClickHouse public)'" in sql


def test_chc_fragment_requires_credentials(monkeypatch):
    monkeypatch.delenv("CLICKHOUSE_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="credentials not configured"):
        cs._build_chc()


def test_chc_fragment_uses_remote_secure(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_URL", "https://abc.clickhouse.cloud")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "pw")
    sql = cs._build_chc()
    assert "remoteSecure('abc.clickhouse.cloud:9440'" in sql
    assert "workshop.nyc_taxi_trips" in sql


# ─── federation assembly ───

def test_assemble_union_wraps_for_global_order():
    import federation_tools as ft
    srcs = cs.resolve_sources(["local"])
    fragments = [(s, s.build()) for s in srcs]
    sql = ft._assemble_union(fragments)
    assert sql.startswith("SELECT * FROM (")
    assert sql.rstrip().endswith("ORDER BY era")


def test_split_keys_parses_comma_and_space():
    import federation_tools as ft
    assert ft._split_keys("gcs, azure  s3") == ["gcs", "azure", "s3"]
    assert ft._split_keys("") is None
    assert ft._split_keys("   ") is None


# ─── end-to-end (offline: local source only, against sample_db) ───

def test_local_only_federation_end_to_end(db_env, sample_db, monkeypatch):
    """Full pipeline over the local source — no network, no CH Cloud.

    Exercises the real chDB MergeTree cache: call 1 federates + materializes into
    nyc_taxi.fleet_cache (stateless db.* I/O), call 2 reads it back.
    """
    import db
    import federation_tools as ft

    # Point both the federation cache I/O (db.DB_PATH) and the zone tool's
    # Connection (ft.DB_PATH) at the sample store (bound at import time).
    monkeypatch.setattr(db, "DB_PATH", sample_db)
    monkeypatch.setattr(ft, "DB_PATH", sample_db)

    # Call 1 — cross-"cloud" reach (here just the local plane).
    out1 = json.loads(ft.analyze_fleet_across_clouds(sources="local", refresh=True))
    assert out1["mode"] == "single-statement"
    assert out1["sources_used"] == ["local (chDB)"]
    assert out1["row_count"] == 1
    row = out1["data"][0]
    assert row["cloud"] == "local (chDB)"
    assert row["era"] == "2024"
    assert row["trips"] == 10  # sample_db has 10 rows, all 2024
    assert row["avg_tip_pct"] > 0
    assert "SELECT * FROM (" in out1["sql"]

    # Call 2 — identical request served from the materialized local cache.
    out2 = json.loads(ft.analyze_fleet_across_clouds(sources="local"))
    assert out2["mode"] == "local cache (materialized)"
    assert out2["row_count"] == 1
    assert out2["data"][0]["era"] == "2024"


# ─── PostgreSQL zone leg ───

def test_pg_config_defaults(monkeypatch):
    for k in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB",
              "POSTGRES_USER", "POSTGRES_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    cfg = cs.pg_config()
    assert cfg["host"] == "localhost" and cfg["port"] == "5432"
    assert cfg["db"] == "nyctaxi" and cfg["table"] == "taxi_zones"


def test_pg_config_env_override(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "pghost")
    monkeypatch.setenv("POSTGRES_PORT", "55432")
    assert cs.pg_config()["host"] == "pghost"
    assert cs.pg_config()["port"] == "55432"


def test_build_zone_sql_federates_to_postgresql(monkeypatch):
    import federation_tools as ft
    monkeypatch.setenv("POSTGRES_HOST", "pghost")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    sql = ft._build_zone_sql(year=2024, top_n=10)
    assert "postgresql('pghost:5432', 'nyctaxi', 'taxi_zones'" in sql
    assert "t.pickup_location_id = z.location_id" in sql
    assert "fare_amount >= 2.5" in sql          # junk-fare floor
    assert "toYear(t.pickup_datetime) = 2024" in sql
    assert "HAVING count() >= 500" in sql        # drop tiny zones
    assert "LIMIT 10" in sql


def test_zone_tipping_end_to_end(db_env, sample_db, monkeypatch):
    """The JOIN runs, but the sample_db has no Postgres — so it raises, proving
    the tool actually reaches postgresql() (not a silent stub)."""
    import federation_tools as ft
    monkeypatch.setattr(ft, "DB_PATH", sample_db)
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PORT", "1")  # nothing listening → connection error
    with pytest.raises(Exception):
        ft.analyze_zone_tipping(year=2024, top_n=5)


@pytest.mark.network
def test_full_multicloud_federation_smoke():
    """Real cross-cloud run — requires network + CLICKHOUSE_* creds. Opt-in only.

    Run with:  pytest -m network tests/test_federation.py
    """
    import os
    import federation_tools as ft

    if not os.getenv("CLICKHOUSE_PASSWORD"):
        pytest.skip("CLICKHOUSE_PASSWORD not set — skipping live multi-cloud run")

    out = json.loads(ft.analyze_fleet_across_clouds(sources="gcs,azure", refresh=True))
    eras = {r["era"] for r in out["data"]}
    assert {"2015", "2018"}.issubset(eras)
    for r in out["data"]:
        assert r["avg_tip_pct"] > 0


# ─── credential redaction (secrets must never leave the process in `sql`) ───

def test_redact_sql_masks_postgres_password():
    sql = ("SELECT * FROM postgresql('h:5432', 'nyctaxi', 'taxi_zones', "
           "'taxiuser', 'sup3rSecretPW')")
    out = cs.redact_sql(sql)
    assert "sup3rSecretPW" not in out
    assert "postgresql('h:5432', 'nyctaxi', 'taxi_zones', 'taxiuser', '***')" in out


def test_redact_sql_masks_remote_secure_password():
    sql = "remoteSecure('host:9440', 'workshop.nyc_taxi_trips', 'default', 'chPass123')"
    out = cs.redact_sql(sql)
    assert "chPass123" not in out
    assert "remoteSecure('host:9440', 'workshop.nyc_taxi_trips', 'default', '***')" in out


def test_redact_sql_leaves_credential_free_sql_untouched():
    sql = "SELECT * FROM nyc_taxi.yellow_trips WHERE fare_amount > 10 LIMIT 5"
    assert cs.redact_sql(sql) == sql


def test_zone_sql_password_is_redacted_in_returned_sql(monkeypatch):
    """The exact _build_zone_sql shape must match the redaction regex, so the
    password never appears in the JSON the tool hands back to the LLM."""
    import federation_tools as ft
    monkeypatch.setenv("POSTGRES_PASSWORD", "pgSecretPW")
    raw = ft._build_zone_sql(year=2024, top_n=5)
    assert "pgSecretPW" in raw                       # raw SQL carries the secret
    assert "pgSecretPW" not in cs.redact_sql(raw)    # redaction removes it


# ─── resilience: skipped legs are reported, not silently dropped ───

def test_federation_reports_unavailable_sources(db_env, sample_db, monkeypatch):
    """A missing ClickHouse Cloud leg is skipped (not fatal) and surfaced in
    `sources_unavailable` — the federation story stays honest about coverage."""
    import db
    import federation_tools as ft
    monkeypatch.setattr(db, "DB_PATH", sample_db)
    monkeypatch.setattr(ft, "DB_PATH", sample_db)
    for k in ("CLICKHOUSE_URL", "CLICKHOUSE_PASSWORD"):
        monkeypatch.delenv(k, raising=False)

    out = json.loads(ft.analyze_fleet_across_clouds(sources="local,chc", refresh=True))
    assert out["sources_used"] == ["local (chDB)"]
    assert out["sources_unavailable"] == ["ClickHouse Cloud"]
    assert "notes" in out and any("ClickHouse Cloud" in n for n in out["notes"])


# ─── ClickHouse Cloud creds: SSM fallback ───

def test_chc_credentials_from_ssm_when_env_absent(monkeypatch):
    """With no env vars, creds resolve from SSM /clickhouse/* and normalize to
    the native-secure remoteSecure() shape."""
    for k in ("CLICKHOUSE_URL", "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cs, "_chc_from_ssm", lambda: {
        "url": "https://xyz.eu-central-1.aws.clickhouse.cloud",
        "user": "default", "pwd": "sekret"})
    assert cs.chc_credentials() == {
        "host": "xyz.eu-central-1.aws.clickhouse.cloud",
        "port": "9440", "user": "default", "password": "sekret",
    }


def test_env_creds_take_precedence_over_ssm(monkeypatch):
    """Explicit env vars win over the SSM fallback."""
    monkeypatch.setenv("CLICKHOUSE_URL", "https://env.clickhouse.cloud")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "envpw")
    monkeypatch.setattr(cs, "_chc_from_ssm",
                        lambda: {"url": "https://ssm", "user": "u", "pwd": "ssmpw"})
    cfg = cs.chc_credentials()
    assert cfg["host"] == "env.clickhouse.cloud"
    assert cfg["password"] == "envpw"
