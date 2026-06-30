"""evaluation/hpo.py — HPO variant planner (Phase 1 of the continuous-eval flywheel).

Reads evaluation/hpo_config.json, prints the variant plan, exits zero. Full
HPO execution (deploy N agent variants → run Langfuse dataset experiments →
score with remote evaluators → compare → select best config) deferred to a
future milestone per Phase A three-phase flywheel scope.

-compliant: invoked as python3 evaluation/hpo.py.

Usage:
    python3 evaluation/hpo.py
    python3 evaluation/hpo.py --config evaluation/hpo_config.json
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="HPO variant planner.")
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "evaluation" / "hpo_config.json"),
        help="Path to HPO config JSON (default: evaluation/hpo_config.json).",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"FAIL: config not found at {config_path}", file=sys.stderr)
        return 1

    config = json.loads(config_path.read_text())
    variants = config.get("variants", {})
    dataset_name = config.get("dataset_name", "<unset>")

    if not variants:
        print("WARN: no variants defined in config; nothing to plan.")
        return 0

    # Cartesian product of all variant axes
    axes = list(variants.keys())
    value_lists = [variants[axis] for axis in axes]
    combinations = list(itertools.product(*value_lists))

    print(f"HPO variant plan — dataset: {dataset_name}")
    print(f"Config: {config_path}")
    print(f"Axes ({len(axes)}): {', '.join(axes)}")
    for axis, values in variants.items():
        print(f"  {axis}: {values} ({len(values)} options)")
    print(f"Total variant combinations: {len(combinations)}")
    print()
    print("Would run the following trials:")
    for idx, combo in enumerate(combinations):
        pairs = ", ".join(f"{a}={v!r}" for a, v in zip(axes, combo))
        print(f"  [trial {idx + 1:02d}] {pairs}")

    print()
    print(" scope: planner prints the variant matrix only. Full HPO execution "
          "(variant deploy → Langfuse experiment → cleanup) is deferred to a "
          "future milestone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
