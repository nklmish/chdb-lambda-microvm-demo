"""scripts/bootstrap_prod_local.py — Idempotently provision this AWS account so
the app can run locally in *prod mode* (AgentCore Memory enabled).

What it ensures (all idempotent — safe to run repeatedly):
  1. The memory execution IAM role (NycTaxiAgentMemoryRole) exists, with the
     same trust + inline policy the CDK IamStack creates.
  2. An AgentCore Memory resource (nyc_taxi_analytical_memory) exists, with the
     3 long-term strategies, and is ACTIVE.
  3. Its id is stored in SSM (/agentcore/AGENTCORE_MEMORY_ID).

Resolution order for the memory id (prefer reuse, never duplicate):
  SSM param → list_memories match by name → create new.

Output contract: progress goes to STDERR; the resolved memory id is the ONLY
thing printed to STDOUT, so callers can capture it:

    MEM_ID="$(.venv/bin/python scripts/bootstrap_prod_local.py)"

Required caller permissions (admin covers all): iam:GetRole/CreateRole/PutRolePolicy,
bedrock-agentcore[-control]:*Memory*, ssm:GetParameter/PutParameter, sts:GetCallerIdentity.
"""
from __future__ import annotations

import json
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

from create_memory import MEMORY_NAME, create_memory_resource, memory_role_name

SSM_PARAM = "/agentcore/AGENTCORE_MEMORY_ID"
ACTIVE_TIMEOUT_S = 300


def log(msg: str) -> None:
    print(f"[bootstrap] {msg}", file=sys.stderr, flush=True)


def _trust_policy() -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }


def _memory_ops_policy(region: str, account: str) -> dict:
    mem_arn = f"arn:aws:bedrock-agentcore:{region}:{account}:memory/*"
    logs_arn = f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/memory/*"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:CreateMemory",
                    "bedrock-agentcore:GetMemory",
                    "bedrock-agentcore:UpdateMemory",
                    "bedrock-agentcore:DeleteMemory",
                    "bedrock-agentcore:ListMemories",
                ],
                "Resource": mem_arn,
            },
            {
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": logs_arn,
            },
        ],
    }


def ensure_role(region: str, account: str) -> str:
    """Return the memory execution role ARN, creating the role if absent.

    Role name is region-aware (IAM is account-global) — see memory_role_name().
    """
    role_name = memory_role_name(region)
    iam = boto3.client("iam")
    try:
        arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        log(f"memory execution role exists: {arn}")
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise

    log(f"creating memory execution role {role_name} ...")
    arn = iam.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=json.dumps(_trust_policy()),
        Description="AgentCore Memory execution role for the NYC Taxi agent",
    )["Role"]["Arn"]
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="MemoryOps",
        PolicyDocument=json.dumps(_memory_ops_policy(region, account)),
    )
    log(f"created role {arn}; waiting for IAM propagation ...")
    time.sleep(10)  # IAM is eventually consistent before the role is assumable
    return arn


def _get_ssm(ssm, name: str) -> str | None:
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return None
        raise


def _memory_status(ctl, memory_id: str) -> str | None:
    try:
        return ctl.get_memory(memoryId=memory_id)["memory"]["status"]
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ResourceNotFoundException", "ValidationException"):
            return None
        raise


def _find_existing_memory(ctl) -> str | None:
    """Return the id of an existing nyc_taxi_analytical_memory, if any.

    Memory ids are '<name>-<suffix>', so match on that prefix.
    """
    paginator_kwargs = {"maxResults": 100}
    token = None
    while True:
        if token:
            paginator_kwargs["nextToken"] = token
        resp = ctl.list_memories(**paginator_kwargs)
        for m in resp.get("memories", []):
            mid = m.get("id") or m.get("memoryId", "")
            if mid.startswith(f"{MEMORY_NAME}-") or mid == MEMORY_NAME:
                return mid
        token = resp.get("nextToken")
        if not token:
            return None


def ensure_memory(region: str, role_arn: str) -> str:
    """Return an ACTIVE memory id, reusing SSM / existing resource before creating."""
    ctl = boto3.client("bedrock-agentcore-control", region_name=region)
    ssm = boto3.client("ssm", region_name=region)

    # 1) SSM is the source of truth — reuse if it points at a live memory.
    ssm_id = _get_ssm(ssm, SSM_PARAM)
    if ssm_id and _memory_status(ctl, ssm_id):
        log(f"reusing memory from SSM {SSM_PARAM}: {ssm_id}")
        return ssm_id
    if ssm_id:
        log(f"SSM memory id {ssm_id} is stale (resource gone); re-provisioning")

    # 2) An untracked memory may already exist (SSM cleared) — adopt it.
    found = _find_existing_memory(ctl)
    if found:
        log(f"found existing memory {found}; recording to SSM")
        memory_id = found
    else:
        # 3) Create a fresh one.
        log("no existing memory — creating AgentCore Memory (3 strategies) ...")
        memory_id = create_memory_resource(region, role_arn)
        log(f"created memory {memory_id}")

    ssm.put_parameter(Name=SSM_PARAM, Value=memory_id, Type="String", Overwrite=True)
    return memory_id


def wait_active(region: str, memory_id: str) -> None:
    """Poll until the memory reports ACTIVE (strategies finish provisioning)."""
    ctl = boto3.client("bedrock-agentcore-control", region_name=region)
    start = time.time()
    while True:
        status = _memory_status(ctl, memory_id)
        if status == "ACTIVE":
            log(f"memory {memory_id} is ACTIVE")
            return
        if status in ("FAILED", None):
            raise RuntimeError(f"memory {memory_id} status={status} — cannot continue")
        if time.time() - start > ACTIVE_TIMEOUT_S:
            raise TimeoutError(
                f"memory {memory_id} still {status} after {ACTIVE_TIMEOUT_S}s"
            )
        log(f"memory status={status}; waiting ...")
        time.sleep(10)


def main() -> int:
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    account = boto3.client("sts").get_caller_identity()["Account"]
    log(f"account={account} region={region}")
    role_arn = ensure_role(region, account)
    memory_id = ensure_memory(region, role_arn)
    wait_active(region, memory_id)
    # STDOUT contract: the memory id, and nothing else.
    print(memory_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
