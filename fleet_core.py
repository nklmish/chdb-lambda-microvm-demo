"""fleet_core — shared fan-out logic for the Lambda MicroVMs fleet demos.

One library, two front-ends: the headless CLI (scripts/microvm_fleet_demo.py)
and the live browser console (scripts/fleet_console.py) both drive the fleet
through these functions, so there is a single, tested source of truth for how a
fleet is launched, polled, questioned, and torn down.

The fleet's point: launch N MicroVMs from the same image, each carrying its own
private, snapshot-hot chDB engine, then fire the same analytical question at all
of them at once. They answer concurrently with no shared backend to contend on —
and because every VM baked chDB from the *same* image, all N must return the
*same* number, which `consensus()` turns into a live snapshot-fidelity check.

Everything shells out to the AWS CLI (no boto dependency) and is account-agnostic.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_REGION = "us-west-2"
DEFAULT_NAME = "nyc-taxi-agent-microvm"
EXEC_ROLE_NAME = "NycTaxiMicroVMExecutionRole"
MAX_FLEET = 50
# Phrased so the answer always carries the exact trip count as an integer — the
# fingerprint below keys on the largest integer, so consensus is robust to the
# LLM's wording ("6 PM" vs "18:00") as long as the count is present.
DEFAULT_QUESTION = (
    "What is the single busiest pickup hour of the day by trip count? "
    "Answer in one short sentence and include the exact trip count."
)


# ── AWS CLI wrapper ──────────────────────────────────────────────────────────

_THROTTLE_MARKERS = ("Throttling", "TooManyRequests", "Rate exceeded", "RequestLimitExceeded")


def aws(*args: str, region: str, retries: int = 5) -> dict | str:
    """Run an AWS CLI command; parse JSON when possible, else return raw text.

    Retries with exponential backoff on throttling — RunMicrovm/ResumeMicrovm are
    rate-limited to ~5/s, so a large fleet launched at once WILL get throttled;
    this keeps the fan-out reliable without the caller having to pace calls.
    """
    delay = 1.0
    for attempt in range(retries + 1):
        out = subprocess.run(
            ["aws", *args, "--region", region], capture_output=True, text=True
        )
        if out.returncode == 0:
            body = (out.stdout or "").strip()
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return body
        err = (out.stderr or "").strip()
        if attempt < retries and any(m in err for m in _THROTTLE_MARKERS):
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
            continue
        raise RuntimeError(err)


def account(region: str) -> str:
    return aws("sts", "get-caller-identity", region=region)["Account"]


def image_arn(account_id: str, region: str, name: str) -> str:
    return f"arn:aws:lambda:{region}:{account_id}:microvm-image:{name}"


def exec_role_arn(account_id: str) -> str:
    return f"arn:aws:iam::{account_id}:role/{EXEC_ROLE_NAME}"


def newest_ready_version(image_arn_: str, region: str) -> str:
    """Highest SUCCESSFUL image version, or raise if none is built yet."""
    items = aws("lambda-microvms", "list-microvm-image-versions",
                "--image-identifier", image_arn_, region=region).get("items", [])
    ready = [it for it in items if it.get("state") == "SUCCESSFUL"]
    if not ready:
        raise RuntimeError(
            "no SUCCESSFUL image version — run scripts/deploy_microvm.py first"
        )
    return str(max(ready, key=lambda it: float(it.get("imageVersion") or 0))
               .get("imageVersion"))


# ── Per-MicroVM lifecycle ────────────────────────────────────────────────────

def run_one(idx: int, image_arn_: str, version: str, exec_arn: str,
            region: str, egress_connectors: list[str] | None = None) -> dict:
    """Launch one MicroVM. Idle/suspend/max-duration policies are a safety net so
    a VM can never bill indefinitely even if a caller forgets to terminate.

    egress_connectors: optional Lambda MicroVMs VPC egress-connector ARNs. When
    given, this VM's outbound traffic routes through the connector's VPC (so it can
    reach a private Aurora over native TCP for the zone-tipping federation). Only
    the agentic fleet passes one; the scan fleet keeps cheap default public egress.
    """
    extra = []
    if egress_connectors:
        extra = ["--egress-network-connectors", *egress_connectors]
    resp = aws(
        "lambda-microvms", "run-microvm",
        "--image-identifier", image_arn_, "--image-version", version,
        "--execution-role-arn", exec_arn,
        *extra,
        "--idle-policy", json.dumps(
            {"maxIdleDurationSeconds": 600, "suspendedDurationSeconds": 600,
             "autoResumeEnabled": True}
        ),
        "--maximum-duration-in-seconds", "3600",
        region=region,
    )
    return {"idx": idx, "id": resp["microvmId"], "endpoint": resp["endpoint"]}


def mint_token(microvm_id: str, region: str, minutes: int = 15) -> str:
    resp = aws("lambda-microvms", "create-microvm-auth-token",
               "--microvm-identifier", microvm_id,
               "--expiration-in-minutes", str(minutes),
               "--allowed-ports", json.dumps([{"port": 8080}]), region=region)
    return resp["authToken"]["X-aws-proxy-auth"]


def call(endpoint: str, path: str, token: str, *, method: str = "GET",
         body: dict | None = None, timeout: float = 90) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"https://{endpoint}{path}", data=data, method=method)
    req.add_header("X-aws-proxy-auth", token)
    req.add_header("X-aws-proxy-port", "8080")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return r.status, r.read().decode()


def wait_ready(vm: dict, region: str, timeout_s: float = 180) -> dict:
    """Poll /ping until the endpoint accepts traffic; records ready_s + token."""
    deadline = time.time() + timeout_s
    t0 = time.time()
    while time.time() < deadline:
        try:
            tok = mint_token(vm["id"], region)
            status, _ = call(vm["endpoint"], "/ping", tok, timeout=10)
            if status == 200:
                vm["ready_s"] = round(time.time() - t0, 1)
                vm["token"] = tok
                return vm
        except Exception:  # noqa: BLE001 — endpoint still warming
            pass
        time.sleep(5)
    vm["ready_s"] = None
    return vm


def ask(vm: dict, question: str, timeout: float = 120,
        max_answer_chars: int = 200, trace_context: dict | None = None,
        session_id: str | None = None, trace_name: str | None = None) -> dict:
    """Fire the question at a ready VM; records chat_ms, answer, fingerprint.

    max_answer_chars caps the stored answer for display: the consensus demo needs
    only the one-line count (200), but the agentic fan-out asks multi-part
    sub-questions whose answers run longer, so it passes a larger cap.

    trace_context {"trace_id", "parent_span_id"}: when supplied, the question is
    sent to /invocations (which the worker links into the caller's Langfuse trace
    via agent.run_agent_with_tracing) instead of the untraced /chat — so each
    MicroVM's own agent spans nest under the coordinator's fan-out trace.

    session_id: groups this worker's own trace into a Langfuse Session (used by the
    consensus fleet, where each worker produces a separate trace).
    """
    if not vm.get("token"):
        vm["answer"] = "(never became ready)"
        vm["chat_ms"] = None
        vm["fingerprint"] = None
        return vm
    t0 = time.time()
    try:
        if trace_context and trace_context.get("trace_id"):
            path = "/invocations"
            req = {"text": question,
                   "trace_id": trace_context.get("trace_id"),
                   "parent_span_id": trace_context.get("parent_span_id")}
        else:
            path, req = "/chat", {"text": question}
        if session_id:
            req["session_id"] = session_id
        if trace_name:
            req["trace_name"] = trace_name
        _, body = call(vm["endpoint"], path, vm["token"], method="POST",
                       body=req, timeout=timeout)
        answer = json.loads(body).get("response", "")
        vm["answer"] = answer[:max_answer_chars]
        vm["fingerprint"] = fingerprint(answer)
    except Exception as e:  # noqa: BLE001
        vm["answer"] = f"(error: {str(e)[:80]})"
        vm["fingerprint"] = None
    vm["chat_ms"] = round((time.time() - t0) * 1000)
    return vm


def terminate(microvm_id: str, region: str) -> None:
    aws("lambda-microvms", "terminate-microvm",
        "--microvm-identifier", microvm_id, region=region)


def terminate_all(vms: list[dict], region: str) -> None:
    """Terminate a whole fleet in parallel (fast teardown even under the ~10/s
    TerminateMicrovm throttle; aws() retries throttling per call)."""
    if not vms:
        return
    with ThreadPoolExecutor(max_workers=min(len(vms), 16)) as ex:
        list(ex.map(lambda v: _try(terminate, v["id"], region), vms))


# ── Pure helpers (unit-tested, no AWS) ───────────────────────────────────────

def clamp_count(n: int, lo: int = 1, hi: int = MAX_FLEET) -> int:
    return max(lo, min(int(n), hi))


def fingerprint(answer: str | None) -> str | None:
    """Canonical value of an answer for the consensus check: the largest integer
    it contains (commas stripped) — the trip count for the default question.

    Robust to LLM phrasing differences ("6 PM (690,932 trips)" vs
    "18:00, 690932 trips") because the count is what all VMs must agree on.
    Returns None when the answer has no digits (e.g. an error string).
    """
    if not answer:
        return None
    nums = [int(m.replace(",", "")) for m in re.findall(r"\d[\d,]*\d|\d", answer)]
    return str(max(nums)) if nums else None


def consensus(vms: list[dict]) -> dict:
    """Summarize agreement across the fleet's answered VMs.

    Returns {agree, answered, groups, majority}. `agree` is True only when every
    answered VM produced the same fingerprint — i.e. all private chDB stores,
    baked from the same image, returned the identical number.
    """
    fps = [v.get("fingerprint") for v in vms if v.get("fingerprint")]
    groups: dict[str, int] = {}
    for f in fps:
        groups[f] = groups.get(f, 0) + 1
    return {
        "agree": len(groups) == 1 and len(fps) > 0,
        "answered": len(fps),
        "groups": groups,
        "majority": max(groups.values()) if groups else 0,
    }


def run_fleet_blocking(n: int, region: str, name: str, question: str,
                       *, on_event=None, keep: bool = False,
                       session_id: str | None = None) -> dict:
    """Launch → wait → ask → (terminate) a fleet, calling on_event(dict) at each
    milestone. Used by both front-ends. Returns the final summary dict.

    on_event receives dicts with a "type": preflight | launch | ready | answer |
    done | terminated | error. Termination is guaranteed in `finally` unless
    keep=True, even if a later phase raises.

    session_id: one Langfuse session for the whole consensus run — each worker
    produces its own trace, so passing a shared session_id groups them in the
    Sessions view (generated per run when not supplied).
    """
    def emit(ev: dict) -> None:
        if on_event:
            on_event(ev)

    sess_id = session_id or _gen_session_id("consensus")
    n = clamp_count(n)
    launched: list[dict] = []
    try:
        acct = account(region)
        img = image_arn(acct, region, name)
        exe = exec_role_arn(acct)
        version = newest_ready_version(img, region)
        emit({"type": "preflight", "n": n, "version": version,
              "region": region, "question": question})

        with ThreadPoolExecutor(max_workers=n) as ex:
            launched = list(ex.map(
                lambda i: run_one(i, img, version, exe, region), range(n)))
        emit({"type": "launch",
              "vms": [{"idx": vm["idx"], "id": vm["id"]} for vm in launched]})

        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [ex.submit(wait_ready, vm, region) for vm in launched]
            for fut in futures:
                vm = fut.result()
                emit({"type": "ready", "idx": vm["idx"], "ready_s": vm["ready_s"]})

        wall0 = time.time()
        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [ex.submit(ask, vm, question, session_id=sess_id,
                                 trace_name="consensus-worker")
                       for vm in launched]
            for fut in futures:
                vm = fut.result()
                emit({"type": "answer", "idx": vm["idx"], "chat_ms": vm["chat_ms"],
                      "answer": vm.get("answer", ""), "fingerprint": vm.get("fingerprint")})
        wall_ms = round((time.time() - wall0) * 1000)

        ok = sum(1 for vm in launched if vm.get("chat_ms"))
        summary = {"type": "done", "wall_ms": wall_ms, "ok": ok, "total": n,
                   "consensus": consensus(launched), "session_id": sess_id}
        emit(summary)
        return summary
    except Exception as e:  # noqa: BLE001 — surface, then always clean up
        emit({"type": "error", "message": str(e)})
        return {"type": "error", "message": str(e)}
    finally:
        if not keep and launched:
            terminate_all(launched, region)
            emit({"type": "terminated", "ids": [vm["id"] for vm in launched]})


# ── Distributed scan over the S3 cold lake (scatter / gather) ────────────────
#
# Lambda MicroVMs pricing (Graviton), us-east-1 basis — https://aws.amazon.com/lambda/pricing/
PRICE_VCPU_SEC = 0.0000276944
PRICE_GB_SEC = 0.0000036667
PRICE_SNAPSHOT_READ_GB = 0.00155    # resume / launch
PRICE_SNAPSHOT_WRITE_GB = 0.0038    # suspend
PRICE_SNAPSHOT_STORE_GB_MONTH = 0.08

DEFAULT_LAKE_PREFIX = "lake/yellow"
OVERTURE_BUCKET = "overturemaps-us-west-2"
DEFAULT_SCAN_DATASET = "buildings"

# Coordinator-side dataset registry (mirrors scan_tools.DATASETS, minus the SQL —
# fleet_core is the client and does not import chDB). `bucket=None` → the private
# lake bucket resolved from the caller identity.
SCAN_DATASETS: dict[str, dict] = {
    "taxi": {"bucket": None, "prefix": DEFAULT_LAKE_PREFIX, "auth": "role",
             "needs_release": False, "label": "NYC yellow-taxi lake",
             "answer": "tip rate by year", "rows_hint": "~0.8B"},
    "buildings": {"bucket": OVERTURE_BUCKET,
                  "prefix": "release/{release}/theme=buildings/type=building",
                  "auth": "nosign", "needs_release": True, "label": "Overture buildings",
                  "answer": "buildings by class", "rows_hint": "~2.5B"},
    "segments": {"bucket": OVERTURE_BUCKET,
                 "prefix": "release/{release}/theme=transportation/type=segment",
                 "auth": "nosign", "needs_release": True, "label": "Overture road network",
                 "answer": "road segments by class", "rows_hint": "~0.3B"},
}


def suspend(microvm_id: str, region: str) -> None:
    aws("lambda-microvms", "suspend-microvm", "--microvm-identifier", microvm_id, region=region)


def resume(microvm_id: str, region: str) -> None:
    aws("lambda-microvms", "resume-microvm", "--microvm-identifier", microvm_id, region=region)


# The MicroVM's memory is configured on the image (minimumMemoryInMiB); the API
# does NOT expose the runtime vCPU allocation, so we read the real memory and
# treat vCPU as a stated assumption — the cost is labelled accordingly and scales
# linearly with the allocation.
ASSUMED_VCPU = 2.0


def image_memory_gb(image_arn_: str, region: str, default: float = 2.0) -> float:
    """Configured memory (GB) from the image's newest SUCCESSFUL version."""
    try:
        items = aws("lambda-microvms", "list-microvm-image-versions",
                    "--image-identifier", image_arn_, region=region).get("items", [])
        ready = [it for it in items if it.get("state") == "SUCCESSFUL"]
        latest = max(ready, key=lambda it: float(it.get("imageVersion") or 0))
        for r in latest.get("resources", []):
            mib = r.get("minimumMemoryInMiB")
            if mib:
                return round(float(mib) / 1024.0, 2)
    except Exception:  # noqa: BLE001
        pass
    return default


