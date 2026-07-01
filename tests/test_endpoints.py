"""tests/test_endpoints.py — HTTP contract the single-file chat UI depends on.

The info panel reads `row_count` from /health and `version` from /info; these
tests lock those field names so a rename can't silently blank the UI again.
"""
from starlette.testclient import TestClient


def test_ping_is_healthy():
    import main
    with TestClient(main.app) as client:
        r = client.get("/ping")
    assert r.status_code == 200
    assert r.json()["status"] == "Healthy"


def test_info_exposes_version_and_endpoints():
    import main
    with TestClient(main.app) as client:
        r = client.get("/info")
    body = r.json()
    assert r.status_code == 200
    assert body.get("version")            # UI's info panel reads this
    assert "/chat" in body["endpoints"]


def test_health_reports_row_count(db_env, sample_db, monkeypatch):
    """/health must return `row_count` (the field the UI info panel reads)."""
    import db
    import main
    monkeypatch.setattr(db, "DB_PATH", sample_db)
    with TestClient(main.app) as client:
        r = client.get("/health")
    body = r.json()
    assert body["status"] == "healthy"
    assert body["row_count"] == 10
