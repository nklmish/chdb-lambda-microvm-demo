#!/usr/bin/env bash
# cicd/delete_agent.sh — Delete a deployed test agent after CI/CD evaluation
#
# Earlier versions used `python3 -c "import json; ..."` for
# JSON reads. Substituted with `jq` per driver approval (behavior preserved,
# syntax substituted per). See ledger note D-D15-SPEC-PYTHON-DASH-C-VIOLATES-.
#
# awscli<2.22.0 lacks bedrock-agentcore-control service;
# delete-agent-runtime call is a destructive op — safer to invoke via boto3
# for parity with deploy_agent.sh + future-proofing against CLI drift.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO_ROOT/.venv/bin/python3"

AGENT_ID=$(jq -r '.tst.agent_id' cicd/hp_config.json)
AGENT_NAME=$(jq -r '.tst.agent_name' cicd/hp_config.json)

# boto3-direct DeleteAgentRuntime (awscli<2.22.0 lacks
# bedrock-agentcore-control). Inline the call via the named helper which
# accepts --delete-id to perform the destructive op. Falls back to failure
# if the runtime doesn't exist (boto3 raises ResourceNotFoundException).
"$PY" cicd/get_runtime_arn.py --agent-name "$AGENT_NAME" --delete-id "$AGENT_ID" \
  || { echo "ERROR: DeleteAgentRuntime failed for ${AGENT_NAME} (${AGENT_ID})" >&2; exit 1; }

echo "Deleted agent: ${AGENT_NAME} (${AGENT_ID})"