def lake_months(bucket: str, region: str, prefix: str = DEFAULT_LAKE_PREFIX) -> list[str]:
    """Months actually staged in the lake (so we only scan what exists)."""
    keys = aws("s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix,
               "--query", "Contents[].Key", "--output", "json", region=region)
    out: list[str] = []
    for k in (keys or []):
        m = re.search(r"yellow_tripdata_(\d{4}-\d{2})\.parquet", k or "")
        if m:
            out.append(m.group(1))
    return sorted(set(out))


def shard_months(months: list[str], n: int) -> list[list[str]]:
    """Round-robin split a file/month list so each shard gets a balanced mix."""
    shards: list[list[str]] = [[] for _ in range(n)]
    for i, m in enumerate(months):
        shards[i % n].append(m)
    return [s for s in shards if s]


def overture_release(region: str) -> str:
    """Newest Overture Maps release id (public bucket, anonymous list)."""
    res = aws("s3api", "list-objects-v2", "--bucket", OVERTURE_BUCKET,
              "--prefix", "release/", "--delimiter", "/", "--no-sign-request",
              "--query", "CommonPrefixes[].Prefix", "--output", "json", region=region) or []
    rels = sorted(p.strip("/").split("/")[-1] for p in res if p)
    if not rels:
        raise RuntimeError("no Overture release found")
    return rels[-1]


