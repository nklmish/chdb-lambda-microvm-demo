---
name: deploy-mount-demo
description: Deploy and verify the S3 Files NFS mount demo on EC2 — runs the agent container on a Graviton EC2 host that mounts the S3 Files filesystem at /mnt/noaa-gsod via the amazon-efs-utils helper so weather queries use file() instead of the S3 fallback. Use when asked to demo or verify the S3 Files mount, since AgentCore Runtime itself cannot mount it.
---

# S3 Files mount demo on EC2 (validated end-to-end in us-west-2)

AgentCore Runtime's `CreateAgentRuntime` API has **no field to mount an external
S3 Files / EFS filesystem** (only ephemeral `sessionStorage`), and its container
runs non-root — so the deployed runtime never mounts `/mnt/noaa-gsod`; weather
there falls back to direct `s3()`. To genuinely demonstrate the S3 Files mount,
run the same image on a plain **EC2** host that mounts the filesystem. **Fargate
can't** — its native volumes are EFS-only and `AWS::S3Files` is a separate service.

The `NycTaxiMountDemo` CDK stack builds this host. This flow was deployed,
verified (`weather_tools` confirmed on the `file()` branch), and torn down — the
four non-obvious requirements below are all baked into the stack already.

## The 4 things that MUST be right (all already encoded in mount_demo_stack.py)
1. **Graviton/arm64 instance with enough RAM** (`t4g.2xlarge` = 32GB + arm64 AL2023
   AMI). The container image is `linux/arm64` (AgentCore runs it on Graviton); an x86
   instance → `exec format error`. **RAM matters:** `query_with_fresh_data`
   materialises the baked side into a pandas DataFrame, so with the baked table at
   ~90M rows (2024+2025) a loosely-filtered fresh query can build a ~10–13GB frame.
   `t4g.small` (2GB) OOM'd and wedged the host (SSM agent dropped → `StartSession`
   `TargetNotConnected`). 32GB is the safe size until the tool is changed to aggregate
   the baked side in chDB SQL instead of pandas.
2. **`amazon-efs-utils` mount helper**, not raw NFS. S3 Files requires
   `mount -t s3files <fs-id>:/ <dir>` (TLS + IAM auth). Raw `mount -t nfs4` →
   `access denied by server`. AL2023 packages efs-utils ≥3.0.0 (`dnf install -y
   amazon-efs-utils`); the helper resolves the mount target from the fs-id (no IP
   lookup, no `aws s3files` CLI needed).
3. **Instance role needs S3 Files client perms** — `AmazonS3FilesClientReadOnlyAccess`
   (grants `s3files:ClientMount`) + `s3:GetObject` on the weather bucket (for
   intelligent read routing). Without it → `access denied by server`.
4. **Same AZ as the mount target.** S3FilesStack puts the mount target in the
   first private subnet; the stack pins the instance to `vpc.availability_zones[0]`.
   Wrong AZ → `Failed to resolve file system DNS name`.

Plus: **IMDS hop limit = 2** (set in the stack) so the bridged Docker container
can assume the instance role.

## Prerequisites
- `NycTaxiNetwork`, `NycTaxiEcr`, `NycTaxiS3Files` deployed; image pushed to ECR
  as `:latest`; `/agentcore/AGENTCORE_MEMORY_ID` + `/langfuse/*` in SSM in the
  deploy region (i.e. **deploy-agentcore** has run).
- **NOAA bucket synced** with the LaGuardia station for the year you'll query,
  e.g. `nyc-taxi-noaa-gsod-<acct>-<region>/2024/72503014732.csv` — else `file()`
  finds no data. Sync from the public bucket (`s3://noaa-gsod-pds/<year>/72503014732.csv`).

