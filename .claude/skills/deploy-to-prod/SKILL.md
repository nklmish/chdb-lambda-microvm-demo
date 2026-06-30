---
name: deploy-to-prod
description: One command for "deploy everything to AWS and let me use it from my browser". Deploys the full stack to AWS AgentCore (full data, Memory, S3 Files mount demo), then runs the chat UI locally pointed at the deployed runtime so the browser talks to the AWS agent. Use when asked to "deploy to prod", "deploy to AWS and run the UI", "deploy everything", or run the full cloud stack with a local browser.
---

# Deploy the full stack to AWS, drive it from a local browser

This is the end-to-end "ship it to AWS and use it from my browser" flow. The agent
(with **2024+2025 (~90M rows) baked locally in chDB**, and **2026+ fetched fresh** at
runtime by `query_with_fresh_data`), AgentCore Memory, and the S3 Files mount demo all
run **on AWS**. The baked cutoff is 2025-12-31; the live-delta window starts 2026-01.

There are **two ways** to drive it from a browser — pick based on whether you want
the full in-app metrics:

```
PRIMARY — full UI with token metrics + tool chips + Langfuse links:
  Browser (localhost:8080)
     │  SSM AWS-StartPortForwardingSession  (no SSH, no public IP)
     ▼
  EC2 mount-demo host  ──runs the SAME container on :8080──▶  agent + chDB (2024+2025)
                                                              ├─ AgentCore Memory (AWS)
                                                              └─ weather via S3 Files mount (file())

ALTERNATIVE — lighter, but NO metrics (see caveat in Step 3b):
  Browser (localhost:8080) ─▶ scripts/ui_proxy.py ──InvokeAgentRuntime (SigV4)─▶ AgentCore Runtime (AWS)
```

> **Why the SSM tunnel is the recommended UI path:** the browser talks to the app's
> own FastAPI server inside the EC2 container, so you get the **full SSE stream** —
> live token-usage metrics, per-tool chips, and the "View in Langfuse" trace link.
> `ui_proxy.py` calls AgentCore `InvokeAgentRuntime`, which is **request/response**
> (one synchronous answer, no intermediate events), so the UI shows **0 token
> metrics and no tool chips** and can't surface a per-turn Langfuse link. The AgentCore
> *Runtime* still records its own traces server-side; you just don't get them in the UI.

> Heavy + billable: full CDK infra (VPC/NAT, ECR, IAM, S3 Files, Monitoring, CICD),
> a `DATA_MODE=full` (2024+2025, ~90M rows) image, an AgentCore Runtime, AgentCore
> Memory, and an EC2 host. Tell the user the cost up front (see deploy-agentcore).
> **Image-size cap:** the baked 2024+2025 store (~2.1GB on disk) builds to a **~1.85GB
> compressed** image — under the **2048MB** AgentCore Runtime cap (quota L-0A9E32B3),
> but only because the Dockerfiles avoid the `chown -R` duplicate-layer (see
> deploy-agentcore). Don't bake 2026 too (that's the live delta and would approach the
> cap). See deploy-agentcore Phase 4 for the compact-bake recipe.

## Step 0 — Already deployed? Just CONNECT (no deploy) — the common case
If the stack already exists in this account (someone has run the deploy), **do NOT
redeploy** — a teammate just wants the browser UI. Confirm it's up, then open the
SSM tunnel (Step 3). One check:
```bash
ID=$(aws cloudformation describe-stacks --stack-name NycTaxiMountDemo --region us-east-1 \
      --query "Stacks[0].Outputs[?OutputKey=='MountDemoInstanceId'].OutputValue" --output text 2>/dev/null)
echo "mount-demo instance: ${ID:-<none — not deployed; do Steps 1-2>}"
```
- If it prints an instance id → skip Steps 1–2 entirely and go to **Step 3** (tunnel).
- Prereqs the teammate must have locally: **AWS credentials for this account** and the
  **session-manager-plugin** (`brew install --cask session-manager-plugin`). Those are
  the only things that can't be automated.
- If it prints `<none>` → nothing is deployed in this account; do Steps 1–2 first.

