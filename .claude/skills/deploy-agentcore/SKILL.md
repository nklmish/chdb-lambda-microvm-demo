---
name: deploy-agentcore
description: Deploy the NYC Taxi agent to AWS AgentCore Runtime in the current account — provision full CDK infra (VPC, ECR, IAM, S3 Files, Monitoring, CICD), AgentCore Memory, build+push the image, and register/poll the runtime. Use when asked to "deploy to agentcore", "run on agentcore runtime", or deploy the agent to AWS.
---

# Deploy the agent to AgentCore Runtime (any AWS account)

This provisions the agent as a managed **AgentCore Runtime** plus all supporting
infra, in whatever account the credentials point at. It is the heavy path: it
creates real, billable AWS resources. Region defaults to **us-east-1** (AgentCore
Runtime, Memory, and the `us.` cross-region inference profile are US-region).

> **Status: validated end-to-end.** This whole sequence was run as a fresh deploy
> in a clean region (us-west-2) and torn down. The gotchas it surfaced are fixed in
> code and captured in the Troubleshooting table below — read that table first if
> anything errors; most failures are already-known with a one-line fix.

> **Weather note:** AgentCore Runtime cannot mount the S3 Files filesystem (its
> API has no external-filesystem mount). On the runtime, weather uses the direct
> public-S3 fallback. The genuine S3 Files *mount* demo runs on EC2 — see the
> **deploy-mount-demo** skill (its stack `NycTaxiMountDemo` is deployed here too).

## Phase 0 — Preflight (STOP on any failure; do not start a partial deploy)

```bash
aws sts get-caller-identity                       # creds live; note the account
cdk --version && finch --version && jq --version  # tooling (docker ok instead of finch)
```
- **Bedrock access (real check, not assumed):** confirm the model is invocable —
  ```bash
  aws bedrock-runtime converse --region us-east-1 \
    --model-id us.anthropic.claude-sonnet-4-20250514-v1:0 \
    --messages '[{"role":"user","content":[{"text":"hi"}]}]' \
    --inference-config '{"maxTokens":1}' >/dev/null && echo "bedrock OK"
  ```
  If this AccessDenies, STOP — enable model access in Bedrock console for
  us-east-1/us-east-2/us-west-2 before deploying.
- **Langfuse (REQUIRED):** verify the 3 SSM params exist; if missing, STOP and have
  the user create them (free Langfuse Cloud tier):
  ```bash
  aws ssm get-parameters --region us-east-1 \
    --names /langfuse/LANGFUSE_HOST /langfuse/LANGFUSE_PUBLIC_KEY /langfuse/LANGFUSE_SECRET_KEY \
    --with-decryption --query '{Found:Parameters[].Name,Missing:InvalidParameters}'
  # If any missing:
  #   aws ssm put-parameter --name /langfuse/LANGFUSE_HOST       --type String       --value https://us.cloud.langfuse.com --region us-east-1
  #   aws ssm put-parameter --name /langfuse/LANGFUSE_PUBLIC_KEY --type String       --value pk-lf-... --region us-east-1
  #   aws ssm put-parameter --name /langfuse/LANGFUSE_SECRET_KEY --type SecureString --value sk-lf-... --region us-east-1
  ```

## Phase 1 — CDK infra (all 6 stacks, in dependency order)

```bash
cd cdk
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cdk bootstrap aws://$(aws sts get-caller-identity --query Account --output text)/us-east-1
cdk deploy NycTaxiNetwork NycTaxiEcr NycTaxiIam NycTaxiS3Files NycTaxiMonitoring NycTaxiCicd --require-approval never
deactivate && cd ..
```
The app reads account/region from `CDK_DEFAULT_ACCOUNT`/`CDK_DEFAULT_REGION` (set
automatically from creds) — no hardcoded account. Network writes
`/nyctaxi/network/*` SSM; IAM writes `/agentcore/AGENT_EXECUTION_ROLE_ARN`.
- **CICD** stands up a CodeBuild pipeline that expects the user's own source
  wiring; it deploys for parity but is inert without his repo config. Note this.