## Deploy
```bash
cd cdk && source .venv/bin/activate
AWS_DEFAULT_REGION=<region> cdk deploy NycTaxiMountDemo --require-approval never
deactivate && cd ..
```
User-data (runs at boot) installs docker + amazon-efs-utils + botocore, mounts
`mount -t s3files <fs-id>:/ /mnt/noaa-gsod`, ECR-logs-in, and `docker run`s the
image with `-v /mnt/noaa-gsod:/mnt/noaa-gsod` + prod env. Give it ~3–5 min after
the stack completes (SSM registration + ~1 GB image pull).

## Redeploy a NEW :latest onto the EXISTING instance (pull does NOT happen on `cdk deploy`)
`cdk deploy` only re-pulls when the **user-data changes** — pushing a new `:latest`
to ECR alone is a CloudFormation **no-op**, so the running instance keeps the old
image. To make the live instance run the freshly-pushed image, re-run the
pull+recreate via SSM (the mount stays up; only the container is replaced). Pass the
commands via a `file://` JSON payload — inline quotes/semicolons trip the CLI parser:
```bash
ID=i-05a903426e9e41362   # or the stack's MountDemoInstanceId output
cat > /tmp/redeploy.json <<'JSON'
{"commands":[
 "set -xe",
 "TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')",
 "REGION=$(curl -s -H \"X-aws-ec2-metadata-token: $TOKEN\" http://169.254.169.254/latest/meta-data/placement/region)",
 "ACCOUNT=$(aws sts get-caller-identity --query Account --output text)",
 "IMAGE=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/nyc-taxi-agent:latest",
 "aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com",
 "docker pull $IMAGE",
 "MEM_ID=$(aws ssm get-parameter --region $REGION --name /agentcore/AGENTCORE_MEMORY_ID --query Parameter.Value --output text)",
 "LF_HOST=$(aws ssm get-parameter --region $REGION --name /langfuse/LANGFUSE_HOST --query Parameter.Value --output text)",
 "LF_PK=$(aws ssm get-parameter --region $REGION --name /langfuse/LANGFUSE_PUBLIC_KEY --query Parameter.Value --output text)",
 "LF_SK=$(aws ssm get-parameter --region $REGION --name /langfuse/LANGFUSE_SECRET_KEY --with-decryption --query Parameter.Value --output text)",
 "OTEL_HEADERS=\"Authorization=Basic $(printf '%s:%s' \"$LF_PK\" \"$LF_SK\" | base64 | tr -d '\\n'),x-langfuse-ingestion-version=4\"",
 "docker rm -f nyc-taxi-agent || true",
 "docker run -d --name nyc-taxi-agent --restart unless-stopped -p 8080:8080 -v /mnt/noaa-gsod:/mnt/noaa-gsod -e AWS_REGION=$REGION -e BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0 -e IS_PROD=true -e LANGFUSE_TRACING_ENVIRONMENT=PRD -e DISABLE_ADOT_OBSERVABILITY=true -e WEATHER_MOUNT_PATH=/mnt/noaa-gsod -e AGENTCORE_MEMORY_ID=$MEM_ID -e \"OTEL_EXPORTER_OTLP_ENDPOINT=$LF_HOST/api/public/otel\" -e \"OTEL_EXPORTER_OTLP_HEADERS=$OTEL_HEADERS\" $IMAGE",
 "sleep 8 && docker ps --format '{{.Image}} {{.Status}}'"
]}
JSON
CMD=$(aws ssm send-command --region us-east-1 --instance-ids $ID \
  --document-name AWS-RunShellScript --parameters file:///tmp/redeploy.json \
  --query 'Command.CommandId' --output text)
aws ssm get-command-invocation --region us-east-1 --command-id $CMD --instance-id $ID \
  --query '{status:Status,out:StandardOutputContent,err:StandardErrorContent}' --output json
```
(Alternatively, `cdk` can replace the instance, but SSM pull+recreate is faster and
keeps the S3 Files mount.) Env vars mirror the stack's user-data `docker run`.
> **Quote `OTEL_EXPORTER_OTLP_HEADERS`** — its value is `Authorization=Basic <b64>,...`
> (contains a space). Unquoted, docker splits it and fails with `invalid reference
> format: ... must be lowercase`. The `-e "KEY=$VAL"` form (escaped `\"` in the JSON)
> keeps it one arg. The `docker rm -f` runs first, so a failed `docker run` leaves the
> host with **no** container — re-run the corrected command to restore it.
> **Prune before pulling, or the disk fills.** docker keeps the old image until pruned;
> pulling the new ~1.8GB image alongside it can exceed the root volume → the pull fails
> with `no space left on device` and (if the disk fully fills) the **SSM agent itself
> wedges** (commands return ResponseCode -1, empty output). Add `docker rm -f
> nyc-taxi-agent` + `docker image prune -af` *before* `docker pull`. The stack now
> provisions a **30GB gp3 root volume** (was the AL2023 ~8GB default) for headroom; if
> an instance is already wedged, the clean recovery is `cdk deploy NycTaxiMountDemo`
> after bumping the volume — it **replaces** the instance (new instance id) and the
> user-data pulls `:latest` fresh on the clean disk.

