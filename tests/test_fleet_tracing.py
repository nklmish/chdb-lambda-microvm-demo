"""tests/test_fleet_tracing.py — fleet_tracing pure logic (offline, no AWS/Langfuse).

The FleetTracer is a thin wrapper over a Langfuse client; here we drive it with a
fake client/span to lock the trace SHAPE (root → plan/agent-i/synthesis) and the
cross-process context the workers receive, without touching the network.
"""
import fleet_tracing as ft


# ── Fakes: record the calls a real Langfuse client/span would receive ──

class FakeSpan:
    def __init__(self, name, as_type, trace_id, span_id, log):
        self.name = name
        self.as_type = as_type
        self.trace_id = trace_id
        self.id = span_id
        self._log = log
        self.ended = False
        self.output = None

    def start_observation(self, *, name, as_type="span", input=None, metadata=None):
        self._log["spans"].append((name, as_type))
        child_id = f"span{len(self._log['spans'])}"
        return FakeSpan(name, as_type, self.trace_id, child_id, self._log)

    def update(self, *, output=None, metadata=None, **kw):
        self.output = output

    def end(self):
        self.ended = True


class FakeClient:
    def __init__(self):
        self.log = {"spans": [], "flushed": False}

    def start_observation(self, *, name, as_type="span", input=None, metadata=None):
        self.log["spans"].append((name, as_type))
        return FakeSpan(name, as_type, "traceABC", "rootSPAN", self.log)

    def flush(self):
        self.log["flushed"] = True


def _tracer():
    return ft.FleetTracer(FakeClient())


# ── root / child shape ──

def test_root_is_agent_observation_named_agentic_fanout():
    t = _tracer()
    root = t.start_root("briefing question", n=4)
    assert root.name == "agentic-fanout"
    assert root.as_type == "agent"
    assert t.client.log["spans"][0] == ("agentic-fanout", "agent")


def test_worker_span_named_by_index_and_is_agent_type():
    t = _tracer()
    root = t.start_root("q", n=2)
    w = t.start_worker(root, idx=1, subquestion="what is X?")
    assert w.name == "agent-1"
    assert w.as_type == "agent"


def test_context_gives_worker_the_trace_and_parent_span_ids():
    t = _tracer()
    root = t.start_root("q", n=1)
    w = t.start_worker(root, idx=0, subquestion="sub")
    ctx = ft.FleetTracer.context(w)
    assert ctx == {"trace_id": w.trace_id, "parent_span_id": w.id}
    # every worker shares the ONE trace id (fan-out under a single trace)
    assert w.trace_id == root.trace_id


def test_flush_delegates_to_client():
    t = _tracer()
    t.flush()
    assert t.client.log["flushed"] is True


def test_trace_url_uses_host_and_trace_id(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "https://cloud.langfuse.com/")
    assert ft.trace_url("abc123") == "https://cloud.langfuse.com/trace/abc123"


def test_trace_url_none_without_host(monkeypatch):
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    assert ft.trace_url("abc123") is None


# ── SSM cred loader is graceful when boto3/creds/params are absent ──

def test_load_langfuse_env_from_ssm_returns_false_on_failure(monkeypatch):
    # No AWS available in the test env → must return False, never raise.
    monkeypatch.setattr(ft, "_ssm_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no aws")))
    assert ft.load_langfuse_env_from_ssm(region="us-east-1") is False


def test_build_tracer_returns_none_when_langfuse_unavailable(monkeypatch):
    # If creds can't be loaded, build_fleet_tracer degrades to None (tracing off),
    # so a run without Langfuse still works exactly as before.
    monkeypatch.setattr(ft, "load_langfuse_env_from_ssm", lambda **k: False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    assert ft.build_fleet_tracer() is None
