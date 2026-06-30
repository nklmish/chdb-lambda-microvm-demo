---
name: deploy-microvm
description: Deploy the NYC Taxi agent to AWS Lambda MicroVMs in the current account — package the app as a MicroVM image with chDB baked in and lifecycle hooks, run a Firecracker-isolated MicroVM with its own private chDB, and verify over the dedicated HTTPS endpoint. Use when asked to "deploy to lambda microvms", "run on microvms", "deploy the microvm demo", or show the chDB × Lambda MicroVMs reference architecture.
---

# Deploy the agent to AWS Lambda MicroVMs (any AWS account)

This runs the agent on **Lambda MicroVMs** — Firecracker-isolated, snapshot-resumable
compute. Every MicroVM carries its **own private chDB engine**, hot from the first
millisecond (snapshot boot), billed only while running, suspended when idle. It is the
cloud embodiment of "chDB as the agent's local data engine": the data layer travels
*inside* the isolated VM, so the agent thinks at CPU speed with zero network round-trips.

> **Status: validated end-to-end** against the live `lambda-microvms` API in us-west-2.
> The gotchas it surfaced are fixed in code and listed in Troubleshooting — read that
> first if anything errors.

**Why MicroVMs and not the AgentCore Runtime path (`deploy-agentcore`)?** MicroVMs expose
two capabilities AgentCore Runtime does not give you: a **build-time snapshot** gated on
your app being warm (`/ready` hook), and **suspend/resume with state preserved**. And the
networking is far simpler — MicroVMs have **public egress by default**, so there is **no
VPC, no NAT gateway, no $32/mo/NAT** (the AgentCore path needs all three).

## What gets created

- An S3 artifact bucket: `nyc-taxi-microvm-artifacts-<account>-<region>` (private).
- Two IAM roles (confused-deputy-safe trust: `aws:SourceAccount` + `aws:SourceArn`):
  - `NycTaxiMicroVMBuildRole` — S3 read + build logs + public-ECR pull.
  - `NycTaxiMicroVMExecutionRole` — runtime logs + Bedrock invoke (3 inference-profile regions).
- One `MicrovmImage` (`nyc-taxi-agent-microvm`) with the chDB store baked in + lifecycle hooks.
- One running `Microvm` with its dedicated HTTPS endpoint.

No secrets are read or written. Bedrock credentials reach the guest via IMDSv2 (the
execution role) — nothing is baked into env vars.

## Prerequisites (the script preflights these — STOP on failure)

- **AWS CLI ≥ 2.35.12** — the version that ships the `lambda-microvms` service. Older CLIs
  return `Invalid choice: lambda-microvms`. Upgrade: `brew upgrade awscli` (or reinstall).
- **Lambda MicroVMs available in the region** — the script confirms a managed base image
  exists via `list-managed-microvm-images`. Default region **us-west-2**.
- **Bedrock model access** for `anthropic.claude-sonnet-4-20250514-v1:0` in the deploy
  region (the `us.` inference profile routes us-east-1/us-east-2/us-west-2 — enable access
  in whichever region you deploy to).
- **IAM permissions** for the caller: `lambda-microvms:*`, `iam:CreateRole/PutRolePolicy/...`
  on the two named roles, `s3:*` on the artifact bucket, and **`lambda:PassNetworkConnector`**
  on the AWS-managed default connectors (required by `RunMicrovm` even with no custom connector).

## One command

```bash
python scripts/deploy_microvm.py --region us-west-2            # real deploy (sample data)
python scripts/deploy_microvm.py --dry-run                    # print the exact AWS calls first
python scripts/deploy_microvm.py --data-mode full             # bake 2024+2025 (bigger image)
python scripts/deploy_microvm.py --terminate-only             # tear down running MicroVMs
```

The script is idempotent: re-running reuses the bucket/roles, ships a **new image version**
(`update-microvm-image`), and runs a fresh MicroVM. Each phase is safe to resume after a
mid-failure.

## Phases (what the script does)

0. **preflight** — CLI has `lambda-microvms`; caller identity; managed base image exists.
1. **bucket** — create/reuse the private S3 artifact bucket (same region as the image).
2. **iam** — create/reuse the build + execution roles; (re)put trust + inline policies.
3. **package** — zip the repo with `Dockerfile.microvm` renamed to `Dockerfile` at the root.
   A **secret guard** refuses to ship any `.env*` except `.env.example`; data dirs, `.git`,
   `.venv`, tests, CDK, and scratch are excluded.
4. **image** — `create-microvm-image` (or `update-microvm-image` on re-run) with:
   - hooks on **port 9000**: build hooks `ready` + `validate` ENABLED; runtime hooks
     `run`/`resume`/`suspend`/`terminate` ENABLED.
   - `environmentVariables` for a self-contained runtime (region, model id, tracing off).
   Then **poll** `list-microvm-image-versions` until the newest version is `SUCCESSFUL`.
