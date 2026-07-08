"""tests/test_fleet_core.py — pure fleet-core logic (no AWS, offline).

The AWS-touching functions (run_one, wait_ready, ask, terminate) are exercised
live by the fleet demos; here we lock the pure helpers that decide the headline
numbers and the consensus verdict.
"""
import json

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


# ── Agentic fan-out: decompose one question → per-VM sub-questions → synthesize ─
#
# The plan/synthesize DECISION logic is pure and unit-tested here; the LLM planner
# and synthesizer are injected callables (a Bedrock CLI call in production), so the
# fallback ladder — curated → LLM → raw question — is exercised offline.


def test_curated_plan_returns_distinct_subquestions_for_default():
    plan = fc.curated_plan(fc.AGENTIC_DEFAULT_QUESTION)
    assert plan is not None
    assert len(plan) >= 3
    assert len(set(plan)) == len(plan)          # all sub-questions distinct


def test_curated_plan_none_for_unknown_question():
    assert fc.curated_plan("how many trips were there in 2024?") is None


def test_plan_subquestions_uses_curated_for_default_and_truncates_to_n():
    full = fc.curated_plan(fc.AGENTIC_DEFAULT_QUESTION)
    got = fc.plan_subquestions(fc.AGENTIC_DEFAULT_QUESTION, 2)
    assert got == full[:2]                       # capped to the requested fleet size


def test_plan_subquestions_uses_injected_planner_for_custom_question():
    def planner(question, n):
        return ["sub A", "sub B", "sub C"][:n]
    got = fc.plan_subquestions("some novel question", 3, planner=planner)
    assert got == ["sub A", "sub B", "sub C"]


def test_plan_subquestions_dedupes_and_strips_planner_output():
    def planner(question, n):
        return ["  keep one  ", "keep one", "keep two", "", "   "]
    got = fc.plan_subquestions("q", 5, planner=planner)
    assert got == ["keep one", "keep two"]       # trimmed, de-duplicated, blanks dropped


def test_plan_subquestions_falls_back_to_raw_when_planner_returns_too_few():
    def planner(question, n):
        return ["only one"]                       # < 2 distinct → not a real decomposition
    got = fc.plan_subquestions("q", 4, planner=planner)
    assert got == ["q"]


def test_plan_subquestions_falls_back_to_raw_when_planner_raises():
    def planner(question, n):
        raise RuntimeError("bedrock unavailable")
    got = fc.plan_subquestions("q", 4, planner=planner)
    assert got == ["q"]


def test_plan_subquestions_no_planner_custom_question_is_raw():
    assert fc.plan_subquestions("q", 4) == ["q"]


# ── synthesize (reduce) ──

def _answered(idx, subq, answer):
    return {"idx": idx, "subquestion": subq, "answer": answer, "chat_ms": 900}


def test_synthesize_template_combines_answered_subquestions():
    vms = [_answered(0, "busiest hour?", "6 PM with 690,932 trips."),
           _answered(1, "best tipping zone?", "JFK at 22%.")]
    out = fc.synthesize_template("briefing", vms)
    assert "busiest hour?" in out and "690,932" in out
    assert "best tipping zone?" in out and "JFK" in out


def test_synthesize_template_skips_error_and_empty_answers():
    vms = [_answered(0, "busiest hour?", "6 PM with 690,932 trips."),
           _answered(1, "broken?", "(error: timeout)"),
           _answered(2, "never?", "(never became ready)")]
    out = fc.synthesize_template("briefing", vms)
    assert "690,932" in out
    assert "error" not in out and "never became ready" not in out


def test_synthesize_template_handles_no_usable_answers():
    vms = [_answered(0, "broken?", "(error: timeout)")]
    out = fc.synthesize_template("briefing", vms)
    assert isinstance(out, str) and out            # non-empty, no crash


def test_synthesize_uses_injected_synthesizer_when_available():
    vms = [_answered(0, "q1", "a1"), _answered(1, "q2", "a2")]
    out = fc.synthesize("briefing", vms, synthesizer=lambda q, v: "LLM briefing")
    assert out == "LLM briefing"


def test_synthesize_falls_back_to_template_when_synthesizer_raises():
    vms = [_answered(0, "q1", "answer one"), _answered(1, "q2", "answer two")]
    def boom(q, v):
        raise RuntimeError("bedrock down")
    out = fc.synthesize("briefing", vms, synthesizer=boom)
    assert "answer one" in out and "answer two" in out


def test_synthesize_falls_back_to_template_when_synthesizer_empty():
    vms = [_answered(0, "q1", "answer one")]
    out = fc.synthesize("briefing", vms, synthesizer=lambda q, v: "   ")
    assert "answer one" in out