def dataset_files(dataset: str, region: str, lake_bucket: str) -> tuple[str | None, str, list[str]]:
    """Discover a dataset's shardable parquet basenames.

    Returns (release_or_None, bucket, [basenames]). For the private taxi lake we
    sign the list; for the public Overture themes we list anonymously.
    """
    ds = SCAN_DATASETS.get(dataset)
    if ds is None:
        raise RuntimeError(f"unknown dataset: {dataset!r} (allowed: {', '.join(SCAN_DATASETS)})")
    bucket = ds["bucket"] or lake_bucket
    if not ds["needs_release"]:  # taxi
        months = lake_months(bucket, region, ds["prefix"])
        if not months:
            raise RuntimeError(f"lake is empty: s3://{bucket}/{ds['prefix']} — run scripts/stage_lake.py")
        return None, bucket, [f"yellow_tripdata_{m}.parquet" for m in months]
    release = overture_release(region)
    prefix = ds["prefix"].format(release=release)
    extra = ["--no-sign-request"] if ds["auth"] == "nosign" else []
    keys = aws("s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix,
               *extra, "--query", "Contents[].Key", "--output", "json", region=region) or []
    files = sorted(k.split("/")[-1] for k in keys if k and k.endswith(".parquet"))
    if not files:
        raise RuntimeError(f"no parquet files under s3://{bucket}/{prefix}")
    return release, bucket, files