5. **run** — `run-microvm` with public egress (default connectors), `idlePolicy`
   (auto-suspend after 900s idle, auto-resume on traffic), 8 hr max duration, CloudWatch logs.
6. **verify** — mint a 30-min auth token (`allowedPorts:[{port:8080}]`), then call the
   endpoint with `X-aws-proxy-auth` + `X-aws-proxy-port: 8080`:
   `/ping` (zero-I/O health) → `/health` (chDB row count) → `/chat` ("Busiest hour?").

## The three demos this enables (run after deploy)

Grab the endpoint + a token from the deploy output (or `get-microvm` + `create-microvm-auth-token`).

1. **Hot from millisecond zero (snapshot boot).** The `/ready` hook returns 200 only after
   the baked ~9.5M-row chDB store is warm, so Lambda snapshots a hot engine. The first
   `/chat` analytical query is served warm — no chDB init, no store load. Contrast with the
   local emulator's cold-vs-warm numbers (`scripts/microvm_local_lifecycle.py`).
2. **The agent brain that suspends and resumes.** Ask *"How has NYC tipping changed over the
   decade?"* → `analyze_fleet_across_clouds` federates 5 clouds and materializes the result
   into the local chDB MergeTree. Then:
   ```bash
   aws lambda-microvms suspend-microvm --microvm-identifier <id> --region us-west-2   # no charge while suspended
   aws lambda-microvms resume-microvm  --microvm-identifier <id> --region us-west-2   # state intact
   ```
   Ask the same question → served from the on-disk cache in ms. The agent brain survived
   suspend/resume because the MergeTree lives on the VM's persistent disk.
3. **Federation hub in a private VM.** Ask *"Which pickup zones tip best?"* → one chDB SQL
   joins local 2024 + a remote source, inside one isolated VM; only a tiny request/answer
   crosses the endpoint.

## Local fidelity check (no AWS) — run this first if iterating

```bash
python scripts/microvm_local_lifecycle.py --synthetic   # real hooks + chDB, emulated suspend/resume
pytest tests/test_microvm_hooks.py -v                    # unit + integration
```

The emulator starts the real `microvm_entrypoint.py` (app :8080 + hooks :9000), drives the
genuine lifecycle, and proves both the snapshot-boot effect (cold vs warm query latency)
and that the chDB store survives a suspend/resume (kill + restart on the same disk).

## Success criteria

- Image version `SUCCESSFUL`; MicroVM reaches a state where ingress succeeds.
- `/ping` → 200, `/health` → 200 with a non-zero `row_count`, `/chat` → 200 with a
  data-grounded answer.
- (Demo 2) the same federation question after suspend/resume returns `mode: local cache`.

## Troubleshooting — validated against the live API

| Symptom | Cause | Fix |
|---|---|---|
| `aws: Invalid choice: lambda-microvms` | CLI older than 2.35.12 | `brew upgrade awscli` (or reinstall the v2 pkg) |
| `CreateRole ... 'description' failed to satisfy ... pattern` | non-ASCII char (em-dash) in `--description` | fixed — descriptions are plain ASCII |
| `RunMicrovm AccessDenied: lambda:PassNetworkConnector` | caller lacks PassNetworkConnector on the default connectors | add it (Resource: `arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:*`) |
| image build `FAILED` | Dockerfile/bake error | read CloudWatch `/aws/lambda-microvms/*`; `list-microvm-image-builds` carries `stateReason` |
| `/chat` 200 but answer not data-grounded / Bedrock AccessDenied in logs | model access not enabled in the deploy region | enable Claude Sonnet 4 access in that region's Bedrock console |
| proxy 502 with MicroVM `RUNNING` | app not listening on 8080 yet, or still warming | retry; the verify step already retries token+call for ~2 min |
| build can't pull base image | build role missing public-ECR perms | fixed — build policy grants `ecr-public:GetAuthorizationToken` |

## Teardown

```bash
python scripts/deploy_microvm.py --terminate-only --region us-west-2   # terminate running MicroVMs
# Image versions incur storage cost even with nothing running — clean up when done:
aws lambda-microvms delete-microvm-image --image-identifier \
  arn:aws:lambda:us-west-2:<account>:microvm-image:nyc-taxi-agent-microvm --region us-west-2
# (delete-microvm-image-version can't remove the last version — delete the whole image.)
```

The S3 artifact bucket and IAM roles are cheap to leave; remove them with
`aws s3 rb --force` and `aws iam delete-role-policy`/`delete-role` if you want a clean account.

## Cost notes

- **No VPC/NAT** (public egress default) — the biggest cost difference vs `deploy-agentcore`.
- You pay for: MicroVM **runtime only** (suspended = no charge), image-version storage,
  S3 artifact (tiny), CloudWatch logs, and Bedrock per-token. An idle, suspended demo costs
  almost nothing — but **terminate** and **delete the image** to be safe.
