#!/usr/bin/env python3
"""Deploy the NYC Taxi agent to AWS Lambda MicroVMs — end to end, one command.

Account-agnostic: every resource is derived from the caller's identity and the
target region. Nothing is hardcoded; no secrets are read or written.

Phases (each idempotent / safe to re-run):
  0. preflight   — CLI has lambda-microvms; caller identity; base image exists
  1. iam         — create/reuse build role + execution role (confused-deputy-safe)
  2. bucket      — create/reuse the S3 artifact bucket (same region as the image)
  3. package     — zip the repo (Dockerfile.microvm -> Dockerfile), no secrets/data
  4. image       — create-or-update the MicroVM image with lifecycle hooks; poll build
  5. run         — run-microvm (public egress default; up to 8 hr; auto-suspend)
  6. verify      — mint an auth token and call /ping, /health, /chat over HTTPS

Usage:
  python scripts/deploy_microvm.py --dry-run            # print the exact AWS calls
  python scripts/deploy_microvm.py                      # real deploy (sample data)
  python scripts/deploy_microvm.py --data-mode full     # bake 2024+2025
  python scripts/deploy_microvm.py --keep               # don't print teardown hint
  python scripts/deploy_microvm.py --terminate-only     # terminate running MicroVMs

Requires AWS CLI >= 2.35.12 (the version that ships the `lambda-microvms`
service) and IAM permissions for lambda-microvms:*, iam:*Role/*RolePolicy on the
two named roles, s3 on the artifact bucket, and lambda:PassNetworkConnector on
the AWS-managed default connectors.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_REGION = "us-west-2"
DEFAULT_NAME = "nyc-taxi-agent-microvm"
BUILD_ROLE_NAME = "NycTaxiMicroVMBuildRole"
EXEC_ROLE_NAME = "NycTaxiMicroVMExecutionRole"

# Bedrock model the agent invokes (cross-region inference profile -> 3 regions).
BEDROCK_MODEL_ID = os.getenv(
    "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"
)
BEDROCK_REGIONS = ("us-east-1", "us-east-2", "us-west-2")

# Region holding the ClickHouse Cloud credential params (/clickhouse/*). The
# federation warehouse leg resolves these from SSM at runtime (cloud_sources.py),
# so nothing secret is baked into the image. Override with CLICKHOUSE_SSM_REGION.
CLICKHOUSE_SSM_REGION = os.getenv("CLICKHOUSE_SSM_REGION", "us-east-1")

# Region holding the Langfuse Cloud credential params (/langfuse/*). The MicroVM
# resolves these from SSM at boot to configure OTLP trace export to Langfuse — no
# secret is baked into the image (mirrors the ClickHouse pattern). Override with
# LANGFUSE_SSM_REGION.
LANGFUSE_SSM_REGION = os.getenv("LANGFUSE_SSM_REGION", "us-east-1")

# Files/dirs never shipped in the build artifact: secrets, local data, scratch,
# build outputs, and anything irrelevant to the runtime image.
EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".kiro", ".agent",
    "cdk", "evaluation", "refactor-docs", "blog", "tests", "agentcore", "cicd",
    "local_chdb_data", "full_chdb_data", ".microvm_emulator_data", "node_modules",
    ".claude", ".github", "scripts",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".swp", ".log", ".bak"}
EXCLUDE_NAMES = {".DS_Store", "data_profile.json"}
# Never ship anything matching these (defense-in-depth secret guard).
SECRET_PREFIXES = (".env",)


class DeployError(RuntimeError):
    pass


def _run(cmd: list[str], *, dry_run: bool, capture: bool = True) -> dict | str | None:
    """Run an AWS CLI command. In dry-run, print it and return a sentinel."""
    printable = " ".join(_shell_quote(c) for c in cmd)
    if dry_run:
        print(f"  [dry-run] {printable}")
        return {} if capture else None
    print(f"  $ {printable}")
    try:
        out = subprocess.run(cmd, check=True, capture_output=capture, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise DeployError(f"command failed: {printable}\n{stderr}") from exc
    if not capture:
        return None
    body = (out.stdout or "").strip()
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def _shell_quote(s: str) -> str:
    return s if all(c.isalnum() or c in "-_=:/.@," for c in s) else json.dumps(s)


def aws(service: str, op: str, *args: str, region: str, dry_run: bool, capture: bool = True):
    return _run(["aws", service, op, *args, "--region", region], dry_run=dry_run, capture=capture)


# ─── Phase 0: preflight ──────────────────────────────────────────────────────


def preflight(region: str) -> str:
    print("[0/6] preflight")
    ver = subprocess.run(["aws", "--version"], capture_output=True, text=True).stdout.strip()
    print(f"  aws cli: {ver}")
    help_rc = subprocess.run(
        ["aws", "lambda-microvms", "help"], capture_output=True, text=True
    ).returncode
    if help_rc != 0:
        raise DeployError(
            "this AWS CLI lacks `lambda-microvms` — upgrade to >= 2.35.12 "
            "(e.g. `brew upgrade awscli`)."
        )
    ident = _run(["aws", "sts", "get-caller-identity"], dry_run=False)
    account = ident["Account"]
    print(f"  account={account} region={region} arn={ident['Arn']}")
    images = aws("lambda-microvms", "list-managed-microvm-images", region=region, dry_run=False)
    base = next((i["imageArn"] for i in images.get("items", [])), None)
    if not base:
        raise DeployError(f"no managed base image in {region} — is the region enabled?")
    print(f"  managed base image: {base}")
    return account


def managed_base_arn(region: str) -> str:
    images = aws("lambda-microvms", "list-managed-microvm-images", region=region, dry_run=False)
    return images["items"][0]["imageArn"]


# ─── Phase 1: IAM roles ──────────────────────────────────────────────────────


def _trust_policy(account: str, region: str) -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "StringEquals": {"aws:SourceAccount": account},
                        "ArnLike": {
                            "aws:SourceArn": f"arn:aws:lambda:{region}:{account}:microvm-image*"
                        },
                    },
                }
            ],
        }
    )


def _build_role_policy(account: str, region: str, bucket: str) -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "ReadCodeArtifact",
                    "Effect": "Allow",
                    "Action": ["s3:GetObject"],
                    "Resource": f"arn:aws:s3:::{bucket}/*",
                },
                {
                    "Sid": "BuildLogs",
                    "Effect": "Allow",
                    "Action": [
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                    ],
                    "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda-microvms/*",
                },
                {
                    "Sid": "PublicEcrPull",
                    "Effect": "Allow",
                    "Action": [
                        "ecr-public:GetAuthorizationToken",
                        "sts:GetServiceBearerToken",
                    ],
                    "Resource": "*",
                },
            ],
        }
    )


def _exec_role_policy(account: str, region: str) -> str:
    model_arns = [
        f"arn:aws:bedrock:{r}::foundation-model/anthropic.claude-sonnet-4-20250514-v1:0"
        for r in BEDROCK_REGIONS
    ]
    profile_arns = [
        f"arn:aws:bedrock:{r}:{account}:inference-profile/{BEDROCK_MODEL_ID}"
        for r in BEDROCK_REGIONS
    ]
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "RuntimeLogs",
                    "Effect": "Allow",
                    "Action": [
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                    ],
                    "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda-microvms/*",
                },
                {
                    "Sid": "BedrockInvoke",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    "Resource": model_arns + profile_arns,
                },
                {
                    # Federation warehouse leg: read ClickHouse Cloud creds from
                    # SSM /clickhouse/* at runtime (least-privilege; no baked
                    # secrets). Harmless when the params don't exist — the app's
                    # SSM fallback is best-effort and just skips the CHC leg.
                    "Sid": "ReadClickHouseCreds",
                    "Effect": "Allow",
                    "Action": ["ssm:GetParameter", "ssm:GetParameters"],
                    "Resource": (
                        f"arn:aws:ssm:{CLICKHOUSE_SSM_REGION}:{account}:parameter/clickhouse/*"
                    ),
                },
                {
                    "Sid": "DecryptClickHouseSecret",
                    "Effect": "Allow",
                    "Action": ["kms:Decrypt"],
                    "Resource": "*",
                    "Condition": {
                        "StringEquals": {
                            "kms:ViaService": f"ssm.{CLICKHOUSE_SSM_REGION}.amazonaws.com"
                        }
                    },
                },
                {
                    # Observability: read Langfuse Cloud creds from SSM /langfuse/*
                    # at boot to configure OTLP trace export (least-privilege; no
                    # baked secrets). Harmless when the params don't exist — the
                    # boot cred-resolve is best-effort and just leaves tracing off.
                    "Sid": "ReadLangfuseCreds",
                    "Effect": "Allow",
                    "Action": ["ssm:GetParameter", "ssm:GetParameters"],
                    "Resource": (
                        f"arn:aws:ssm:{LANGFUSE_SSM_REGION}:{account}:parameter/langfuse/*"
                    ),
                },
                {
                    "Sid": "DecryptLangfuseSecret",
                    "Effect": "Allow",
                    "Action": ["kms:Decrypt"],
                    "Resource": "*",
                    "Condition": {
                        "StringEquals": {
                            "kms:ViaService": f"ssm.{LANGFUSE_SSM_REGION}.amazonaws.com"
                        }
                    },
                },
                {
                    # Distributed-scan cold lake: read the private yellow-taxi
                    # parquet the MicroVM's chDB s3() scans. Least-privilege —
                    # scoped to this account's artifact bucket's lake/ prefix.
                    "Sid": "ReadColdLake",
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:ListBucket"],
                    "Resource": [
                        f"arn:aws:s3:::nyc-taxi-microvm-artifacts-{account}-{region}",
                        f"arn:aws:s3:::nyc-taxi-microvm-artifacts-{account}-{region}/lake/*",
                    ],
                },
            ],
        }
    )


def _ensure_role(name: str, trust: str, policy_name: str, policy: str, *, dry_run: bool) -> str:
    """Create the role if absent and (re)put its inline policy. Returns the ARN."""
    existing = subprocess.run(
        ["aws", "iam", "get-role", "--role-name", name],
        capture_output=True, text=True,
    )
    if existing.returncode == 0:
        arn = json.loads(existing.stdout)["Role"]["Arn"]
        print(f"  role exists: {name}")
    else:
        created = _run(
            ["aws", "iam", "create-role", "--role-name", name,
             "--assume-role-policy-document", trust,
             "--description", "NYC Taxi agent - Lambda MicroVMs"],
            dry_run=dry_run,
        )
        arn = created.get("Role", {}).get("Arn", f"arn:aws:iam::ACCOUNT:role/{name}") if isinstance(created, dict) else f"arn:aws:iam::ACCOUNT:role/{name}"
        print(f"  role created: {name}")
    # Keep trust + policy current even when the role pre-existed.
    _run(["aws", "iam", "update-assume-role-policy", "--role-name", name,
          "--policy-document", trust], dry_run=dry_run)
    _run(["aws", "iam", "put-role-policy", "--role-name", name,
          "--policy-name", policy_name, "--policy-document", policy], dry_run=dry_run)
    return arn


def ensure_roles(account: str, region: str, bucket: str, *, dry_run: bool) -> tuple[str, str]:
    print("[2/6] iam roles")
    trust = _trust_policy(account, region)
    build_arn = _ensure_role(
        BUILD_ROLE_NAME, trust, "build-policy",
        _build_role_policy(account, region, bucket), dry_run=dry_run,
    )
    exec_arn = _ensure_role(
        EXEC_ROLE_NAME, trust, "exec-policy",
        _exec_role_policy(account, region), dry_run=dry_run,
    )
    if not dry_run:
        # IAM is eventually consistent; give the new roles a moment before use.
        time.sleep(8)
    return build_arn, exec_arn


# ─── Phase 2: artifact bucket ────────────────────────────────────────────────


def ensure_bucket(account: str, region: str, *, dry_run: bool) -> str:
    print("[1/6] artifact bucket")
    bucket = f"nyc-taxi-microvm-artifacts-{account}-{region}"
    head = subprocess.run(
        ["aws", "s3api", "head-bucket", "--bucket", bucket, "--region", region],
        capture_output=True, text=True,
    )
    if head.returncode == 0:
        print(f"  bucket exists: {bucket}")
        return bucket
    args = ["aws", "s3api", "create-bucket", "--bucket", bucket, "--region", region]
    if region != "us-east-1":
        args += ["--create-bucket-configuration", f"LocationConstraint={region}"]
    _run(args, dry_run=dry_run)
    _run(["aws", "s3api", "put-public-access-block", "--bucket", bucket,
          "--public-access-block-configuration",
          "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"],
         dry_run=dry_run)
    print(f"  bucket created: {bucket}")
    return bucket


# ─── Phase 3: package ────────────────────────────────────────────────────────


def _included(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    parts = rel.parts
    if any(p in EXCLUDE_DIRS for p in parts[:-1]):
        return False
    name = path.name
    if name in EXCLUDE_NAMES or path.suffix in EXCLUDE_SUFFIXES:
        return False
    if any(name.startswith(p) for p in SECRET_PREFIXES) and name != ".env.example":
        return False
    return True


def package(out_zip: Path, *, dry_run: bool) -> Path:
    print("[3/6] package artifact")
    dockerfile = ROOT / "Dockerfile.microvm"
    if not dockerfile.exists():
        raise DeployError("Dockerfile.microvm not found")
    if dry_run:
        print(f"  [dry-run] would zip repo -> {out_zip.name} (Dockerfile.microvm -> Dockerfile)")
        return out_zip

    n = 0
    # Other Dockerfiles must not collide with the artifact-root "Dockerfile" we
    # just wrote (and the prebaked/agentcore ones aren't used here).
    dockerfile_variants = {"Dockerfile", "Dockerfile.microvm", "Dockerfile.prebaked"}
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        # Dockerfile must be at the artifact root and literally named "Dockerfile".
        zf.write(dockerfile, "Dockerfile")
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or path == dockerfile or path == out_zip:
                continue
            if path.name in dockerfile_variants:
                continue
            top = path.relative_to(ROOT).parts[0]
            if top in EXCLUDE_DIRS:
                continue
            if not _included(path):
                continue
            zf.write(path, str(path.relative_to(ROOT)))
            n += 1
        names = zf.namelist()
    # Secret guard: a real post-condition over the FINAL archive contents. Any
    # dotenv (other than the template) or obvious key material must never ship.
    leaked = [
        a for a in names
        if (Path(a).name.startswith(".env") and Path(a).name != ".env.example")
        or Path(a).name in {"credentials", ".pypirc", "id_rsa", ".netrc"}
    ]
    if leaked:
        out_zip.unlink(missing_ok=True)
        raise DeployError(f"secret guard tripped - refusing to ship: {leaked}")
    size_mb = round(out_zip.stat().st_size / 1e6, 2)
    print(f"  packaged {n + 1} files -> {out_zip.name} ({size_mb} MB)")
    return out_zip


def upload(bucket: str, zip_path: Path, key: str, region: str, *, dry_run: bool) -> str:
    uri = f"s3://{bucket}/{key}"
    _run(["aws", "s3", "cp", str(zip_path), uri, "--region", region], dry_run=dry_run)
    return uri


# ─── Phase 4: image ──────────────────────────────────────────────────────────


def _hooks_config() -> str:
    return json.dumps(
        {
            "port": 9000,
            "microvmImageHooks": {
                "ready": "ENABLED",
                "readyTimeoutInSeconds": 120,
                "validate": "ENABLED",
                "validateTimeoutInSeconds": 120,
            },
            "microvmHooks": {
                # run/resume fire post-snapshot-resume, in-process, under the exec
                # role — where observability.configure_langfuse_runtime resolves
                # /langfuse/* from SSM and attaches the OTLP exporter. 30s gives the
                # cross-region SSM lookups headroom (setup is also idempotently
                # retried on the first traced request as a backstop).
                "run": "ENABLED", "runTimeoutInSeconds": 30,
                "resume": "ENABLED", "resumeTimeoutInSeconds": 30,
                "suspend": "ENABLED", "suspendTimeoutInSeconds": 10,
                "terminate": "ENABLED", "terminateTimeoutInSeconds": 10,
            },
        }
    )


def _image_arn(name: str, account: str, region: str) -> str:
    return f"arn:aws:lambda:{region}:{account}:microvm-image:{name}"


def _env_vars(region: str, account: str) -> str:
    """Baked-in runtime env — no secrets (no Langfuse creds, no ClickHouse creds).

    Bedrock region must match where model access is enabled. The execution-role
    creds reach the guest via IMDSv2, so no AWS keys are baked in. Both the
    ClickHouse federation leg and Langfuse tracing resolve their creds from SSM at
    runtime (via the exec role), so only the SSM *regions* are passed here — never
    the credentials.

    Observability: OTEL_TRACES_EXPORTER stays "none" as the safe default;
    microvm_boot.py flips it to "otlp" only after it successfully resolves
    /langfuse/* from SSM (LANGFUSE_RESOLVE_FROM_SSM=true). LANGFUSE_TRACING_ENVIRONMENT
    is DEV so /invocations links each worker into the coordinator's fan-out trace
    (run_agent_with_tracing); a failed resolve just boots untraced.
    """
    # NB: AWS_REGION / AWS_DEFAULT_REGION are RESERVED keys (rejected by
    # create-microvm-image). The region the guest's IMDS reports is used by the
    # AWS SDK; we pass BEDROCK_REGION explicitly for the Bedrock client (agent.py
    # prefers it), so the inference profile resolves in the deploy region.
    return json.dumps(
        {
            "BEDROCK_REGION": region,
            "BEDROCK_MODEL_ID": BEDROCK_MODEL_ID,
            "CHDB_DATA_PATH": "/app/local_chdb_data",
            "MICROVM_HOOKS_PORT": "9000",
            "IS_PROD": "false",
            # Where the federation warehouse leg looks up ClickHouse Cloud creds
            # (SSM /clickhouse/*) — may differ from the compute region.
            "CLICKHOUSE_SSM_REGION": CLICKHOUSE_SSM_REGION,
            # Distributed-scan cold lake (private S3, read via the exec role).
            "LAKE_BUCKET": f"nyc-taxi-microvm-artifacts-{account}-{region}",
            "LAKE_PREFIX": "lake/yellow",
            # Observability → Langfuse Cloud. Creds resolved from SSM at boot by
            # microvm_boot.py (mirrors the /clickhouse/* pattern); no secret baked.
            "LANGFUSE_RESOLVE_FROM_SSM": "true",
            "LANGFUSE_SSM_REGION": LANGFUSE_SSM_REGION,
            "LANGFUSE_TRACING_ENVIRONMENT": "DEV",
            "OTEL_TRACES_EXPORTER": "none",   # microvm_boot flips to "otlp" on resolve
            "OTEL_METRICS_EXPORTER": "none",
            "DISABLE_ADOT_OBSERVABILITY": "true",
        }
    )


def _list_versions(image_arn: str, region: str) -> list[dict]:
    """Return the image's version records (empty list if the image doesn't exist)."""
    try:
        v = aws("lambda-microvms", "list-microvm-image-versions",
                "--image-identifier", image_arn, region=region, dry_run=False)
    except DeployError:
        return []
    return v.get("items") or v.get("imageVersions") or []


def _vnum(item: dict) -> float:
    """Numeric sort key for an image version (e.g. '2.0' -> 2.0)."""
    try:
        return float(item.get("imageVersion") or item.get("version") or 0)
    except (TypeError, ValueError):
        return 0.0


def create_or_update_image(
    name: str, account: str, region: str, base_arn: str, build_role: str,
    s3_uri: str, *, dry_run: bool,
) -> tuple[str, set[str]]:
    print("[4/6] microvm image")
    image_arn = _image_arn(name, account, region)
    exists = subprocess.run(
        ["aws", "lambda-microvms", "get-microvm-image",
         "--image-identifier", image_arn, "--region", region],
        capture_output=True, text=True,
    )
    # Snapshot version numbers BEFORE mutating, so wait_for_build can wait for the
    # *new* version rather than matching an already-SUCCESSFUL prior one.
    prior = set()
    if exists.returncode == 0 and not dry_run:
        prior = {str(it.get("imageVersion") or it.get("version"))
                 for it in _list_versions(image_arn, region)}
    code_artifact = json.dumps({"uri": s3_uri})
    if exists.returncode == 0:
        print(f"  image exists -> new version: {name}")
        aws("lambda-microvms", "update-microvm-image",
            "--image-identifier", image_arn,
            "--base-image-arn", base_arn,
            "--build-role-arn", build_role,
            "--code-artifact", code_artifact,
            "--hooks", _hooks_config(),
            "--environment-variables", _env_vars(region, account),
            region=region, dry_run=dry_run)
    else:
        print(f"  creating image: {name}")
        aws("lambda-microvms", "create-microvm-image",
            "--name", name,
            "--description", "NYC Taxi analytics agent (chDB) on Lambda MicroVMs",
            "--base-image-arn", base_arn,
            "--build-role-arn", build_role,
            "--code-artifact", code_artifact,
            "--hooks", _hooks_config(),
            "--environment-variables", _env_vars(region, account),
            region=region, dry_run=dry_run)
    return image_arn, prior


def wait_for_build(
    image_arn: str, region: str, prior: set[str], *, dry_run: bool, timeout_s: int = 1800
) -> str:
    """Poll until the *newly built* image version is SUCCESSFUL; return its version.

    The API field is ``imageVersion`` (not ``version``). On an update there are
    multiple versions and the old one is already SUCCESSFUL, so we wait for a
    version that wasn't present before the create/update (``prior``) — otherwise we
    would run the stale image. Falls back to the newest version on a fresh create.
    """
    if dry_run:
        print("  [dry-run] would poll list-microvm-image-versions until SUCCESSFUL")
        return "1.0"
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        items = _list_versions(image_arn, region)
        candidates = [it for it in items
                      if str(it.get("imageVersion") or it.get("version")) not in prior]
        # On an update, the new version row may not be registered yet — keep
        # waiting rather than matching the old SUCCESSFUL one.
        if prior and not candidates:
            time.sleep(10)
            continue
        pool = candidates or items
        if pool:
            newest = max(pool, key=_vnum)
            version = str(newest.get("imageVersion") or newest.get("version") or "1.0")
            state = newest.get("state") or newest.get("versionState")
            if state != last:
                print(f"  build version {version}: {state}")
                last = state
            if state in ("SUCCESSFUL", "ACTIVE", "CREATED"):
                return version
            if state == "FAILED":
                reason = newest.get("stateReason", "see CloudWatch /aws/lambda-microvms/*")
                raise DeployError(f"image build FAILED: {reason}")
        time.sleep(15)
    raise DeployError("image build timed out")


# ─── Phase 5: run ────────────────────────────────────────────────────────────


def run_microvm(
    image_arn: str, version: str, exec_role: str, name: str, region: str, *, dry_run: bool,
) -> tuple[str, str]:
    print("[5/6] run microvm")
    idle = json.dumps(
        {"maxIdleDurationSeconds": 900, "suspendedDurationSeconds": 1800, "autoResumeEnabled": True}
    )
    logging_cfg = json.dumps({"cloudWatch": {"logGroup": f"/aws/lambda-microvms/{name}"}})
    resp = aws("lambda-microvms", "run-microvm",
               "--image-identifier", image_arn,
               "--image-version", version,
               "--execution-role-arn", exec_role,
               "--idle-policy", idle,
               "--maximum-duration-in-seconds", "28800",
               "--logging", logging_cfg,
               region=region, dry_run=dry_run)
    if dry_run:
        return "microvm-DRYRUN", "DRYRUN.lambda-microvm.on.aws"
    microvm_id = resp["microvmId"]
    endpoint = resp["endpoint"]
    print(f"  microvmId={microvm_id}")
    print(f"  endpoint={endpoint}")
    return microvm_id, endpoint


# ─── Phase 6: verify ─────────────────────────────────────────────────────────


def _auth_token(microvm_id: str, region: str) -> str:
    resp = aws("lambda-microvms", "create-microvm-auth-token",
               "--microvm-identifier", microvm_id,
               "--expiration-in-minutes", "30",
               "--allowed-ports", json.dumps([{"port": 8080}]),
               region=region, dry_run=False)
    # The token lives under authToken or tokenParts depending on API shape.
    for container_key in ("authToken", "tokenParts", "TokenParts"):
        container = resp.get(container_key)
        if isinstance(container, dict) and "X-aws-proxy-auth" in container:
            return container["X-aws-proxy-auth"]
    raise DeployError(f"could not find X-aws-proxy-auth in token response: {list(resp)}")


def _call(endpoint: str, path: str, token: str, *, method: str = "GET",
          body: dict | None = None, timeout: float = 60.0) -> tuple[int, str]:
    url = f"https://{endpoint}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-aws-proxy-auth", token)
    req.add_header("X-aws-proxy-port", "8080")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def verify(microvm_id: str, endpoint: str, region: str, *, dry_run: bool) -> bool:
    print("[6/6] verify")
    if dry_run:
        print("  [dry-run] would mint a token and call /ping, /health, /chat")
        return True
    # The MicroVM is ready when ingress succeeds (state lags); retry the token+call.
    token = None
    for attempt in range(20):
        try:
            token = token or _auth_token(microvm_id, region)
            status, body = _call(endpoint, "/ping", token, timeout=15)
            if status == 200:
                print(f"  /ping -> {status} {body[:80]}")
                break
        except Exception as exc:  # noqa: BLE001 — endpoint warming up
            if attempt % 4 == 0:
                print(f"  waiting for ingress ({attempt}) ... {str(exc)[:80]}")
        time.sleep(6)
    else:
        raise DeployError("MicroVM endpoint never accepted traffic")

    s_health, b_health = _call(endpoint, "/health", token)
    print(f"  /health -> {s_health} {b_health[:120]}")

    s_chat, b_chat = _call(
        endpoint, "/chat", token, method="POST",
        body={"text": "Busiest hour of the day? One sentence."}, timeout=90,
    )
    print(f"  /chat -> {s_chat}")
    try:
        print(f"  answer: {json.loads(b_chat)['response'][:200]}")
    except Exception:  # noqa: BLE001
        print(f"  body: {b_chat[:200]}")
    return s_health == 200 and s_chat == 200


# ─── teardown helper ─────────────────────────────────────────────────────────


def terminate_all(region: str) -> int:
    print("terminate: listing running MicroVMs")
    listing = aws("lambda-microvms", "list-microvms", region=region, dry_run=False)
    items = listing.get("items") or listing.get("microvms") or []
    n = 0
    for mv in items:
        mid = mv.get("microvmId")
        state = mv.get("state")
        if mid and state not in ("TERMINATED", "TERMINATING"):
            aws("lambda-microvms", "terminate-microvm", "--microvm-identifier", mid,
                region=region, dry_run=False)
            print(f"  terminating {mid} ({state})")
            n += 1
    print(f"  terminated {n} MicroVM(s)")
    return n


# ─── main ────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Deploy NYC Taxi agent to Lambda MicroVMs")
    ap.add_argument("--region", default=os.getenv("AWS_REGION", DEFAULT_REGION))
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--data-mode", choices=["sample", "full"], default="sample",
                    help="(informational) — the image bakes DATA_MODE at build time")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep", action="store_true", help="don't print the teardown hint")
    ap.add_argument("--terminate-only", action="store_true")
    args = ap.parse_args()

    region = args.region
    try:
        if args.terminate_only:
            terminate_all(region)
            return 0

        account = preflight(region)
        base_arn = managed_base_arn(region) if not args.dry_run else f"arn:aws:lambda:{region}:aws:microvm-image:al2023-1"
        bucket = ensure_bucket(account, region, dry_run=args.dry_run)
        build_role, exec_role = ensure_roles(account, region, bucket, dry_run=args.dry_run)

        zip_path = ROOT / ".microvm_artifact.zip"
        package(zip_path, dry_run=args.dry_run)
        key = f"microvm-images/{args.name}/code-artifact.zip"
        s3_uri = upload(bucket, zip_path, key, region, dry_run=args.dry_run)

        image_arn, prior_versions = create_or_update_image(
            args.name, account, region, base_arn, build_role, s3_uri, dry_run=args.dry_run
        )
        version = wait_for_build(image_arn, region, prior_versions, dry_run=args.dry_run)
        microvm_id, endpoint = run_microvm(
            image_arn, version, exec_role, args.name, region, dry_run=args.dry_run
        )
        ok = verify(microvm_id, endpoint, region, dry_run=args.dry_run)

        if not args.dry_run:
            zip_path.unlink(missing_ok=True)
        print("\n" + ("=" * 60))
        print(f"RESULT: {'PASS' if ok else 'FAIL'}")
        if not args.dry_run and not args.keep:
            print(f"\nMicroVM: {microvm_id}  endpoint: https://{endpoint}")
            print("Lifecycle:")
            print(f"  aws lambda-microvms suspend-microvm   --microvm-identifier {microvm_id} --region {region}")
            print(f"  aws lambda-microvms resume-microvm    --microvm-identifier {microvm_id} --region {region}")
            print(f"  aws lambda-microvms terminate-microvm --microvm-identifier {microvm_id} --region {region}")
            print(f"Teardown all: python scripts/deploy_microvm.py --terminate-only --region {region}")
        return 0 if ok else 1
    except DeployError as exc:
        print(f"\nDEPLOY ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