def merge_partials(results: list[dict]) -> dict:
    """Gather step: sum every shard's per-group partial into merged group totals.

    Dataset-agnostic — sums each numeric field per group key `grp`. `answer_rows`
    then turns the merged groups into the display answer for the dataset.
    """
    groups: dict[str, dict] = {}
    total_rows = total_bytes = 0
    for r in results:
        if not r or r.get("error"):
            continue
        total_rows += int(r.get("rows_scanned", 0) or 0)
        total_bytes += int(r.get("bytes_read", 0) or 0)
        for row in r.get("partial", []):
            g = groups.setdefault(str(row.get("grp")), {})
            for k, v in row.items():
                if k == "grp":
                    continue
                try:
                    g[k] = g.get(k, 0.0) + float(v or 0)
                except (TypeError, ValueError):
                    pass
    return {"groups": groups, "total_rows": total_rows, "total_bytes": total_bytes}


def answer_rows(dataset: str, merged: dict, top: int = 12) -> list[dict]:
    """Format merged groups into display rows [{label, count, value, unit}]."""
    g = merged.get("groups", {})
    if dataset == "taxi":
        rows = []
        for year, m in g.items():
            fare, tip, cnt = m.get("fare_sum", 0), m.get("tip_sum", 0), int(m.get("cnt", 0))
            rows.append({"label": year, "count": cnt, "unit": "%",
                         "value": round(tip / fare * 100, 2) if fare else 0})
        return sorted(rows, key=lambda r: r["label"])
    # "by class" answer = the classified distribution; the absence of a class
    # ('(unclassified)') is not itself a class, so drop it from the chart (the
    # headline row count already includes every row, classified or not).
    skip = {"(unclassified)", "(none)", "", "None"}
    rows = [{"label": k, "count": int(m.get("cnt", 0)),
             "value": int(m.get("cnt", 0)), "unit": ""}
            for k, m in g.items() if k not in skip]
    return sorted(rows, key=lambda r: -r["count"])[:top]


