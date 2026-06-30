"""cicd/get_runtime_arn.py — Resolve (and optionally delete) AgentCore Runtime by name.

Replaces `aws bedrock-agentcore-control list-agent-runtimes`/`delete-agent-runtime`
CLI calls in deploy_agent.sh + delete_agent.sh.
Host AWS CLI versions <2.22.0 lack bedrock-agentcore-control service
registration (service added in awscli 2.22.0); boto3 has it via bundled
service models, so this helper is robust to awscli version drift on both
CI runners and local dev hosts.

Usage:
    # Resolve ARN (default)
    python3 cicd/get_runtime_arn.py --agent-name nyc_taxi_agent_TST
    # Resolve ID or both
    python3 cicd/get_runtime_arn.py --agent-name nyc_taxi_agent_TST --output id
    python3 cicd/get_runtime_arn.py --agent-name nyc_taxi_agent_TST --output both
    # Destructive: delete by id (used by cicd/delete_agent.sh)
    python3 cicd/get_runtime_arn.py --agent-name nyc_taxi_agent_TST --delete-id <id>

Exit codes:
    0 — success (printed requested field, or delete succeeded)
    1 — not found (resolve) or delete failed
    2 — usage error (bad args)
"""
from __future__ import annotations

import argparse
import os
import sys

import boto3


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve AgentCore Runtime ARN/ID, or delete by id.")
    ap.add_argument("--agent-name", required=True, help="Runtime name, e.g. nyc_taxi_agent_TST.")
    ap.add_argument("--output", default="arn", choices=["arn", "id", "both"],
                    help="Which field(s) to print on resolve (default: arn). Ignored when --delete-id is set.")
    ap.add_argument("--delete-id", default=None,
                    help="If set, delete the runtime with this agent-runtime-id (destructive).")
    args = ap.parse_args()

    # Spec-amendment: see stamp
    client = boto3.client(
        "bedrock-agentcore-control",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )

    if args.delete_id:
        # Destructive path — invoked by cicd/delete_agent.sh
        try:
            client.delete_agent_runtime(agentRuntimeId=args.delete_id)
            print(f"OK: deleted runtime id={args.delete_id} (name={args.agent_name})")
            return 0
        except Exception as e:
            print(f"ERROR: delete failed for id={args.delete_id}: {type(e).__name__}: {e}",
                  file=sys.stderr)
            return 1

    # Resolve path
    resp = client.list_agent_runtimes(maxResults=100)
    match = None
    for summary in resp.get("agentRuntimes", []):
        if summary.get("agentRuntimeName") == args.agent_name:
            match = summary
            break

    if match is None:
        print(f"ERROR: no runtime found with name {args.agent_name!r}", file=sys.stderr)
        return 1

    arn = match.get("agentRuntimeArn", "")
    rid = match.get("agentRuntimeId", "")

    if args.output == "arn":
        print(arn)
    elif args.output == "id":
        print(rid)
    else:  # both
        print(f"{arn}\t{rid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
