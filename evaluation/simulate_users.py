"""evaluation/simulate_users.py — User traffic simulator (Phase 3 flywheel).

Reads evaluation/load_config.json, prints the simulation plan, exits zero.
Full load-sim execution (N concurrent simulated users, adversarial prompts,
rate-limit probing, latency percentile aggregation) deferred to a future
milestone per Phase A three-phase flywheel scope.

-compliant: invoked as python3 evaluation/simulate_users.py.

Usage:
    python3 evaluation/simulate_users.py
    python3 evaluation/simulate_users.py --config evaluation/load_config.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="User simulation planner.")
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "evaluation" / "load_config.json"),
        help="Path to load config JSON (default: evaluation/load_config.json).",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"FAIL: config not found at {config_path}", file=sys.stderr)
        return 1

    config = json.loads(config_path.read_text())
    concurrency = config.get("concurrent_users", 0)
    duration_s = config.get("duration_seconds", 0)
    prompts = config.get("prompts", [])
    adversarial = [p for p in prompts if p.get("adversarial")]
    benign = [p for p in prompts if not p.get("adversarial")]
    target = config.get("target", "<unset>")

    print(f"User simulation plan — target: {target}")
    print(f"Config: {config_path}")
    print(f"Concurrent users: {concurrency}")
    print(f"Duration: {duration_s}s")
    print(f"Prompts: {len(prompts)} total ({len(benign)} benign, {len(adversarial)} adversarial)")
    for idx, p in enumerate(prompts):
        tag = "[ADV]" if p.get("adversarial") else "[   ]"
        text = p.get("text", "")
        print(f"  {tag} [{idx + 1:02d}] {text[:80]}{'...' if len(text) > 80 else ''}")

    estimated_invocations = concurrency * max(1, duration_s // 10) if duration_s > 0 else 0
    print()
    print(f"Estimated total invocations (concurrency × duration/10s): ~{estimated_invocations}")
    print()
    print(" scope: planner prints the simulation matrix only. Full load-sim "
          "execution (concurrent users, adversarial probing, latency aggregation) "
          "is deferred to a future milestone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