def cost_estimate(n: int, vcpu: float, mem_gb: float, run_seconds: float,
                  snapshot_gb: float) -> dict:
    """Real per-burst cost from published Lambda MicroVM (Graviton) pricing.

    A burst = resume (snapshot read) + run (vCPU+mem seconds) + suspend (snapshot
    write). At rest, only snapshot storage bills (no compute) — the $0-at-rest story.
    """
    per_run = run_seconds * (vcpu * PRICE_VCPU_SEC + mem_gb * PRICE_GB_SEC)
    per_resume = snapshot_gb * PRICE_SNAPSHOT_READ_GB
    per_suspend = snapshot_gb * PRICE_SNAPSHOT_WRITE_GB
    return {
        "burst_usd": round(n * (per_resume + per_run + per_suspend), 4),
        "compute_usd": round(n * per_run, 4),
        "snapshot_usd": round(n * (per_resume + per_suspend), 4),
        "at_rest_usd_per_hour": round(n * snapshot_gb * PRICE_SNAPSHOT_STORE_GB_MONTH / 730.0, 5),
        "n": n, "vcpu": vcpu, "mem_gb": mem_gb, "snapshot_gb": snapshot_gb,
        "run_seconds": round(run_seconds, 2),
    }


def scan_one(vm: dict, dataset: str, release: str | None, files: list[str],
             region: str, timeout: float = 300, attempts: int = 2) -> dict:
    """Send a MicroVM its file-shard via /scan; record its partial + timing.

    Retries once (with a fresh token) on a transient failure — a single flaky S3
    read or a not-quite-resumed VM shouldn't drop a whole shard from the merge.
    """
    t0 = time.time()
    last_err = "no attempt"
    for attempt in range(attempts):
        try:
            vm["token"] = mint_token(vm["id"], region)
            status, body = call(vm["endpoint"], "/scan", vm["token"], method="POST",
                                body={"dataset": dataset, "release": release, "files": files},
                                timeout=timeout)
            data = json.loads(body)
            if status != 200:
                raise RuntimeError(data.get("error", f"HTTP {status}"))
            vm["scan"] = data
            vm["rows_scanned"] = int(data.get("rows_scanned", 0))
            vm["bytes_read"] = int(data.get("bytes_read", 0))
            vm["scan_ms"] = round((time.time() - t0) * 1000)
            vm["files"] = files
            return vm
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:120]
            if attempt + 1 < attempts:
                time.sleep(3)
    vm["scan"] = {"error": last_err}
    vm["rows_scanned"] = 0
    vm["bytes_read"] = 0
    vm["scan_ms"] = round((time.time() - t0) * 1000)
    vm["files"] = files
    return vm


def resume_hot(vm: dict, region: str, timeout_s: float = 60) -> dict:
    """Resume a suspended VM and time snapshot-hot readiness (resume → /ping 200)."""
    t0 = time.time()
    try:
        resume(vm["id"], region)
    except Exception:  # noqa: BLE001 — may already be resuming (auto-resume)
        pass
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            tok = mint_token(vm["id"], region)
            status, _ = call(vm["endpoint"], "/ping", tok, timeout=8)
            if status == 200:
                vm["token"] = tok
                vm["resume_ms"] = round((time.time() - t0) * 1000)
                return vm
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    vm["resume_ms"] = None
    return vm


def _median(xs: list) -> float | None:
    vals = sorted(v for v in xs if v is not None)
    if not vals:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2


