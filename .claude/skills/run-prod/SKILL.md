---
name: run-prod
description: Run the NYC Taxi Analytics Agent locally in PROD MODE — provision AgentCore Memory in the current AWS account (idempotent), then launch the FastAPI app with IS_PROD=true and memory enabled, and drive a /chat that proves memory retrieval + write. Use when asked to "run in prod", "run prod mode", or start the app with AgentCore Memory.
---

# Run the agent locally in PROD MODE (AgentCore Memory enabled)

This launches the **local** app exactly as it runs in production behavior:
`IS_PROD=true`, `LANGFUSE_TRACING_ENVIRONMENT=PRD`, and AgentCore **Memory**
wired in — but against **the current AWS account's own** memory resource, which
this skill provisions. It does NOT deploy the AgentCore Runtime or VPC/S3 Files
(that's the README "Initial AWS setup" + CDK path).

> For a memory-less quick check, use the **run-local** skill instead. This skill
> is the heavier path: it creates real AWS resources (an IAM role + an AgentCore
> Memory) in the caller's account on first run.

## Prerequisites (the part that can't be automated)

```bash
.venv/bin/python -c "import fastapi, uvicorn, chdb, strands, bedrock_agentcore; print('deps OK', chdb.__version__)"
aws sts get-caller-identity        # valid identity required
```

- Targets **chDB ≥ 4.1.8** (CloudFront 403 fix for the live `query_with_fresh_data`
  delta path). Upgrade with `.venv/bin/pip install -U 'chdb>=4.1.8'` if older.

- **Bedrock model access** to `us.anthropic.claude-sonnet-4-20250514-v1:0` in
  **us-east-1, us-east-2, us-west-2** (cross-region inference profile).
- **IAM permissions on the caller** (admin covers all): `iam:GetRole/CreateRole/PutRolePolicy`,
  `bedrock-agentcore[-control]:*Memory*` + data-plane (`CreateEvent`,
  `RetrieveMemoryRecords`, `GetMemory`, `ListMemoryRecords`, `ListEvents`),
  `ssm:GetParameter/PutParameter`, `sts:GetCallerIdentity`.
- If `aws sts get-caller-identity` fails or Bedrock access is missing, STOP and
  tell the user — the agent cannot answer `/chat` without it.

## Step 1 — Provision AgentCore Memory (idempotent)

`scripts/bootstrap_prod_local.py` ensures the memory execution role + the
AgentCore Memory resource exist and are ACTIVE, records the id in SSM, and prints
the id to stdout (progress goes to stderr). Safe to re-run — it reuses existing
resources and never duplicates.

```bash
MEM_ID="$(AWS_REGION=us-east-1 .venv/bin/python scripts/bootstrap_prod_local.py)"
echo "memory id: $MEM_ID"
```

First run in a fresh account creates the role + memory and waits ~1–2 min for
the 3 strategies to become ACTIVE. Subsequent runs return instantly.

## Step 2 — Bake a local chDB sample + profile

Identical to the **run-local** skill, Step 1 — run that bake + `data_profile.json`
generation now if `local_chdb_data/` or `data_profile.json` is missing. (One
month, ~3M rows, ~2s. Use the full README bake for representative data.)

Remember the chDB rule from run-local: stop any running server before baking —
only one process may hold `local_chdb_data` at a time.

## Step 3 — Launch in PROD MODE with memory wired

```bash
CHDB_DATA_PATH="$PWD/local_chdb_data" \
AWS_REGION=us-east-1 \
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0 \
IS_PROD=true \
LANGFUSE_TRACING_ENVIRONMENT=PRD \
AGENTCORE_MEMORY_ID="$MEM_ID" \
DISABLE_ADOT_OBSERVABILITY=true \
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8080 --workers 1
```

(Run in the background and tee logs to a file so you can read the memory lines.)

What prod mode changes vs run-local:
- `IS_PROD=true` → the localhost CORS middleware is dropped. The same-origin UI
  at `http://localhost:8080` still works; a cross-origin frontend (e.g. `:3000`)
  would be blocked.
- `AGENTCORE_MEMORY_ID` set → the agent builds an `AgentCoreMemorySessionManager`
  and reads/writes the memory store on every turn.
- `LANGFUSE_TRACING_ENVIRONMENT=PRD` → spans export to an OTLP endpoint. With no
  collector running you'll see harmless `localhost:4318 Connection refused` retry
  warnings; set `OTEL_EXPORTER_OTLP_ENDPOINT` + Langfuse creds to silence them.

## Step 4 — Drive it and PROVE memory is used

```bash
curl -s http://127.0.0.1:8080/health      # {"status":"healthy","row_count":...}
curl -s -X POST http://127.0.0.1:8080/chat -H 'Content-Type: application/json' \
  -d '{"text":"What is the busiest hour of the day? One sentence."}' --max-time 120
```

Then confirm memory in the server log — these lines are the proof:

```
bedrock_agentcore.memory.client Initialized MemoryClient ...
bedrock_agentcore.memory.client Retrieved N memories from namespace: taxi-analytics/...
bedrock_agentcore.memory.integrations.strands... Retrieved M customer context items
bedrock_agentcore.memory.client Created event: ...     <- conversation written back
```

## Success criteria

- `bootstrap_prod_local.py` exits 0 and prints a memory id.
- Server log shows `MemoryClient` initialized, context **retrieved**, and an
  event **created** (written) during the `/chat`.
- `/chat` returns a data-grounded answer.

## Caveats

- **Real, persistent AWS resources** are created on first run (IAM role +
  AgentCore Memory). They incur cost and remain until deleted. Re-runs reuse them.
- **Chat traffic is written into this account's memory store** (under actor
  `anonymous` for `/chat`, `agentcore` for `/invocations`). That's the intended
  prod behavior — there is no separate "test" memory.
- Data is still the local sample unless the full bake was run — prod *mode*
  (memory) is independent of prod *data volume*.
- This is NOT the deployed AgentCore Runtime. The agent runs in local uvicorn;
  only the Memory backend is real cloud infrastructure.
