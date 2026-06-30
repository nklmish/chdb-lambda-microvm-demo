# scripts/create_memory.py — Provision the AgentCore Memory resource (run once).
#
# Uses MemoryClient from the AgentCore SDK, which wraps both control-plane
# (bedrock-agentcore-control) and data-plane (bedrock-agentcore) boto3 clients.
# Accepts snake_case parameters and handles response normalization internally.
#
# Importable: STRATEGIES and create_memory_resource() are reused by
# scripts/bootstrap_prod_local.py. Running this module directly creates the
# memory using the NycTaxiAgentMemoryRole in the *current* AWS account — the
# role ARN is derived from the caller's account (or MEMORY_EXECUTION_ROLE_ARN),
# never hard-coded, so this works in any account that has the role (created by
# the CDK IamStack or by bootstrap_prod_local.py).
from __future__ import annotations

import os

from bedrock_agentcore.memory import MemoryClient

MEMORY_NAME = "nyc_taxi_analytical_memory"
MEMORY_DESCRIPTION = "Persistent analytical knowledge for NYC Taxi Analytics Agent"
EVENT_EXPIRY_DAYS = 90  # Integer — days (valid range: 3-365)


def memory_role_name(region: str) -> str:
    """Region-aware memory execution role name.

    IAM roles are account-global, so the role is region-suffixed everywhere except
    us-east-1 (which keeps the original name for backward compatibility). Must match
    cdk/stacks/iam_stack.py and scripts/bootstrap_prod_local.py.
    """
    return "NycTaxiAgentMemoryRole" if region == "us-east-1" else f"NycTaxiAgentMemoryRole-{region}"

# Three long-term memory strategies. Kept as a module constant so the bootstrap
# script provisions an identical resource to the canonical CDK/manual path.
STRATEGIES = [
    {
        "semanticMemoryStrategy": {
            "name": "analytical_discoveries",
            "description": "Facts discovered through taxi data analysis",
            "namespaces": ["taxi-analytics"],
            "namespaceTemplates": ["taxi-analytics/{actorId}"],
        }
    },
    {
        "userPreferenceMemoryStrategy": {
            "name": "analyst_preferences",
            "description": "How each user prefers to analyze taxi data",
            "namespaces": ["taxi-analytics"],
            "namespaceTemplates": ["taxi-analytics/{actorId}/preferences"],
        }
    },
    {
        "episodicMemoryStrategy": {
            "name": "analysis_episodes",
            "description": "Records of past analysis sessions with pattern synthesis",
            "namespaces": ["taxi-analytics"],
            "namespaceTemplates": ["taxi-analytics/insights/{actorId}/episodes"],
            "reflectionConfiguration": {
                "namespaces": ["taxi-analytics/insights"],
            },
        }
    },
]


def create_memory_resource(region: str, memory_execution_role_arn: str) -> str:
    """Create the AgentCore Memory resource and return its id."""
    client = MemoryClient(region_name=region)
    response = client.create_memory(
        name=MEMORY_NAME,
        description=MEMORY_DESCRIPTION,
        memory_execution_role_arn=memory_execution_role_arn,
        event_expiry_days=EVENT_EXPIRY_DAYS,
        strategies=STRATEGIES,
    )
    # SDK normalizes response: returns memory dict directly with both "id" and "memoryId"
    return response["id"]


def default_memory_role_arn() -> str:
    """Resolve the memory execution role ARN for the current account.

    Uses env var MEMORY_EXECUTION_ROLE_ARN if set (e.g. a non-default role name);
    otherwise looks up NycTaxiAgentMemoryRole by name, which requires the role to
    already exist (CDK IamStack or bootstrap_prod_local.py creates it).
    """
    env_arn = os.getenv("MEMORY_EXECUTION_ROLE_ARN")
    if env_arn:
        return env_arn
    import boto3

    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    iam = boto3.client("iam")
    return iam.get_role(RoleName=memory_role_name(region))["Role"]["Arn"]


if __name__ == "__main__":
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    role_arn = default_memory_role_arn()
    memory_id = create_memory_resource(region, role_arn)
    print(f"Created AgentCore Memory: {memory_id}")
    # Save this ID — it goes into SSM (/agentcore/AGENTCORE_MEMORY_ID) and the
    # app's AGENTCORE_MEMORY_ID env var. bootstrap_prod_local.py automates this.
