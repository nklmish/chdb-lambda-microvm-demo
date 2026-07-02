"""tests/test_agentic_console.py — agentic console wiring (offline, no AWS).

The fan-out lifecycle is covered by fleet_core's unit tests and exercised live by
the demo; here we lock the thin server wiring: /config advertises the agentic
default question and fleet cap, and / serves the console page.
"""
import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import agentic_console as ac  # noqa: E402
import fleet_core as fc  # noqa: E402


def _client() -> TestClient:
    return TestClient(ac.build_app("us-west-2", "nyc-taxi-agent-microvm"))


def test_config_advertises_agentic_default_question_and_cap():
    c = _client().get("/config").json()
    assert c["question"] == fc.AGENTIC_DEFAULT_QUESTION
    assert c["maxFleet"] == fc.MAX_FLEET
    assert c["defaultRegion"] == "us-west-2"


def test_index_serves_agentic_console_page():
    r = _client().get("/")
    assert r.status_code == 200
    assert "Agentic Fan-out" in r.text
