#!/usr/bin/env python3
"""Fleet fan-out: N Lambda MicroVMs, each with its own private chDB, in parallel.

The blog's claim — *"There's no shared database to scale, and nothing for a
thousand agents to overload at once"* — made visible: launch N MicroVMs from the
same image, each carrying its own private, snapshot-hot chDB engine, then fire the
same analytical question at all of them at once. They answer concurrently with no
shared backend to contend on. Compare to a classic stack where N agents hammer one
warehouse/RDS and the tail latency (and the retry storm) explodes.

Account-agnostic and self-contained (shells out to the AWS CLI). Uses the image
built by scripts/deploy_microvm.py. Terminates the fleet at the end unless --keep.

Usage:
  python scripts/microvm_fleet_demo.py --count 5 --region us-west-2
  python scripts/microvm_fleet_demo.py --count 3 --keep        # leave them running
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_REGION = "us-west-2"
DEFAULT_NAME = "nyc-taxi-agent-microvm"
EXEC_ROLE_NAME = "NycTaxiMicroVMExecutionRole"
QUESTION = "What is the single busiest hour of the day by trip count? One sentence."


def aws(*args: str, region: str) -> dict | str:
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


def _account(region: str) -> str:
    return aws("sts", "get-caller-identity", region=region)["Account"]


def _newest_ready_version(image_arn: str, region: str) -> str:
    items = aws("lambda-microvms", "list-microvm-image-versions",
                "--image-identifier", image_arn, region=region).get("items", [])
    ready = [it for it in items if it.get("state") == "SUCCESSFUL"]
    if not ready:
        raise RuntimeError("no SUCCESSFUL image version — run scripts/deploy_microvm.py first")
    return str(max(ready, key=lambda it: float(it.get("imageVersion") or 0)).get("imageVersion"))


def _run_one(idx: int, image_arn: str, version: str, exec_arn: str, region: str) -> dict:
    resp = aws(
        "lambda-microvms", "run-microvm",
        "--image-identifier", image_arn, "--image-version", version,
        "--execution-role-arn", exec_arn,
        "--idle-policy", json.dumps(
            {"maxIdleDurationSeconds": 600, "suspendedDurationSeconds": 600, "autoResumeEnabled": True}
        ),
        "--maximum-duration-in-seconds", "3600",
        region=region,
    )
    return {"idx": idx, "id": resp["microvmId"], "endpoint": resp["endpoint"]}


def _token(microvm_id: str, region: str) -> str:
    resp = aws("lambda-microvms", "create-microvm-auth-token",
               "--microvm-identifier", microvm_id, "--expiration-in-minutes", "15",
               "--allowed-ports", json.dumps([{"port": 8080}]), region=region)
    return resp["authToken"]["X-aws-proxy-auth"]


def _call(endpoint: str, path: str, token: str, *, method="GET", body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"https://{endpoint}{path}", data=data, method=method)
    req.add_header("X-aws-proxy-auth", token)
    req.add_header("X-aws-proxy-port", "8080")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return r.status, r.read().decode()


def _wait_ready(vm: dict, region: str, timeout_s: float = 150) -> dict:
    deadline = time.time() + timeout_s
    t0 = time.time()
    while time.time() < deadline:
        try:
            tok = _token(vm["id"], region)
            status, _ = _call(vm["endpoint"], "/ping", tok, timeout=10)
            if status == 200:
                vm["ready_s"] = round(time.time() - t0, 1)
                vm["token"] = tok
                return vm
        except Exception:  # noqa: BLE001 — endpoint still warming
            pass
        time.sleep(5)
    vm["ready_s"] = None
    return vm


def _ask(vm: dict) -> dict:
    if not vm.get("token"):
        vm["answer"] = "(never became ready)"
        vm["chat_ms"] = None
        return vm
    t0 = time.time()
    try:
        _, body = _call(vm["endpoint"], "/chat", vm["token"], method="POST",
                        body={"text": QUESTION}, timeout=120)
        vm["answer"] = json.loads(body).get("response", "")[:120]
    except Exception as e:  # noqa: BLE001
        vm["answer"] = f"(error: {str(e)[:60]})"
    vm["chat_ms"] = round((time.time() - t0) * 1000)
    return vm


def main() -> int:
    ap = argparse.ArgumentParser(description="MicroVM fleet fan-out demo")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--region", default=DEFAULT_REGION)
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--keep", action="store_true", help="don't terminate the fleet")
    args = ap.parse_args()
    region = args.region
    n = max(1, min(args.count, 20))

    account = _account(region)
    image_arn = f"arn:aws:lambda:{region}:{account}:microvm-image:{args.name}"
    exec_arn = f"arn:aws:iam::{account}:role/{EXEC_ROLE_NAME}"
    version = _newest_ready_version(image_arn, region)
    print(f"fleet: {n} MicroVMs from {args.name} v{version} in {region}\n")

    print(f"[1/4] launching {n} MicroVMs (each gets its own private chDB) ...")
    with ThreadPoolExecutor(max_workers=n) as ex:
        fleet = list(ex.map(lambda i: _run_one(i, image_arn, version, exec_arn, region), range(n)))
    for vm in fleet:
        print(f"  #{vm['idx']} {vm['id']}")

    print("\n[2/4] waiting for all endpoints to accept traffic ...")
    with ThreadPoolExecutor(max_workers=n) as ex:
        fleet = list(ex.map(lambda vm: _wait_ready(vm, region), fleet))

    print("\n[3/4] firing the SAME question at all MicroVMs CONCURRENTLY ...")
    wall0 = time.time()
    with ThreadPoolExecutor(max_workers=n) as ex:
        fleet = list(ex.map(_ask, fleet))
    wall = round((time.time() - wall0) * 1000)

    print("\n[4/4] results\n")
    print(f"  {'#':<3}{'ready_s':<9}{'chat_ms':<9}answer")
    for vm in sorted(fleet, key=lambda v: v["idx"]):
        print(f"  {vm['idx']:<3}{str(vm.get('ready_s')):<9}{str(vm.get('chat_ms')):<9}{vm.get('answer','')}")
    ok = sum(1 for vm in fleet if vm.get("chat_ms"))
    print(f"\n  {ok}/{n} answered. Wall-clock for ALL {n} concurrent answers: {wall} ms")
    print("  No shared database — each MicroVM queried its own private chDB in parallel.")

    if not args.keep:
        print("\nterminating fleet ...")
        for vm in fleet:
            try:
                aws("lambda-microvms", "terminate-microvm", "--microvm-identifier", vm["id"], region=region)
            except Exception:  # noqa: BLE001
                pass
        print("  done.")
    else:
        print("\n--keep set; suspend/terminate later with the ids above.")
    return 0 if ok == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
