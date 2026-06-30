#!/usr/bin/env bash
# cicd/deploy_agent.sh — Deploy agent to AgentCore for CI/CD evaluation.
# Uses boto3-direct runtime registration via scripts/create_runtime.py
#. Does NOT use the @aws/agentcore CLI.
#
# Earlier versions used `python3 -c "import json; ..."` heredoc
# for in-place hp_config.json mutation. Substituted with `jq` per driver
# approval. See ledger note
# D-D15-SPEC-PYTHON-DASH-C-VIOLATES-.
#
# awscli<2.22.0 lacks bedrock-agentcore-control service;
# resolve via boto3 helper cicd/get_runtime_arn.py. Also pins python3 to
# .venv/bin/python3 to defeat CDK-venv PATH bleed per D-D15-VENV-PYTHON313.
set -euo pipefail
export AWS_DEFAULT_REGION=us-east-1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO_ROOT/.venv/bin/python3"

ENVIRONMENT="${1:-TST}"
AGENT_NAME="nyc_taxi_agent_${ENVIRONMENT}"
ENV_LOWER=$(echo "$ENVIRONMENT" | tr '[:upper:]' '[:lower:]')

# Delegate runtime creation to scripts/create_runtime.py (boto3-direct)
"$PY" scripts/create_runtime.py --environment "$ENVIRONMENT"

# boto3 helper (awscli<2.22.0 lacks bedrock-agentcore-control).
# Explicit || exit pattern defeats bash set -e gotcha inside $() assignments.
AGENT_ARN="$("$PY" cicd/get_runtime_arn.py --agent-name "$AGENT_NAME" --output arn)" \
  || { echo "ERROR: ARN resolution failed for ${AGENT_NAME}" >&2; exit 1; }
AGENT_ID="$("$PY" cicd/get_runtime_arn.py --agent-name "$AGENT_NAME" --output id)" \
  || { echo "ERROR: ID resolution failed for ${AGENT_NAME}" >&2; exit 1; }

# Save config for tst.py and delete_agent.sh
TMP_FILE=$(mktemp)
jq --arg key "$ENV_LOWER" \
   --arg arn "$AGENT_ARN" \
   --arg name "$AGENT_NAME" \
   --arg id "$AGENT_ID" \
   --arg env "$ENVIRONMENT" \
   '.[$key] = {agent_arn: $arn, agent_name: $name, agent_id: $id, environment: $env}' \
   cicd/hp_config.json > "$TMP_FILE" && mv "$TMP_FILE" cicd/hp_config.json

echo "Deployed: ${AGENT_ARN}"