## Verify (via SSM Session Manager — no SSH, no public IP)
```bash
ID=$(aws cloudformation describe-stacks --stack-name NycTaxiMountDemo --region <region> \
  --query "Stacks[0].Outputs[?OutputKey=='MountDemoInstanceId'].OutputValue" --output text)
# Use `aws ssm send-command` with AWS-RunShellScript (pass commands via a file:// JSON
# payload — inline shell with quotes/semicolons trips the CLI parser):
#   uname -m                                   -> aarch64
#   mount | grep noaa-gsod                     -> ...type nfs4 (vers=4.2...)  (efs-proxy TLS)
#   docker ps                                  -> nyc-taxi-agent Up (healthy)
#   docker exec nyc-taxi-agent ls /mnt/noaa-gsod/2024/72503014732.csv   <- THE proof
#   curl -s localhost:8080/chat -d '{"text":"Do rainy days have more taxi trips than clear days in 2024? One sentence."}'
```
**Definitive proof the mount is used:** the CSV is visible *inside the container*
at `/mnt/noaa-gsod/<year>/72503014732.csv`. `weather_tools.py` keys off
`os.path.exists(<mount path>)` → `file('/mnt/noaa-gsod/...')`. (A newer image also
logs `weather source: S3 Files mount via file() — ...`; the older `d24` image
lacks that line, so use the in-container `ls` as the proof.)

## Troubleshooting — exact errors seen in testing → cause → fix
| Symptom | Cause | Fix |
|---|---|---|
| container `Restarting (255)`, logs `exec format error` | x86 instance running arm64 image | use `t4g.*` (Graviton) + arm64 AMI (stack does this) |
| `mount.nfs4: access denied by server` | used raw NFS, or role lacks `s3files:ClientMount` | use `mount -t s3files`; attach `AmazonS3FilesClientReadOnlyAccess` |
| `mount.s3files: command not found` | amazon-efs-utils missing/<3.0.0 | `dnf install -y amazon-efs-utils` (AL2023 has 3.1.1); else build from source |
| `Failed to resolve file system DNS name` | instance not in mount target's AZ | place instance in `vpc.availability_zones[0]` (stack does this) |
| container can't get AWS creds / IMDS timeouts | IMDS hop limit 1 blocks bridged container | set `HttpPutResponseHopLimit: 2` (stack does this) |
| `aws: invalid choice 's3files'` | instance's AWS CLI too old | not needed — the s3files mount helper resolves the target from the fs-id |
| weather answer but "no data" | NOAA CSV for that year not synced to the bucket | sync `s3://noaa-gsod-pds/<year>/72503014732.csv` into the weather bucket |

## Teardown
```bash
cd cdk && source .venv/bin/activate && AWS_DEFAULT_REGION=<region> cdk destroy NycTaxiMountDemo
```
The instance is deleted cleanly (no managed-ENI delay — that delay only affects
the AgentCore Runtime's VPC ENIs, not this EC2). See deploy-agentcore for the
full-environment teardown gotchas.
