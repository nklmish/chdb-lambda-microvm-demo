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
            region: str) -> dict:
    """Launch one MicroVM. Idle/suspend/max-duration policies are a safety net so
    a VM can never bill indefinitely even if a caller forgets to terminate."""
    resp = aws(
        "lambda-microvms", "run-microvm",
        "--image-identifier", image_arn_, "--image-version", version,
        "--execution-role-arn", exec_arn,
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


def ask(vm: dict, question: str, timeout: float = 120) -> dict:
    """Fire the question at a ready VM; records chat_ms, answer, fingerprint."""
    if not vm.get("token"):
        vm["answer"] = "(never became ready)"
        vm["chat_ms"] = None
        vm["fingerprint"] = None
        return vm
    t0 = time.time()
    try:
        _, body = call(vm["endpoint"], "/chat", vm["token"], method="POST",
                       body={"text": question}, timeout=timeout)
        answer = json.loads(body).get("response", "")
        vm["answer"] = answer[:200]
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
                       *, on_event=None, keep: bool = False) -> dict:
    """Launch → wait → ask → (terminate) a fleet, calling on_event(dict) at each
    milestone. Used by both front-ends. Returns the final summary dict.

    on_event receives dicts with a "type": preflight | launch | ready | answer |
    done | terminated | error. Termination is guaranteed in `finally` unless
    keep=True, even if a later phase raises.
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
            futures = [ex.submit(ask, vm, question) for vm in launched]
            for fut in futures:
                vm = fut.result()
                emit({"type": "answer", "idx": vm["idx"], "chat_ms": vm["chat_ms"],
                      "answer": vm.get("answer", ""), "fingerprint": vm.get("fingerprint")})
        wall_ms = round((time.time() - wall0) * 1000)

        ok = sum(1 for vm in launched if vm.get("chat_ms"))
        summary = {"type": "done", "wall_ms": wall_ms, "ok": ok, "total": n,
                   "consensus": consensus(launched)}
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