## Step 1 — Deploy the full stack to AWS
Run the **deploy-agentcore** skill end to end (it builds the `DATA_MODE=full` image,
so the friend gets ALL the data, not the 1-month sample). Its preflight gates on
AWS creds, Bedrock model access, Langfuse SSM params, and tooling — do not skip them.

## Step 2 — Deploy the S3 Files mount demo (optional but part of "everything")
Run the **deploy-mount-demo** skill (the `NycTaxiMountDemo` EC2 host). Skip only if
the user doesn't need the mount demonstration.

## Step 3 — Open the full UI via SSM port-forward to the EC2 container (RECOMMENDED)
The mount-demo host already runs the **same image** with the app's FastAPI server on
:8080. Port-forward to it over SSM (no SSH, no public IP) and open the local port —
the browser then hits the app directly, so the full SSE UI (token metrics, tool
chips, Langfuse link) works.
```bash
aws ssm start-session --target i-05a903426e9e41362 --region us-east-1 \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8080"],"localPortNumber":["8080"]}'
# → open http://localhost:8080
```
- Replace the instance id with this deploy's `MountDemoInstanceId`
  (`aws cloudformation describe-stacks --stack-name NycTaxiMountDemo --query "Stacks[0].Outputs[?OutputKey=='MountDemoInstanceId'].OutputValue" --output text`).
- Requires the SSM Session Manager plugin locally and `ssm:StartSession` on the caller.
- This is the genuine app server, so weather uses the **S3 Files mount** (`file()`),
  Memory is wired, and the footer shows real token counts + the Langfuse trace link.

## Step 3b — (Alternative) ui_proxy.py — lighter, but NO in-app metrics
Use only if the EC2 host isn't deployed or SSM isn't available. **Caveat:** because
AgentCore `InvokeAgentRuntime` is request/response, the UI shows **0 token metrics
and no tool chips**, and there's no per-turn Langfuse link (see the intro note).
```bash
AWS_REGION=<deploy-region> .venv/bin/python scripts/ui_proxy.py   # → http://localhost:8080
```
- Auto-discovers the READY `nyc_taxi_agent_*` runtime (or set `AGENTCORE_RUNTIME_ARN`).
- Serves `static/index.html`; implements `/health`, `/chat`, `/chat/stream` by calling
  `bedrock-agentcore:InvokeAgentRuntime` (SigV4 with the caller's live creds).
- One session id per proxy run, so AgentCore Memory keeps conversational context.
- The runtime's `/invocations` is synchronous, so the proxy chunks the full answer for
  a streaming feel (not true token streaming) and cannot recover usage/tool events.

## Step 4 — Verify in the browser (show BOTH data paths — the demo's point)
Open **http://localhost:8080**. Via the SSM tunnel (Step 3) the footer shows token
metrics and a Langfuse link; via ui_proxy (Step 3b) it won't.
- **Baked-local:** "How many trips in January 2025?" → answered from the baked chDB
  store (2024+2025), no network, ~3.5M.
- **Live-fresh:** "How many trips in January 2026? Use fresh data." →
  `query_with_fresh_data` fetches the CloudFront file at runtime, ~3.7M, no 403.
- General: "Average fare in Manhattan?", "Busiest hour of the day?".

## Success criteria
- deploy-agentcore: all stacks `CREATE_COMPLETE`, memory ACTIVE, runtime READY,
  `invoke_agent_runtime` returns a data-grounded answer.
- SSM tunnel: `http://localhost:8080` serves the app; chat returns full-data answers
  with token metrics + a Langfuse link in the footer.
- (If used) `ui_proxy.py` prints the resolved runtime ARN and serves localhost:8080
  (answers correct; metrics absent — expected).

## Notes / caveats
- **This is not "run in prod"** (that's the local app in prod mode, memory-enabled,
  cheap — the `run-prod` skill). This skill deploys to AWS and is billable.
- Bedrock + AgentCore + Memory are US-region; default/recommended `us-east-1`.
- Teardown: see deploy-agentcore (delete runtime/memory first; purge the RETAIN'd
  versioned weather bucket; the VPC stack waits ~20–40 min on AgentCore Hyperplane
  ENIs before it can delete). `cdk destroy NycTaxiMountDemo` for the EC2 host.
- Troubleshooting for each phase lives in deploy-agentcore and deploy-mount-demo.
