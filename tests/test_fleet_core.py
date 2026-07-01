"""tests/test_fleet_core.py — pure fleet-core logic (no AWS, offline).

The AWS-touching functions (run_one, wait_ready, ask, terminate) are exercised
live by the fleet demos; here we lock the pure helpers that decide the headline
numbers and the consensus verdict.
"""
import fleet_core as fc


# ── clamp_count ──

def test_clamp_count_bounds():
    assert fc.clamp_count(0) == 1
    assert fc.clamp_count(5) == 5
    assert fc.clamp_count(999) == fc.MAX_FLEET
    assert fc.clamp_count(-3) == 1


# ── fingerprint (largest integer, commas stripped) ──

def test_fingerprint_extracts_trip_count_with_commas():
    assert fc.fingerprint("The busiest hour is 6 PM (18:00) with 690,932 trips.") == "690932"


def test_fingerprint_ignores_hour_phrasing():
    # "6 PM" vs "18:00" must not change the fingerprint — the count dominates.
    a = fc.fingerprint("6 PM, 690932 trips")
    b = fc.fingerprint("Hour 18:00 had 690932 trips")
    assert a == b == "690932"


def test_fingerprint_none_when_no_digits():
    assert fc.fingerprint("(never became ready)") is None
    assert fc.fingerprint("") is None
    assert fc.fingerprint(None) is None


# ── consensus ──

def _vm(idx, fp):
    return {"idx": idx, "fingerprint": fp, "chat_ms": 800}


def test_consensus_all_agree():
    vms = [_vm(0, "690932"), _vm(1, "690932"), _vm(2, "690932")]
    c = fc.consensus(vms)
    assert c["agree"] is True
    assert c["answered"] == 3
    assert c["majority"] == 3
    assert c["groups"] == {"690932": 3}


def test_consensus_detects_divergence():
    vms = [_vm(0, "690932"), _vm(1, "690932"), _vm(2, "111111")]
    c = fc.consensus(vms)
    assert c["agree"] is False
    assert c["answered"] == 3
    assert c["majority"] == 2
    assert c["groups"] == {"690932": 2, "111111": 1}


def test_consensus_skips_unanswered_vms():
    vms = [_vm(0, "690932"), {"idx": 1, "fingerprint": None}, _vm(2, "690932")]
    c = fc.consensus(vms)
    assert c["agree"] is True          # the two that answered agree
    assert c["answered"] == 2


def test_consensus_no_answers_is_not_agreement():
    c = fc.consensus([{"idx": 0, "fingerprint": None}])
    assert c["agree"] is False
    assert c["answered"] == 0
    assert c["majority"] == 0


# ── ARN builders ──

def test_arn_builders():
    assert fc.image_arn("123456789012", "us-west-2", "nyc-taxi-agent-microvm") == \
        "arn:aws:lambda:us-west-2:123456789012:microvm-image:nyc-taxi-agent-microvm"
    assert fc.exec_role_arn("123456789012") == \
        "arn:aws:iam::123456789012:role/NycTaxiMicroVMExecutionRole"
