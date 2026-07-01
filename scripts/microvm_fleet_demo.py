#!/usr/bin/env python3
"""Fleet fan-out: N Lambda MicroVMs, each with its own private chDB, in parallel.

The blog's claim — *"There's no shared database to scale, and nothing for a
thousand agents to overload at once"* — made visible: launch N MicroVMs from the
same image, each carrying its own private, snapshot-hot chDB engine, then fire the
same analytical question at all of them at once. They answer concurrently with no
shared backend to contend on. Because every VM baked chDB from the *same* image,
all N return the *same* number — a live snapshot-fidelity (consensus) check.

Account-agnostic and self-contained (shells out to the AWS CLI via fleet_core).
Uses the image built by scripts/deploy_microvm.py. Terminates the fleet at the
end unless --keep.

For a live browser view of the same fan-out, see scripts/fleet_console.py.

Usage:
  python scripts/microvm_fleet_demo.py --count 5 --region us-west-2
  python scripts/microvm_fleet_demo.py --count 3 --keep        # leave them running
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet_core as fc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="MicroVM fleet fan-out demo")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--region", default=fc.DEFAULT_REGION)
    ap.add_argument("--name", default=fc.DEFAULT_NAME)
    ap.add_argument("--question", default=fc.DEFAULT_QUESTION)
    ap.add_argument("--keep", action="store_true", help="don't terminate the fleet")
    args = ap.parse_args()

    n = fc.clamp_count(args.count)
    vms: dict[int, dict] = {}

    def on_event(ev: dict) -> None:
        t = ev.get("type")
        if t == "preflight":
            print(f"fleet: {ev['n']} MicroVMs from {args.name} "
                  f"v{ev['version']} in {ev['region']}\n")
            print(f"[1/4] launching {ev['n']} MicroVMs (each gets its own private chDB) ...")
        elif t == "launch":
            for vm in ev["vms"]:
                vms[vm["idx"]] = vm
                print(f"  #{vm['idx']} {vm['id']}")
            print("\n[2/4] waiting for all endpoints to accept traffic ...")
        elif t == "ready":
            vms.setdefault(ev["idx"], {})["ready_s"] = ev["ready_s"]
        elif t == "answer":
            vm = vms.setdefault(ev["idx"], {})
            vm["chat_ms"] = ev["chat_ms"]
            vm["answer"] = ev["answer"]
        elif t == "done":
            print("\n[3/4] fired the SAME question at all MicroVMs CONCURRENTLY")
            print("\n[4/4] results\n")
            print(f"  {'#':<3}{'ready_s':<9}{'chat_ms':<9}answer")
            for idx in sorted(vms):
                vm = vms[idx]
                print(f"  {idx:<3}{str(vm.get('ready_s')):<9}"
                      f"{str(vm.get('chat_ms')):<9}{vm.get('answer', '')}")
            c = ev["consensus"]
            agree = "AGREE" if c["agree"] else f"DIVERGE {c['groups']}"
            print(f"\n  {ev['ok']}/{ev['total']} answered. "
                  f"Wall-clock for ALL {ev['total']} concurrent answers: {ev['wall_ms']} ms")
            print(f"  consensus: {c['answered']}/{ev['total']} produced a number, "
                  f"{c['majority']}/{c['answered']} identical → {agree}")
            print("  No shared database — each MicroVM queried its own private chDB in parallel.")
        elif t == "terminated":
            print("\nterminated fleet.")
        elif t == "error":
            print(f"\nERROR: {ev['message']}", file=sys.stderr)

    summary = fc.run_fleet_blocking(
        n, args.region, args.name, args.question, on_event=on_event, keep=args.keep)

    if args.keep and vms:
        print("\n--keep set; suspend/terminate later with the ids above.")
    if summary.get("type") == "error":
        return 1
    return 0 if summary.get("ok") == summary.get("total") else 1


if __name__ == "__main__":
    raise SystemExit(main())