## Phase 2 — AgentCore Memory (idempotent)

```bash
.venv/bin/python scripts/bootstrap_prod_local.py   # writes /agentcore/AGENTCORE_MEMORY_ID
```

## Phase 3 — Populate NOAA weather bucket (consumed by the EC2 mount demo)

Sync the LaGuardia GSOD station into `nyc-taxi-noaa-gsod-<acct>-<region>` (see
README "Initial AWS setup" step 5). The AgentCore Runtime doesn't read this, but
the EC2 mount demo does.

## Phase 4 — Build + push the image (DATA_MODE=full, baked 2024+2025)

**Demo data model:** **2024+2025 (~90M rows) baked locally** in chDB (fast, no
network); **2026+ fetched fresh at runtime** by `query_with_fresh_data` (chDB 4.1.8
fixed the CloudFront 403 that path hit). The baked cutoff is **2025-12-31** and the
delta window starts **2026-01** — which is the `DELTA_START` default, so no gap.

Build the compact 2024+2025 store on the **host** (the in-container bake can't be
made compact at build time — see gotchas), then build with `Dockerfile.prebaked`:

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text); REGION=us-east-1
ECR=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/nyc-taxi-agent
aws ecr get-login-password --region $REGION | finch login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com

# 1) Host-bake the compact 2024+2025 store (turnkey — see "compact bake" below).
#    Run in a chDB 4.1.8 env (host venv or a python:3.13 container):
CHDB_DATA_PATH="$PWD/full_chdb_data" BAKE_START_YEAR=2024 BAKE_END_YEAR=2025 \
  python3 scripts/bake_full.py        # ~15 min; prints BAKE_DONE + GC parts active≈2.1GB
