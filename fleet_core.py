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
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_REGION = "us-west-2"
DEFAULT_NAME = "nyc-taxi-agent-microvm"
EXEC_ROLE_NAME = "NycTaxiMicroVMExecutionRole"
MAX_FLEET = 20
# Phrased so the answer always carries the exact trip count as an integer — the
# fingerprint below keys on the largest integer, so consensus is robust to the
# LLM's wording ("6 PM" vs "18:00") as long as the count is present.
DEFAULT_QUESTION = (
    "What is the single busiest pickup hour of the day by trip count? "
    "Answer in one short sentence and include the exact trip count."
)


# ── AWS CLI wrapper ──────────────────────────────────────────────────────────

def aws(*args: str, region: str) -> dict | str:
    """Run an AWS CLI command; parse JSON when possible, else return raw text."""
    out = subprocess.run(
        ["aws", *args, "--region", region], capture_output=True, text=True
    )
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "").strip())
    body = (out.stdout or "").strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


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
            for vm in launched:
                try:
                    terminate(vm["id"], region)
                except Exception:  # noqa: BLE001 — best-effort teardown
                    pass
            emit({"type": "terminated", "ids": [vm["id"] for vm in launched]})