def run_scan_fleet_blocking(n: int, region: str, name: str, *,
                            dataset: str = DEFAULT_SCAN_DATASET, lake_bucket: str = "",
                            max_files: int | None = None, on_event=None,
                            keep: bool = False, burst: bool = True) -> dict:
    """Scatter/gather a distributed analytical scan over a sharded S3 dataset.

    Launches N MicroVMs, (optionally) suspends the fleet to $0 then resumes it
    snapshot-hot (timed), fans one file-shard of the dataset at each VM's /scan,
    gathers the mergeable partials, and reports throughput + real Lambda-MicroVM
    cost + the merged answer. Teardown is guaranteed in `finally` unless keep=True.
    """
    def emit(ev: dict) -> None:
        if on_event:
            on_event(ev)

    n = clamp_count(n)
    launched: list[dict] = []
    try:
        acct = account(region)
        img = image_arn(acct, region, name)
        exe = exec_role_arn(acct)
        version = newest_ready_version(img, region)

        release, bucket, files = dataset_files(dataset, region, lake_bucket)
        if max_files:
            files = files[:max_files]
        shards = shard_months(files, n)  # generic round-robin file split
        n = len(shards)
        meta = SCAN_DATASETS.get(dataset, {})
        emit({"type": "preflight", "n": n, "version": version, "region": region,
              "dataset": dataset, "label": meta.get("label", dataset),
              "answer_label": meta.get("answer", ""), "rows_hint": meta.get("rows_hint", ""),
              "release": release, "files": len(files)})

        with ThreadPoolExecutor(max_workers=min(n, 8)) as ex:
            launched = list(ex.map(lambda i: run_one(i, img, version, exe, region), range(n)))
        emit({"type": "launch", "vms": [{"idx": v["idx"], "id": v["id"]} for v in launched]})

        with ThreadPoolExecutor(max_workers=n) as ex:
            for fut in [ex.submit(wait_ready, v, region) for v in launched]:
                v = fut.result()
                emit({"type": "ready", "idx": v["idx"], "ready_s": v["ready_s"]})

        mem_gb = image_memory_gb(img, region)     # real configured memory
        vcpu = ASSUMED_VCPU                        # not exposed by the API (assumption)
        snapshot_gb = mem_gb  # memory-snapshot size ≈ configured memory

        resume_ms = None
        if burst:
            emit({"type": "suspend", "note": "fleet suspended → $0 compute at rest"})
            with ThreadPoolExecutor(max_workers=n) as ex:
                list(ex.map(lambda v: _try(suspend, v["id"], region), launched))
            time.sleep(2)
            with ThreadPoolExecutor(max_workers=n) as ex:
                for fut in [ex.submit(resume_hot, v, region) for v in launched]:
                    v = fut.result()
                    emit({"type": "resumed", "idx": v["idx"], "resume_ms": v.get("resume_ms")})
            resume_ms = _median([v.get("resume_ms") for v in launched])

        for v, sh in zip(launched, shards):
            v["shard"] = sh
        emit({"type": "scatter",
              "shards": [{"idx": v["idx"], "files": len(v["shard"])} for v in launched]})

        wall0 = time.time()
        with ThreadPoolExecutor(max_workers=n) as ex:
            for fut in [ex.submit(scan_one, v, dataset, release, v["shard"], region)
                        for v in launched]:
                v = fut.result()
                emit({"type": "scanned", "idx": v["idx"], "rows": v["rows_scanned"],
                      "bytes": v["bytes_read"], "scan_ms": v["scan_ms"]})
        wall_ms = round((time.time() - wall0) * 1000)

        merged = merge_partials([v.get("scan") for v in launched])
        run_seconds = max(wall_ms / 1000.0, 0.001)
        rows = merged["total_rows"]
        gb = merged["total_bytes"] / 1e9
        cost = cost_estimate(n, vcpu, mem_gb, run_seconds, snapshot_gb)
        summary = {
            "type": "done", "n": n, "wall_ms": wall_ms, "dataset": dataset,
            "answer_label": meta.get("answer", ""),
            "total_rows": rows, "total_gb": round(gb, 2),
            "rows_per_s": round(rows / run_seconds), "gb_per_s": round(gb / run_seconds, 2),
            "resume_ms": resume_ms, "answer": answer_rows(dataset, merged), "cost": cost,
            "ok": sum(1 for v in launched if v.get("rows_scanned")),
        }
        emit(summary)
        return summary
    except Exception as e:  # noqa: BLE001
        emit({"type": "error", "message": str(e)})
        return {"type": "error", "message": str(e)}
    finally:
        if not keep and launched:
            terminate_all(launched, region)
            emit({"type": "terminated", "ids": [v["id"] for v in launched]})


def _try(fn, *a):
    try:
        return fn(*a)
    except Exception:  # noqa: BLE001
        return None


# ── Agentic fan-out: decompose one question → per-VM sub-questions → synthesize ─
#
# A different pattern from consensus (same question at every VM) and the scan
# (split the *data*): here a coordinator decomposes ONE high-level question into
# distinct SUB-questions, fans a *different* sub-question at each MicroVM's agent
# (every VM holds the same complete private chDB, so each answers a different
# analytical facet correctly), then synthesizes the partials into one briefing.
#
# plan_subquestions/synthesize keep the DECISION logic pure and injectable: the
# LLM planner+synthesizer are passed in (a Bedrock CLI call in production), so the
# fallback ladder — curated → LLM → raw question — is testable without AWS.

# A broad, multi-faceted question whose curated decomposition maps each sub-question
# onto a distinct tool the worker agent already has (hour / zone tips / payment /
# weather) — so every card exercises a different analytical path. The console sends
# this verbatim as its default, which is what triggers the curated (always-green) plan.
AGENTIC_DEFAULT_QUESTION = (
    "Give me a rush-hour operations briefing for NYC yellow taxi: when is demand "
    "highest, where do drivers earn the best tips, how do riders pay, and how does "
    "weather change ridership?"
)

CURATED_PLANS: dict[str, list[str]] = {
    AGENTIC_DEFAULT_QUESTION: [
        "What is the single busiest pickup hour of the day by trip count? "
        "Answer in one short sentence and include the exact trip count.",
        "Which pickup zones have the highest revenue-weighted tip rate? "
        "Answer in one short sentence naming the top zone, its borough, and its tip rate.",
        "What is the split of trips by payment type (credit card vs cash)? "
        "Answer in one short sentence with the percentage for each.",
        "How does rain change ridership compared with dry days? "
        "Answer in one short sentence and cite the NOAA LaGuardia weather source.",
    ],
}


def curated_plan(question: str) -> list[str] | None:
    """The hand-authored decomposition for a known question, else None.

    Returned as a fresh copy so callers can slice/mutate without touching the
    registry.
    """
    plan = CURATED_PLANS.get(question)
    return list(plan) if plan else None