# 2) Let Dockerfile.prebaked COPY the store (it's gitignored & .dockerignore'd by default):
sed -i.bak 's|^full_chdb_data/|# full_chdb_data/|' .dockerignore
finch build -t nyc-taxi-agent:latest -f Dockerfile.prebaked .
mv .dockerignore.bak .dockerignore   # restore
finch tag nyc-taxi-agent:latest "${ECR}:latest"     # NOTE the braces — see zsh gotcha below
finch push "${ECR}:latest"
```
> **2024+2025 (~90M rows) fits the 2048MB cap (~1.85GB compressed).** The original
> "65M = 2.4GB, doesn't fit" was inflated by the `chown -R` duplicate-layer bug
> (below); with that fixed the ~2.1GB on-disk store yields a ~1.85GB image. (Don't
> bake 2026 — it's the live delta and would approach the cap.)

### Compact bake — use `scripts/bake_full.py` (encodes 3 non-obvious lessons)
`scripts/bake_full.py` is the turnkey host bake (counterpart to the in-container
`init_db.py`). It handles all three things that otherwise need debugging:
1. **CloudFront rate throttle:** a 24-month sequential pull with plain `url()` starts
   returning `Code: 86` after ~16 months. The script sends a browser User-Agent
   header, spaces requests (`MONTH_SPACING_S`, default 15s), retries, and does a
   final backfill pass for any throttled months.
2. **OPTIMIZE FINAL** merges per-month parts into one.
3. **Fresh-process GC:** OPTIMIZE leaves the source parts *inactive* and chDB's
   in-process cleaner does **not** fire (even with `old_parts_lifetime=0` + a sleep);
   the script re-execs itself (`--gc`) so a fresh startup drops them
   (~6.8GB → ~2.1GB). It also writes `data_profile.json` (row_count/cutoff/delta_start).
   Verify the printed `GC parts active=1 ...≈2.1GB` (and no large `active=0`).
(`MONTHS=2026-01,2026-02 python3 scripts/bake_full.py` does a targeted backfill / quick smoke.)

### Image-size gotchas (fixed in the Dockerfiles — verify if size regresses)
- **`chown -R /app` doubles the image.** A trailing `RUN chown -R appuser /app`
  copies-up the baked store into a second overlay layer. The Dockerfiles create the
  user first and use `COPY --chown` (no copy-up) — the single biggest size win.
- **In-container `OPTIMIZE FINAL` does NOT shrink the build-time store** (inactive
  parts linger; the build can't run the fresh-process GC) — that's why the compact
  bake happens on the host and ships via `Dockerfile.prebaked`.
- **zsh ate `:latest`.** `$ECR:latest` in zsh applies the `:l` (lowercase) history
  modifier → `nyc-taxi-agentatest`. Always brace it: `"${ECR}:latest"`.

## Phase 5 — Register the AgentCore Runtime

```bash
AWS_REGION=us-east-1 python3 scripts/create_runtime.py --environment PRD --dry-run   # review payload
AWS_REGION=us-east-1 python3 scripts/create_runtime.py --environment PRD             # create + poll READY
```
- Account/region are no longer hardcoded — the script runs in the caller's account
  (set `EXPECTED_ACCOUNT` to re-enable the strict guard).
- After a fresh create it auto-binds the ID-dependent `OTEL_EXPORTER_OTLP_LOGS_HEADERS`
  via a follow-up `UpdateAgentRuntime` (parity with the reference runtime).
- **Redeploy after a new image:** `create_runtime.py --environment PRD --update`
  (updates in place via `UpdateAgentRuntime` instead of COLLISION-erroring).
- Exit codes: 0 ok · 2 collision (use `--update`) · 3 timeout · 4 create-failed.

## Phase 6 — Verify

```bash
.venv/bin/python - <<'PY'
import boto3, json, uuid
dp = boto3.client("bedrock-agentcore", "us-east-1")
arn = boto3.client("bedrock-agentcore-control","us-east-1").list_agent_runtimes()["agentRuntimes"][0]["agentRuntimeArn"]
r = dp.invoke_agent_runtime(agentRuntimeArn=arn, qualifier="DEFAULT",
      runtimeSessionId="verify-"+uuid.uuid4().hex, contentType="application/json",
      accept="application/json", payload=json.dumps({"text":"Busiest hour? one sentence."}).encode())
