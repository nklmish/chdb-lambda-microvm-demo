"""evaluation/deploy.py — Deploy an agent variant with Langfuse OTEL env vars.

Thin wrapper around scripts/create_runtime.py for experimenters (HPO, ad-hoc
evaluation runs). Delegates to the canonical SDK-direct runtime creation path
per — does NOT use the @aws/agentcore CLI.

Usage:
    python3 evaluation/deploy.py --environment TST
    python3 evaluation/deploy.py --environment TST --agent-name-suffix exp1

The agent's observability env-vars (DISABLE_ADOT_OBSERVABILITY, OTEL_EXPORTER_*,
AGENTCORE_MEMORY_ID, etc.) are sourced from scripts/create_runtime.py per the
 canonical recipe; this wrapper does not override them.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy an agent variant for experimentation/evaluation."
    )
    parser.add_argument(
        "--environment",
        default="TST",
        choices=["DEV", "TST", "PRD"],
        help="Target environment (default: TST).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve inputs + print the redacted payload; exit before CreateAgentRuntime.",
    )
    args = parser.parse_args()

    create_runtime = REPO_ROOT / "scripts" / "create_runtime.py"
    if not create_runtime.exists():
        print(f"FAIL: {create_runtime} not found", file=sys.stderr)
        return 1

    cmd = ["python3", str(create_runtime), "--environment", args.environment]
    if args.dry_run:
        cmd.append("--dry-run")

    print(f"Delegating to: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