def _dedupe(items: list[str]) -> list[str]:
    """Trim, drop blanks, and de-duplicate preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        s = (it or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def plan_subquestions(question: str, n: int, *, planner=None) -> list[str]:
    """Decompose `question` into 1..n distinct sub-questions.

    Ladder: a curated plan for a known question wins (always green for the demo);
    otherwise an injected `planner(question, n) -> list[str]` LLM decomposes it;
    if that yields fewer than two usable sub-questions (or raises), fall back to
    fanning the raw question — a safe, visible degradation rather than a failure.
    The result is capped to `n` (the requested fleet size).
    """
    cap = max(1, int(n))
    curated = curated_plan(question)
    if curated:
        return curated[:cap]
    if planner is not None:
        try:
            subs = _dedupe(list(planner(question, cap)))
            if len(subs) >= 2:
                return subs[:cap]
        except Exception:  # noqa: BLE001 — planner is best-effort; fall back below
            pass
    return [question]


def synthesize_template(question: str, vms: list[dict]) -> str:
    """Deterministic reduce: fold each answered sub-question into one briefing.

    Skips VMs whose answer is an error/never-ready marker (those start with "(").
    Used as the always-available fallback when no LLM synthesizer is provided or
    the LLM call fails.
    """
    parts: list[str] = []
    for vm in vms:
        sq = (vm.get("subquestion") or "").strip()
        ans = (vm.get("answer") or "").strip()
        if sq and ans and not ans.startswith("("):
            parts.append(f"• {sq}\n  {ans}")
    if not parts:
        return "No sub-answers were produced by the fleet."
    return f"Combined briefing — {question}\n\n" + "\n\n".join(parts)


def synthesize(question: str, vms: list[dict], *, synthesizer=None) -> str:
    """Combine the fleet's per-sub-question answers into a final response.

    Uses an injected `synthesizer(question, vms) -> str` (an LLM reduce) when
    given and it returns non-empty text; otherwise (or on error) uses the
    deterministic template.
    """
    if synthesizer is not None:
        try:
            out = synthesizer(question, vms)
            if out and out.strip():
                return out.strip()
        except Exception:  # noqa: BLE001 — fall back to the deterministic template
            pass
    return synthesize_template(question, vms)


# ── Coordinator LLM via the AWS CLI (no boto/chDB dependency in this module) ────

DEFAULT_COORDINATOR_MODEL = "us.anthropic.claude-sonnet-4-20250514-v1:0"


def _bedrock_converse(prompt: str, region: str, model_id: str, *,
                      max_tokens: int = 1024, timeout: float = 60) -> str:
    """One text-in/text-out turn via `aws bedrock-runtime converse`.

    Kept CLI-only so fleet_core stays boto-free and account-agnostic, matching
    the rest of the module.
    """
    messages = json.dumps([{"role": "user", "content": [{"text": prompt}]}])
    inference = json.dumps({"maxTokens": max_tokens, "temperature": 0.2})
    out = subprocess.run(
        ["aws", "bedrock-runtime", "converse", "--model-id", model_id,
         "--messages", messages, "--inference-config", inference,
         "--region", region],
        capture_output=True, text=True, timeout=timeout,
    )
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "converse failed").strip()[:200])
    data = json.loads(out.stdout)
    return data["output"]["message"]["content"][0]["text"].strip()


def bedrock_planner(region: str, model_id: str = DEFAULT_COORDINATOR_MODEL):
    """Build an injectable planner that asks Bedrock to decompose a question into
    N sub-questions, returned as a JSON array of strings."""
    def planner(question: str, n: int) -> list[str]:
        prompt = (
            f"Decompose this analytical question about an NYC yellow-taxi dataset "
            f"into at most {n} independent sub-questions that can each be answered "
            f"on its own. Each sub-question must stand alone and target a distinct "
            f"facet. Reply with ONLY a JSON array of strings.\n\nQuestion: {question}"
        )
        text = _bedrock_converse(prompt, region, model_id)
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1:
            return []
        parsed = json.loads(text[start:end + 1])
        return [str(s) for s in parsed if isinstance(s, str)]
    return planner


def bedrock_synthesizer(region: str, model_id: str = DEFAULT_COORDINATOR_MODEL):
    """Build an injectable synthesizer that folds the fleet's sub-answers into one
    coherent briefing via Bedrock."""
    def synthesizer(question: str, vms: list[dict]) -> str:
        findings = "\n\n".join(
            f"Sub-question: {vm.get('subquestion','')}\nAnswer: {vm.get('answer','')}"
            for vm in vms
            if (vm.get("answer") or "").strip() and not (vm.get("answer") or "").startswith("(")
        )
        if not findings:
            return ""
        prompt = (
            f"You are combining independent findings from a fleet of agents, each of "
            f"which answered one sub-question about an NYC yellow-taxi dataset. Write a "
            f"concise briefing (a few sentences) that answers the overall question, "
            f"citing the concrete numbers from the findings. Do not invent numbers.\n\n"
            f"Overall question: {question}\n\nFindings:\n{findings}"
        )
        return _bedrock_converse(prompt, region, model_id, max_tokens=800)
    return synthesizer


def _safe(fn, *a, **k):
    """Call a tracer method, swallowing any error — observability must NEVER break
    a fleet run. Returns the result or None."""
    try:
        return fn(*a, **k)
    except Exception:  # noqa: BLE001
        return None


def _gen_session_id(prefix: str = "agentic") -> str:
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def run_agentic_fleet_blocking(question: str, region: str, name: str, *,
                               count: int = 4, on_event=None, keep: bool = False,
                               planner=None, synthesizer=None, tracer=None,
                               egress_connectors: list[str] | None = None,
                               session_id: str | None = None,
                               user_id: str = "fleet-console",
                               tags: list[str] | None = None) -> dict:
    """Plan → launch → wait → ask (a *different* sub-question per VM) → synthesize
    → (terminate) an agentic fleet, emitting on_event(dict) at each milestone.

    on_event receives dicts with a "type": preflight | launch | ready | plan |
    answer | synthesis | done | terminated | error. Termination is guaranteed in
    `finally` unless keep=True, even if a later phase raises.

    tracer (optional, duck-typed FleetTracer): when supplied, the whole fan-out is
    stitched into ONE Langfuse trace — a root span with plan / agent-i / synthesis
    children, and each worker's own agent trace propagated via trace_context so it
    nests under agent-i. Tracing failures never interrupt the run (see _safe).
    """
    def emit(ev: dict) -> None:
        if on_event:
            on_event(ev)

    # Langfuse session grouping: stamp session_id / user_id / tags on the whole
    # distributed trace so runs land grouped in the Sessions view. Entered BEFORE
    # any observation is created; exited in `finally`. Best-effort (never breaks).
    sess_id = session_id or _gen_session_id()
    _scope = None
    if tracer:
        _scope = _safe(tracer.run_scope, session_id=sess_id, user_id=user_id,
                       tags=tags or ["agentic-fanout", "microvm-fleet", f"region:{region}"],
                       metadata={"region": region, "fleet_size": clamp_count(count)})
        if _scope is not None:
            _safe(_scope.__enter__)

    root = _safe(tracer.start_root, question, clamp_count(count)) if tracer else None

    plan_span = _safe(tracer.child, root, "plan", input=question) if root else None
    plan = plan_subquestions(question, clamp_count(count), planner=planner)
    if plan_span:
        _safe(plan_span.update, output=plan)
        _safe(plan_span.end)
    n = len(plan)
    launched: list[dict] = []
    final: str | None = None
    try:
        acct = account(region)
        img = image_arn(acct, region, name)
        exe = exec_role_arn(acct)
        version = newest_ready_version(img, region)
        emit({"type": "preflight", "n": n, "version": version, "region": region,
              "question": question, "plan": plan})

        with ThreadPoolExecutor(max_workers=n) as ex:
            launched = list(ex.map(
                lambda i: run_one(i, img, version, exe, region,
                                  egress_connectors=egress_connectors), range(n)))
        emit({"type": "launch",
              "vms": [{"idx": vm["idx"], "id": vm["id"]} for vm in launched]})

        with ThreadPoolExecutor(max_workers=n) as ex:
            for fut in [ex.submit(wait_ready, vm, region) for vm in launched]:
                vm = fut.result()
                emit({"type": "ready", "idx": vm["idx"], "ready_s": vm["ready_s"]})

        for vm, sq in zip(launched, plan):
            vm["subquestion"] = sq
        emit({"type": "plan",
              "items": [{"idx": vm["idx"], "subquestion": vm["subquestion"]}
                        for vm in launched]})

        # One coordinator-side span per worker; each worker's own agent spans nest
        # under it via the trace_context we hand to /invocations.
        worker_spans: dict = {}
        if root:
            for vm in launched:
                worker_spans[vm["idx"]] = _safe(
                    tracer.start_worker, root, vm["idx"], vm["subquestion"])

        def ask_one(vm: dict) -> dict:
            ws = worker_spans.get(vm["idx"])
            tctx = _safe(tracer.context, ws) if ws else None
            ask(vm, vm["subquestion"], 120, 600, trace_context=tctx)
            if ws:
                _safe(ws.update, output=vm.get("answer"),
                      metadata={"microvm_id": vm.get("id"), "chat_ms": vm.get("chat_ms")})
                _safe(ws.end)
            return vm

        wall0 = time.time()
        with ThreadPoolExecutor(max_workers=n) as ex:
            for fut in [ex.submit(ask_one, vm) for vm in launched]:
                vm = fut.result()
                emit({"type": "answer", "idx": vm["idx"], "chat_ms": vm["chat_ms"],
                      "subquestion": vm.get("subquestion", ""),
                      "answer": vm.get("answer", "")})
        wall_ms = round((time.time() - wall0) * 1000)

        syn_span = _safe(tracer.child, root, "synthesis", input=question) if root else None
        final = synthesize(question, launched, synthesizer=synthesizer)
        if syn_span:
            _safe(syn_span.update, output=final)
            _safe(syn_span.end)
        emit({"type": "synthesis", "text": final})

        ok = sum(1 for vm in launched if vm.get("chat_ms"))
        trace_id = getattr(root, "trace_id", None) if root else None
        summary = {"type": "done", "wall_ms": wall_ms, "ok": ok, "total": n,
                   "synthesis": final, "trace_id": trace_id,
                   "session_id": sess_id if tracer else None}
        emit(summary)
        return summary
    except Exception as e:  # noqa: BLE001 — surface, then always clean up
        emit({"type": "error", "message": str(e)})
        return {"type": "error", "message": str(e)}
    finally:
        if root:
            _safe(root.update, output=final)
            _safe(root.end)
            _safe(tracer.flush)
        if _scope is not None:
            _safe(_scope.__exit__, None, None, None)
        if not keep and launched:
            terminate_all(launched, region)
            emit({"type": "terminated", "ids": [vm["id"] for vm in launched]})
