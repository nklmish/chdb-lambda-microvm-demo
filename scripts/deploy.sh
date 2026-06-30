#!/usr/bin/env bash
# scripts/deploy.sh — Deploy NYC Taxi Agent to AgentCore Runtime (legacy @aws/agentcore CLI path).
# NOTE: scripts/create_runtime.py (boto3-direct) is the canonical/recommended deploy path and
#       needs no @aws/agentcore CLI. This script is kept as the CLI alternative; it is account-
#       agnostic — region comes from AWS_DEFAULT_REGION and the account is read from STS.
# Prerequisites: npm install -g @aws/agentcore (Node.js 20+), AWS credentials configured,
#                jq installed (used to merge secrets into the rendered agentcore.json)
set -euo pipefail
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
REGION="$AWS_DEFAULT_REGION"

ENVIRONMENT="${1:-PRD}"
TARGET="${2:-default}"   # maps to an entry in agentcore/aws-targets.json
AGENT_NAME="nyc_taxi_agent_${ENVIRONMENT}"

# Fetch Langfuse credentials from SSM
LANGFUSE_HOST=$(aws ssm get-parameter --region "$REGION" --name "/langfuse/LANGFUSE_HOST" --query "Parameter.Value" --output text)
LANGFUSE_PK=$(aws ssm get-parameter --region "$REGION" --name "/langfuse/LANGFUSE_PUBLIC_KEY" --query "Parameter.Value" --output text)
LANGFUSE_SK=$(aws ssm get-parameter --region "$REGION" --name "/langfuse/LANGFUSE_SECRET_KEY" --with-decryption --query "Parameter.Value" --output text)
AGENTCORE_MEMORY_ID=$(aws ssm get-parameter --region "$REGION" --name "/agentcore/AGENTCORE_MEMORY_ID" --query "Parameter.Value" --output text)

# Build OTEL auth header for Langfuse
OTEL_ENDPOINT="${LANGFUSE_HOST}/api/public/otel"
AUTH_TOKEN=$(printf '%s' "${LANGFUSE_PK}:${LANGFUSE_SK}" | base64 | tr -d '\n')
OTEL_HEADERS="Authorization=Basic ${AUTH_TOKEN},x-langfuse-ingestion-version=4"
IS_PROD=$([ "$ENVIRONMENT" = "PRD" ] && echo "true" || echo "false")

# Render into a private staging dir so the rendered file never sits next to the
# checked-in non-secret one and never leaks into the build context.
STAGE="$(mktemp -d)"
trap 'find "$STAGE" -type f -exec shred -u {} + 2>/dev/null; rm -rf "$STAGE"' EXIT
cp agentcore/agentcore.json "$STAGE/agentcore.json"
# aws-targets.json is also read from the same directory by `agentcore deploy`.
# Rewrite the target's account/region from the live credentials so the checked-in
# placeholder account never pins the deploy to someone else's account.
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
jq --arg acct "$ACCOUNT" --arg region "$REGION" \
   '(.[].account) = $acct | (.[].region) = $region' \
   agentcore/aws-targets.json > "$STAGE/aws-targets.json"

# Merge runtime env vars into runtimes[0].
# Top-level key is `runtimes` (NOT `agents`) and `envVars` is an array of
# {name, value} per the @aws/agentcore JSON schema.
jq --arg name          "$AGENT_NAME" \
   --arg env           "$ENVIRONMENT" \
   --arg otel_endpoint "$OTEL_ENDPOINT" \
   --arg otel_headers  "$OTEL_HEADERS" \
   --arg is_prod       "$IS_PROD" \
   --arg memory_id     "$AGENTCORE_MEMORY_ID" \
   --arg region        "$REGION" \
'
  .runtimes[0].name = $name
  | .runtimes[0].instrumentation = { "enableOtel": false }
  | .runtimes[0].envVars = [
      {name:"BEDROCK_MODEL_ID",            value:"us.anthropic.claude-sonnet-4-20250514-v1:0"},
      {name:"AWS_REGION",                  value:$region},
      {name:"CHDB_DATA_PATH",              value:"/app/local_chdb_data"},
      {name:"WEATHER_MOUNT_PATH",          value:"/mnt/noaa-gsod"},
      {name:"IS_PROD",                     value:$is_prod},
      {name:"LANGFUSE_TRACING_ENVIRONMENT",value:$env},
      {name:"DISABLE_ADOT_OBSERVABILITY",  value:"true"},
      {name:"OTEL_EXPORTER_OTLP_ENDPOINT", value:$otel_endpoint},
      {name:"OTEL_EXPORTER_OTLP_HEADERS",  value:$otel_headers},
      {name:"AGENTCORE_MEMORY_ID",         value:$memory_id}
    ]
' "$STAGE/agentcore.json" > "$STAGE/agentcore.json.tmp"
mv "$STAGE/agentcore.json.tmp" "$STAGE/agentcore.json"

# DEV/TST: append Langfuse SDK env vars for distributed tracing (langfuse.get_client()).
# PRD uses OTEL-only tracing and does NOT need these.
if [ "$ENVIRONMENT" != "PRD" ]; then
  jq --arg pk "$LANGFUSE_PK" --arg sk "$LANGFUSE_SK" --arg host "$LANGFUSE_HOST" '
    .runtimes[0].envVars += [
      {name:"LANGFUSE_PUBLIC_KEY", value:$pk},
      {name:"LANGFUSE_SECRET_KEY", value:$sk},
      {name:"LANGFUSE_HOST",       value:$host}
    ]
  ' "$STAGE/agentcore.json" > "$STAGE/agentcore.json.tmp"
  mv "$STAGE/agentcore.json.tmp" "$STAGE/agentcore.json"
fi

# Validate the rendered config. `agentcore validate -d <dir>` reads agentcore.json
# from the given directory — there is no --config flag.
agentcore validate -d "$STAGE"

# `agentcore deploy` has no --config/--directory flag — it reads agentcore.json
# from the current working directory. Cd into the staging dir for the deploy call.
# Flags available: --target, -y/--yes, -v/--verbose, --dry-run, --diff, --json.
(
  cd "$STAGE"
  agentcore deploy --target "$TARGET" --yes --verbose
)

# trap above shreds the rendered agentcore.json and removes $STAGE
