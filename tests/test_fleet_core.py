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


# ── distributed-scan pure helpers ──

def test_shard_months_round_robin_balances():
    shards = fc.shard_months(["a", "b", "c", "d", "e"], 2)
    assert shards == [["a", "c", "e"], ["b", "d"]]


def test_shard_months_drops_empty_when_more_shards_than_months():
    shards = fc.shard_months(["a", "b"], 5)
    assert shards == [["a"], ["b"]]


def test_merge_partials_sums_groups_generically():
    p1 = {"rows_scanned": 100, "bytes_read": 10,
          "partial": [{"grp": "residential", "rows_read": 60, "cnt": 60}]}
    p2 = {"rows_scanned": 50, "bytes_read": 5,
          "partial": [{"grp": "residential", "rows_read": 40, "cnt": 40},
                      {"grp": "service", "rows_read": 10, "cnt": 10}]}
    m = fc.merge_partials([p1, p2, None])
    assert m["total_rows"] == 150 and m["total_bytes"] == 15
    assert m["groups"]["residential"]["cnt"] == 100     # 60 + 40
    ans = fc.answer_rows("segments", m)
    assert ans[0]["label"] == "residential" and ans[0]["count"] == 100  # sorted desc


def test_answer_rows_taxi_computes_tip_pct():
    m = {"groups": {"2015": {"cnt": 100, "tip_sum": 12.0, "fare_sum": 100.0},
                    "2016": {"cnt": 50, "tip_sum": 9.0, "fare_sum": 50.0}},
         "total_rows": 150, "total_bytes": 0}
    ans = fc.answer_rows("taxi", m)
    assert [r["label"] for r in ans] == ["2015", "2016"]   # sorted by year
    assert ans[0]["value"] == 12.0 and ans[0]["unit"] == "%"
    assert ans[1]["value"] == 18.0                          # 9/50*100


def test_cost_estimate_uses_real_rates():
    c = fc.cost_estimate(n=10, vcpu=2, mem_gb=4, run_seconds=5, snapshot_gb=4)
    # compute = 10 * 5 * (2*vcpu_rate + 4*gb_rate)
    per_run = 5 * (2 * fc.PRICE_VCPU_SEC + 4 * fc.PRICE_GB_SEC)
    assert c["compute_usd"] == round(10 * per_run, 4)
    assert c["burst_usd"] > c["compute_usd"]           # + snapshot read/write
    assert c["at_rest_usd_per_hour"] >= 0
    assert c["n"] == 10


def test_median():
    assert fc._median([3, 1, 2]) == 2
    assert fc._median([4, 1, 2, 3]) == 2.5
    assert fc._median([None, None]) is None