# ── ask() trace-context routing (the cross-process propagation contract) ──

def _fake_call_capturing(store, response="6 PM with 690,932 trips"):
    def fake_call(endpoint, path, token, *, method="GET", body=None, timeout=90):
        store["path"] = path
        store["body"] = body
        return 200, json.dumps({"response": response})
    return fake_call


def test_ask_without_trace_context_uses_chat(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(fc, "call", _fake_call_capturing(store))
    vm = {"idx": 0, "token": "tok", "endpoint": "ep"}
    fc.ask(vm, "busiest hour?")
    assert store["path"] == "/chat"
    assert store["body"] == {"text": "busiest hour?"}
    assert vm["answer"].startswith("6 PM")
    assert vm["fingerprint"] == "690932"


def test_ask_with_trace_context_routes_to_invocations(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(fc, "call", _fake_call_capturing(store, response="answer"))
    vm = {"idx": 0, "token": "tok", "endpoint": "ep"}
    fc.ask(vm, "sub?", trace_context={"trace_id": "T1", "parent_span_id": "S1"})
    assert store["path"] == "/invocations"
    assert store["body"] == {"text": "sub?", "trace_id": "T1", "parent_span_id": "S1"}


def test_ask_includes_session_id_in_body(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(fc, "call", _fake_call_capturing(store))
    vm = {"idx": 0, "token": "tok", "endpoint": "ep"}
    fc.ask(vm, "busiest hour?", session_id="consensus-abc123")
    assert store["path"] == "/chat"
    assert store["body"]["session_id"] == "consensus-abc123"


def test_consensus_run_generates_session_and_passes_to_workers(monkeypatch):
    seen_sessions: list = []
    monkeypatch.setattr(fc, "account", lambda region: "123456789012")
    monkeypatch.setattr(fc, "newest_ready_version", lambda img, region: "12.0")
    monkeypatch.setattr(fc, "run_one",
                        lambda i, img, v, exe, region, egress_connectors=None: {
                            "idx": i, "id": f"vm{i}", "endpoint": "ep"})
    monkeypatch.setattr(fc, "wait_ready",
                        lambda vm, region: {**vm, "ready_s": 1.0, "token": "t"})

    seen_names: list = []

    def fake_ask(vm, q, timeout=120, max_answer_chars=200, trace_context=None,
                 session_id=None, trace_name=None):
        seen_sessions.append(session_id)
        seen_names.append(trace_name)
        vm["answer"] = "6 PM 690932 trips"
        vm["chat_ms"] = 100
        vm["fingerprint"] = "690932"
        return vm

    monkeypatch.setattr(fc, "ask", fake_ask)
    monkeypatch.setattr(fc, "terminate_all", lambda vms, region: None)

    summary = fc.run_fleet_blocking(3, "us-west-2", "img", "busiest hour?", keep=True)
    assert summary["session_id"].startswith("consensus-")
    # every worker got the SAME session id → grouped in the Sessions view
    assert set(seen_sessions) == {summary["session_id"]}
    assert len(seen_sessions) == 3
    # and a descriptive trace name (not "POST /chat")
    assert set(seen_names) == {"consensus-worker"}


def test_ask_includes_trace_name_in_body(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(fc, "call", _fake_call_capturing(store))
    vm = {"idx": 0, "token": "tok", "endpoint": "ep"}
    fc.ask(vm, "q", session_id="s", trace_name="consensus-worker")
    assert store["body"]["trace_name"] == "consensus-worker"


# ── run_agentic_fleet_blocking stitches the fan-out into one trace tree ──

class _FakeSpan:
    def __init__(self, name):
        self.name = name
        self.trace_id = "TRACE"
        self.id = "span-" + name
        self.output = None
        self.ended = False

    def update(self, **kw):
        self.output = kw.get("output")

    def end(self):
        self.ended = True


class _FakeScope:
    def __init__(self, log, kwargs):
        self.log = log
        self.kwargs = kwargs

    def __enter__(self):
        self.log.append(("scope_enter", self.kwargs))
        return self

    def __exit__(self, *a):
        self.log.append(("scope_exit", None))
        return False


class _FakeTracer:
    """Records the trace-tree calls fleet_core makes, returns fake spans."""
    def __init__(self):
        self.calls: list = []
        self.flushed = False
        self.scope_kwargs: dict | None = None

    def run_scope(self, **kwargs):
        self.scope_kwargs = kwargs
        return _FakeScope(self.calls, kwargs)

    def start_root(self, question, n):
        self.calls.append(("root", n))
        return _FakeSpan("root")

    def child(self, parent, name, *, input=None):
        self.calls.append(("child", name))
        return _FakeSpan(name)

    def start_worker(self, root, idx, subquestion):
        self.calls.append(("worker", idx))
        return _FakeSpan(f"agent-{idx}")

    @staticmethod
    def context(span):
        return {"trace_id": span.trace_id, "parent_span_id": span.id}

    def flush(self):
        self.flushed = True


def _stub_aws(monkeypatch, seen_ctx):
    monkeypatch.setattr(fc, "account", lambda region: "123456789012")
    monkeypatch.setattr(fc, "newest_ready_version", lambda img, region: "9.0")
    monkeypatch.setattr(fc, "run_one",
                        lambda i, img, v, exe, region, egress_connectors=None: {
                            "idx": i, "id": f"vm{i}", "endpoint": "ep"})

    def fake_wait(vm, region):
        vm["ready_s"] = 1.0
        vm["token"] = "tok"
        return vm

    def fake_ask(vm, q, timeout=120, max_answer_chars=200, trace_context=None):
        vm["answer"] = f"ans-{vm['idx']}"
        vm["chat_ms"] = 100
        vm["fingerprint"] = None
        seen_ctx[vm["idx"]] = trace_context
        return vm

    monkeypatch.setattr(fc, "wait_ready", fake_wait)
    monkeypatch.setattr(fc, "ask", fake_ask)
    monkeypatch.setattr(fc, "terminate_all", lambda vms, region: None)


def test_agentic_run_stitches_one_trace_and_propagates_context(monkeypatch):
    seen_ctx: dict = {}
    _stub_aws(monkeypatch, seen_ctx)
    tracer = _FakeTracer()

    summary = fc.run_agentic_fleet_blocking(
        "Q", "us-west-2", "img", count=2,
        planner=lambda q, n: ["subA", "subB"],
        synthesizer=lambda q, v: "FINAL BRIEFING",
        tracer=tracer, keep=True)

    assert summary["type"] == "done"
    assert summary["trace_id"] == "TRACE"           # root trace id surfaced
    # session grouping: a session_id is set, surfaced, and the scope wraps the run
    assert summary["session_id"] and summary["session_id"].startswith("agentic-")
    assert tracer.scope_kwargs["session_id"] == summary["session_id"]
    assert "agentic-fanout" in tracer.scope_kwargs["tags"]
    assert tracer.scope_kwargs["user_id"] == "fleet-console"
    kinds = [c[0] for c in tracer.calls]
    assert kinds[0] == "scope_enter" and kinds[-1] == "scope_exit"   # wraps the run
    assert kinds.count("root") == 1
    assert ("child", "plan") in tracer.calls
    assert ("child", "synthesis") in tracer.calls
    assert kinds.count("worker") == 2               # one span per MicroVM
    assert tracer.flushed is True
    # every worker was linked to the SAME trace, under DISTINCT parent spans
    assert seen_ctx[0]["trace_id"] == "TRACE" == seen_ctx[1]["trace_id"]
    assert seen_ctx[0]["parent_span_id"] != seen_ctx[1]["parent_span_id"]


def test_run_one_omits_egress_connector_by_default(monkeypatch):
    captured = {}
    def fake_aws(*args, region, **kw):
        captured["args"] = args
        return {"microvmId": "vm0", "endpoint": "ep"}
    monkeypatch.setattr(fc, "aws", fake_aws)
    fc.run_one(0, "img", "9.0", "exe", "us-west-2")
    assert "--egress-network-connectors" not in captured["args"]


def test_run_one_passes_egress_connector_when_given(monkeypatch):
    captured = {}
    def fake_aws(*args, region, **kw):
        captured["args"] = args
        return {"microvmId": "vm0", "endpoint": "ep"}
    monkeypatch.setattr(fc, "aws", fake_aws)
    arn = "arn:aws:lambda:us-west-2:123:network-connector:abc"
    fc.run_one(0, "img", "9.0", "exe", "us-west-2", egress_connectors=[arn])
    args = captured["args"]
    assert "--egress-network-connectors" in args
    assert arn in args
    # flag must precede the connector value
    assert args.index("--egress-network-connectors") < args.index(arn)


def test_agentic_run_untraced_when_no_tracer(monkeypatch):
    seen_ctx: dict = {}
    _stub_aws(monkeypatch, seen_ctx)
    summary = fc.run_agentic_fleet_blocking(
        "Q", "us-west-2", "img", count=2,
        planner=lambda q, n: ["subA", "subB"],
        synthesizer=lambda q, v: "FINAL", keep=True)
    assert summary["type"] == "done"
    assert summary["trace_id"] is None              # no tracer → no trace id
    assert seen_ctx[0] is None and seen_ctx[1] is None  # workers get no context