print(r["response"].read().decode())
PY
```

The first `invoke_agent_runtime` may return **500 / RuntimeClientError** on a cold
start or while a just-fixed IAM policy propagates — retry 3–4× with a short delay
before treating it as a real failure. If it persists, read the runtime's logs:
`/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT` (filter for `ERROR`/`Exception`).

**Verify both data paths** — this is the demo's whole point:
- **Baked-local (2024+2025):** ask "How many trips in January 2025?" → answered from
  the baked chDB store (no network); expect ~3.5M (`query_with_fresh_data` /
  `analyze_taxi_data` both read the baked table for in-range dates).
- **Live-fresh (2026):** ask "How many trips in January 2026? Use fresh data." →
  `query_with_fresh_data` fetches the CloudFront file at runtime; expect ~3.7M with
  **no 403** (the path chDB 4.1.8 fixed). Verified: a Jan-2026 question returned
  **3,724,894 trips**, live CDN, no 403.

> **Delta window aligns with the baked cutoff (no gap).** `_discover_delta_months()`
> probes from `os.getenv("DELTA_START", "2026-01")`. Because the baked cutoff is now
> **2025-12-31**, that default is exactly right: 2024+2025 are baked, 2026+ is the live
> delta — no missing-year gap. (Earlier, when only 2024 was baked, 2025 fell in a gap
> because the window still started at 2026; baking through 2025 resolved it. The tool
> fetches+unions every discovered delta month per call, so keep the baked cutoff close
> to "now" — don't widen the window to span many un-baked months.)

## Success criteria
- All 6 CDK stacks `CREATE_COMPLETE`; memory ACTIVE; image in ECR; runtime READY.
- `invoke_agent_runtime` returns a data-grounded answer.
- The fresh-data probe (post-cutoff question) returns 2025 rows with no 403.

## Idempotency / resume
Every phase is re-runnable: `cdk deploy` (no-op if unchanged), `bootstrap_prod_local.py`
(reuses), image push (re-tag), `create_runtime.py` (CLEAR/EXISTS/COLLISION + `--update`).
A mid-failure is safe to resume from the failed phase.

## Troubleshooting — validated in a real fresh-region deploy (us-west-2)
| Symptom | Cause | Fix |
|---|---|---|
| runtime invoke 500; logs show `AccessDeniedException` on `ConverseStream` | Bedrock **inference-profile ARN region** mismatch (was hardcoded us-east-1) | already fixed in `iam_stack.py` (`{self.region}`); ensure model access is enabled in the deploy region |
| `cdk deploy NycTaxiIam` fails: role `NycTaxiAgentMemoryRole` already exists | IAM is account-global; second region collides | already fixed — role is region-suffixed for non-us-east-1 |
| `create_runtime.py` errors fetching `/langfuse/*` | Langfuse SSM params don't exist **in the deploy region** | copy `/langfuse/*` (and they're SecureString) into the deploy region first (preflight checks this) |
| `aws bedrock-agentcore-control` "invalid choice" in AWS CLI | CLI lacks the service | use the boto3 client (`bedrock-agentcore-control` / `bedrock-agentcore`), not the CLI |
| `aws ssm send-command` parse error on inline shell | quotes/semicolons trip `--parameters` | pass commands via a `file://params.json` (`{"commands":[...]}`) |
| first invoke 500 then works | cold start / IAM propagation | retry a few times (see above) |

## Teardown — validated gotchas (do in this order)
1. `delete_agent_runtime` + `delete_memory` (boto3, control plane) — **do this FIRST**.
2. Delete the copied `/langfuse/*` + `/agentcore/*` + `/nyctaxi/network/*` SSM params.
3. `cdk destroy --all --force`.
4. **Weather bucket is `RemovalPolicy.RETAIN` and versioned** — `cdk destroy` leaves
   it. Purge ALL versions + delete markers (paginate `list_object_versions` →
   `delete_objects`), then `delete_bucket`. A plain `s3 rm --recursive` is not enough.
5. **`NycTaxiNetwork` (VPC) will `DELETE_FAILED`** if the runtime was created in its
   VPC: AgentCore leaves **Hyperplane ENIs** (`ela-attach`, service-managed — you
   cannot delete them) attached to the subnets for **~20–40 min** after the runtime
   is deleted (same as Lambda VPC ENI cleanup). Wait until
   `describe-network-interfaces` for those subnets returns 0, then retry
   `delete-stack NycTaxiNetwork`. Don't try to force-delete the ENIs — it fails with
   `OperationNotPermitted: ... 'ela-attach'`.

## Cost / caveats (tell the user before running)
- VPC NAT gateway(s) (~$32/mo each) + S3 Files + ECR storage + AgentCore Memory +
  Runtime + EC2 (mount demo, **t4g.2xlarge ≈ $196/mo** — sized for the fresh-data
  pandas materialisation; downsize once the tool aggregates in SQL) + CloudWatch.
  Real ongoing spend on an idle demo — stop/terminate the mount-demo instance when not in use.
- **Same-account second region is the safe full test** (prod region untouched), but
  needs the region-suffixed role fix (done) and `cdk bootstrap` in the new region.
  A second deploy in the *same* region is NOT isolated — it updates the live stacks.
- Region is configurable but **default + recommended `us-east-1`** (AgentCore
  Runtime, Memory, and the `us.` inference profile are US-region; verified also in
  us-east-2/us-west-2). Account is always taken from the caller — no hardcoded IDs.
