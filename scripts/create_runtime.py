#!/usr/bin/env python3
"""scripts/create_runtime.py — SDK-direct AgentCore Runtime creation.

Mirrors scripts/create_memory.py but evolves to a main()+argparse entrypoint
to support the --dry-run and --environment flags.

No @aws/agentcore CLI dependency. One-shot create via boto3
bedrock-agentcore-control:CreateAgentRuntime. Tri-state idempotent:
CLEAR (no runtime) / EXISTS (matches planned) / COLLISION (diverges).

Upstream inputs resolved at runtime:
- Container URI: ECR image at digest (not :latest tag).
- Role ARN: /agentcore/AGENT_EXECUTION_ROLE_ARN from SSM.
- Subnets: /nyctaxi/network/PRIVATE_SUBNET_IDS from SSM.
- Security groups: /nyctaxi/network/EFS_SG_ID from SSM.
- Memory ID: /agentcore/AGENTCORE_MEMORY_ID from SSM.
- Env vars: /langfuse/* SSM + static config (10 PRD vars).
- ClickHouse Cloud (optional): /clickhouse/* SSM → CLICKHOUSE_* env (federation
  warehouse leg; omitted gracefully when the params are absent).

Invocation contract:
  python3 scripts/create_runtime.py [--environment PRD|DEV|TST] [--dry-run]

Exit codes:
  0  CLEAR+created (polled to READY) OR EXISTS+no-op
  2  COLLISION (existing runtime diverges from planned)
  3  Timeout (polling exceeded POLL_TIMEOUT_SECONDS)
  4  CREATE_FAILED (service reported terminal failure)

Credential-liveness check runs first in main() before any resolve/mutate.
Tri-state idempotency probe.
"""
import argparse
import base64
import json
import os
import re
import sys
import time
from typing import Optional

import boto3


# Constants
# Region is configurable but AgentCore Runtime, AgentCore Memory and the
# `us.` cross-region inference profile are US-region constructs — us-east-1 is
# the validated default. EXPECTED_ACCOUNT is now an OPTIONAL guard: if unset,
# the script runs in whatever account the caller's credentials resolve to
# (this is what lets a fresh account deploy). Set EXPECTED_ACCOUNT to re-enable
# the strict account-match guard.
REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
EXPECTED_ACCOUNT = os.getenv("EXPECTED_ACCOUNT")  # None ⇒ accept caller's account
ECR_REPO_NAME = "nyc-taxi-agent"           # from cdk/stacks/ecr_stack.py
POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 600

# Payload Redaction Contract — deny-list regex.
# Authoritative source for SECRET-class env-var key matching. Lifted from
# d12_c6_build_envfile::SECRET_PATTERN precedent verbatim — already covers
# the current 22-key env-var set per Phase A gap analysis. Future
# env-var additions whose key matches this pattern are redacted
# automatically (closes the allow-list-brittleness class that
# caused D-D15-OTLP-TRACES-HEADERS-REDACT-GAP).
# LANGFUSE_PUBLIC_KEY is handled separately as AMBIGUOUS-class (partial-redact
# preserving the pk-lf- identifying prefix; must be checked BEFORE the
# deny-list match to avoid over-redaction, since the public key name
# contains "key" substring-wise but does NOT match this pattern).
SECRET_KEY_PATTERN = re.compile(
    r"secret|password|authorization|otlp_headers|langfuse_secret|traces_headers",
    re.IGNORECASE,
)

# Exit codes
EXIT_OK = 0
EXIT_COLLISION = 2
EXIT_TIMEOUT = 3
EXIT_CREATE_FAILED = 4


def d019_cred_check() -> tuple[bool, str]:
    """Credential liveness probe. Returns (ok, detail)."""
    try:
        sts = boto3.client("sts", region_name=REGION)
        resp = sts.get_caller_identity()
        account = resp.get("Account")
        if EXPECTED_ACCOUNT and account != EXPECTED_ACCOUNT:
            return False, f"account mismatch: got {account}, expected {EXPECTED_ACCOUNT}"
        return True, f"account={account}, region={REGION}, arn={resp.get('Arn')}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def resolve_container_uri() -> str:
    """Return the ECR image URI at digest form."""
    ecr = boto3.client("ecr", region_name=REGION)
    resp = ecr.describe_images(repositoryName=ECR_REPO_NAME)
    images = resp.get("imageDetails", [])
    if not images:
        raise RuntimeError(f"ECR repository {ECR_REPO_NAME!r} has no images")
    # Prefer the image tagged "latest"; fall back to most-recently-pushed.
    tagged = [img for img in images if "latest" in (img.get("imageTags") or [])]
    target = tagged[0] if tagged else max(images, key=lambda i: i.get("imagePushedAt"))
    digest = target["imageDigest"]
    registry_id = target.get("registryId") or boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    return f"{registry_id}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO_NAME}@{digest}"


def resolve_role_arn() -> str:
    """Fetch AgentExecutionRole ARN from /agentcore/AGENT_EXECUTION_ROLE_ARN."""
    ssm = boto3.client("ssm", region_name=REGION)
    resp = ssm.get_parameter(Name="/agentcore/AGENT_EXECUTION_ROLE_ARN")
    return resp["Parameter"]["Value"]


def resolve_subnets() -> list[str]:
    """Return private subnet ids from /nyctaxi/network/PRIVATE_SUBNET_IDS.

    The parameter is a StringList type; boto3 returns its Value as a comma-joined
    string which we split on commas. NetworkStack is the producer (cdk deploy NycTaxiNetwork).
    """
    ssm = boto3.client("ssm", region_name=REGION)
    resp = ssm.get_parameter(Name="/nyctaxi/network/PRIVATE_SUBNET_IDS")
    return sorted([s for s in resp["Parameter"]["Value"].split(",") if s])


def resolve_security_groups() -> list[str]:
    """Return the EFS security group id from /nyctaxi/network/EFS_SG_ID.

    NetworkStack is the producer (cdk deploy NycTaxiNetwork).
    """
    ssm = boto3.client("ssm", region_name=REGION)
    resp = ssm.get_parameter(Name="/nyctaxi/network/EFS_SG_ID")
    return [resp["Parameter"]["Value"]]


def resolve_agentcore_memory_id() -> str:
    """Fetch AgentCore Memory id from /agentcore/AGENTCORE_MEMORY_ID."""
    ssm = boto3.client("ssm", region_name=REGION)
    resp = ssm.get_parameter(Name="/agentcore/AGENTCORE_MEMORY_ID")
    return resp["Parameter"]["Value"]


def resolve_langfuse_env() -> dict[str, str]:
    """Fetch /langfuse/* SSM params + build OTEL header block (mirrors deploy.sh shape)."""
    ssm = boto3.client("ssm", region_name=REGION)
    host = ssm.get_parameter(Name="/langfuse/LANGFUSE_HOST")["Parameter"]["Value"]
    pk = ssm.get_parameter(Name="/langfuse/LANGFUSE_PUBLIC_KEY")["Parameter"]["Value"]
    sk = ssm.get_parameter(Name="/langfuse/LANGFUSE_SECRET_KEY", WithDecryption=True)["Parameter"]["Value"]
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    return {
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"{host}/api/public/otel",
        "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Basic {auth},x-langfuse-ingestion-version=4",
        "LANGFUSE_HOST": host,
        "LANGFUSE_PUBLIC_KEY": pk,
        "LANGFUSE_SECRET_KEY": sk,
    }


def resolve_clickhouse_env() -> dict[str, str]:
    """Fetch optional /clickhouse/* SSM params for the federation warehouse leg.

    All-or-nothing: returns the three CLICKHOUSE_* vars only when all are present,
    else {} — so a deploy without a configured ClickHouse Cloud service still
    succeeds. The federation tool's ClickHouse Cloud leg (remoteSecure) is then
    skipped at runtime when CLICKHOUSE_PASSWORD is unset, exactly as in local-dev
    (graceful absence, mirroring the optional AGENTCORE_MEMORY_ID posture).

    Resolved with the deploying principal's credentials (like resolve_langfuse_env);
    the values are baked into environmentVariables, so the runtime role needs no
    SSM access of its own. The password parameter is a SecureString (decrypted
    here) and is redacted from --dry-run output by SECRET_KEY_PATTERN.
    """
    ssm = boto3.client("ssm", region_name=REGION)
    try:
        url = ssm.get_parameter(Name="/clickhouse/CLICKHOUSE_URL")["Parameter"]["Value"]
        user = ssm.get_parameter(Name="/clickhouse/CLICKHOUSE_USER")["Parameter"]["Value"]
        pwd = ssm.get_parameter(
            Name="/clickhouse/CLICKHOUSE_PASSWORD", WithDecryption=True
        )["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return {}
    return {
        "CLICKHOUSE_URL": url,
        "CLICKHOUSE_USER": user,
        "CLICKHOUSE_PASSWORD": pwd,
    }


def build_environment_variables(environment: str, runtime_id: Optional[str] = None) -> dict[str, str]:
    """Assemble env vars for AgentCore runtime.

 FIX-CANONICAL-OBSERVABILITY env-var block:
      - Non-ID-dependent vars (always present): 18 for PRD, 21 for DEV/TST.
      - ID-dependent var (only when runtime_id provided, +1):
        OTEL_EXPORTER_OTLP_LOGS_HEADERS — built from runtime-ID-bearing canonical
        log-group path.

    Signal-specific OTLP routing:
      - OTEL_EXPORTER_OTLP_TRACES_{ENDPOINT,HEADERS} → Langfuse.
      - OTEL_EXPORTER_OTLP_LOGS_{ENDPOINT,HEADERS} → CloudWatch OTLP collector-less
        endpoint, SigV4-signed by aws-opentelemetry-distro.

    Removed vs pre- block (2): OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_HEADERS
      (generic endpoint ambiguous under auto-instrumentation; replaced by signal-specific).

    Flipped vs pre- (1): DISABLE_ADOT_OBSERVABILITY "true" → "false"
.

    Create-path note: runtime_id is None pre-CreateAgentRuntime (the ID suffix is
    AWS-assigned). ID-dependent vars must be bound via post-create UpdateAgentRuntime
    for fresh-create flows (see D-D12-ID-DEPENDENT-ENVVAR-CREATE-PATH-GAP). updates
    an existing runtime → ID known → full 19-var block lands in one UpdateAgentRuntime.
    """
    langfuse = resolve_langfuse_env()
    memory_id = resolve_agentcore_memory_id()
    langfuse_otel_base = langfuse["OTEL_EXPORTER_OTLP_ENDPOINT"]  # e.g. https://cloud.langfuse.com/api/public/otel
    env_vars = {
        # 8 original non-OTEL retained (alphabetical-ish; historical order preserved)
        "BEDROCK_MODEL_ID": "us.anthropic.claude-sonnet-4-20250514-v1:0",
        "AWS_REGION": REGION,
        "CHDB_DATA_PATH": "/app/local_chdb_data",
        "WEATHER_MOUNT_PATH": "/mnt/noaa-gsod",
        "IS_PROD": "true" if environment == "PRD" else "false",
        "LANGFUSE_TRACING_ENVIRONMENT": environment,
        "DISABLE_ADOT_OBSERVABILITY": "false",  # flip: was "true" pre-
        "AGENTCORE_MEMORY_ID": memory_id,
        # canonical observability recipe — 10 non-ID-dependent OTEL additions
        "AGENT_OBSERVABILITY_ENABLED": "true",
        "OTEL_PYTHON_DISTRO": "aws_distro",
        "OTEL_PYTHON_CONFIGURATOR": "aws_configurator",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"{langfuse_otel_base}/v1/traces",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": langfuse["OTEL_EXPORTER_OTLP_HEADERS"],
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": f"https://logs.{REGION}.amazonaws.com/v1/logs",
        "OTEL_RESOURCE_ATTRIBUTES": (
            f"service.name=nyc-taxi-agent,"
            f"service.namespace=bedrock-agentcore,"
            f"deployment.environment={environment}"
        ),
    }
    if runtime_id is not None:
        # ID-dependent: canonical log-group + stream routing headers for CloudWatch OTLP.
        # Log-group path empirically verified at A.1: <runtime-id>-DEFAULT (endpoint
        # name literally "DEFAULT"; stream name otel-rt-logs per materialization).
        env_vars["OTEL_EXPORTER_OTLP_LOGS_HEADERS"] = (
            f"x-aws-log-group=/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT,"
            f"x-aws-log-stream=otel-rt-logs"
        )
    if environment != "PRD":
        env_vars["LANGFUSE_PUBLIC_KEY"] = langfuse["LANGFUSE_PUBLIC_KEY"]
        env_vars["LANGFUSE_SECRET_KEY"] = langfuse["LANGFUSE_SECRET_KEY"]
        env_vars["LANGFUSE_HOST"] = langfuse["LANGFUSE_HOST"]
    # Optional ClickHouse Cloud federation leg (graceful when /clickhouse/* absent).
    env_vars.update(resolve_clickhouse_env())
    return env_vars


def build_create_payload(environment: str) -> dict:
    """Assemble the full CreateAgentRuntime request body. Returns dict for **kwargs."""
    return {
        "agentRuntimeName": f"nyc_taxi_agent_{environment}",
        "description": f"NYC Taxi Analytics Agent — {environment} runtime",
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": resolve_container_uri()},
        },
        "roleArn": resolve_role_arn(),
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {
                "subnets": resolve_subnets(),
                "securityGroups": resolve_security_groups(),
            },
        },
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "environmentVariables": build_environment_variables(environment),
    }


def _redact_payload(payload: dict) -> dict:
    """Return a deep-copy of the payload with SECRET-class env-var values masked.

    Implements the Payload Redaction Contract:
      - Deny-list regex model: any env-var whose key matches SECRET_KEY_PATTERN
        gets its value replaced with "[redacted]". Supersedes the pre-
        2-key allow-list which failed at env-var schema drift
        (D-D15-OTLP-TRACES-HEADERS-REDACT-GAP).
      - AMBIGUOUS-class special case: LANGFUSE_PUBLIC_KEY is tenant-fingerprint
        sensitive but not auth-equivalent. Checked BEFORE the deny-list match so
        the partial-redact (pk-lf- prefix preserved, remainder stripped) is
        applied cleanly; the key's "_KEY" substring does NOT match the current
        pattern, but this ordering is a defensive invariant regardless.
      - Mutation safety: returns a deep-copy via json round-trip; input is not
        modified.
    """
    redacted = json.loads(json.dumps(payload))  # deep copy
    env = redacted.get("environmentVariables", {})
    for key in list(env.keys()):
        # AMBIGUOUS-class special case: check FIRST to preserve partial-redact.
        if key == "LANGFUSE_PUBLIC_KEY" and isinstance(env[key], str) and env[key].startswith("pk-lf-"):
            env[key] = env[key][:6] + "...[redacted]"
            continue
        # SECRET-class deny-list: any matching key → full replace.
        if SECRET_KEY_PATTERN.search(key):
            env[key] = "[redacted]"
    return redacted


def check_runtime_state(client, agent_runtime_name: str, planned_payload: dict) -> tuple[str, Optional[dict]]:
    """Tri-state probe: CLEAR / EXISTS / COLLISION.

    Returns:
        ("CLEAR", None)                — no runtime with target name; safe to create.
        ("EXISTS", get_response_dict)  — matching runtime found (container URI + role + net match).
        ("COLLISION", diff_dict)       — runtime with same name but divergent shape.
    """
    resp = client.list_agent_runtimes(maxResults=100)
    for summary in resp.get("agentRuntimes", []):
        if summary.get("agentRuntimeName") == agent_runtime_name:
            got = client.get_agent_runtime(agentRuntimeId=summary["agentRuntimeId"])
            diff = {}
            got_uri = got.get("agentRuntimeArtifact", {}).get("containerConfiguration", {}).get("containerUri")
            planned_uri = planned_payload["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
            if got_uri != planned_uri:
                diff["containerUri"] = {"got": got_uri, "planned": planned_uri}
            if got.get("roleArn") != planned_payload["roleArn"]:
                diff["roleArn"] = {"got": got.get("roleArn"), "planned": planned_payload["roleArn"]}
            got_net = got.get("networkConfiguration", {}).get("networkModeConfig", {})
            planned_net = planned_payload["networkConfiguration"]["networkModeConfig"]
            if sorted(got_net.get("subnets", [])) != sorted(planned_net["subnets"]):
                diff["subnets"] = {"got": got_net.get("subnets"), "planned": planned_net["subnets"]}
            if sorted(got_net.get("securityGroups", [])) != sorted(planned_net["securityGroups"]):
                diff["securityGroups"] = {"got": got_net.get("securityGroups"), "planned": planned_net["securityGroups"]}
            if diff:
                return "COLLISION", diff
            return "EXISTS", got
    return "CLEAR", None


def create_runtime(client, payload: dict) -> dict:
    """Call CreateAgentRuntime and return the response dict."""
    return client.create_agent_runtime(**payload)


def _runtime_id_by_name(client, agent_runtime_name: str) -> Optional[str]:
    """Return the runtime id for a name, or None."""
    resp = client.list_agent_runtimes(maxResults=100)
    for summary in resp.get("agentRuntimes", []):
        if summary.get("agentRuntimeName") == agent_runtime_name:
            return summary["agentRuntimeId"]
    return None


def update_runtime(client, agent_runtime_id: str, environment: str) -> dict:
    """Call UpdateAgentRuntime for an existing runtime.

    Rebuilds the payload and — crucially — passes runtime_id into
    build_environment_variables so the ID-dependent OTEL_EXPORTER_OTLP_LOGS_HEADERS
    var is included (it cannot be set on the create path; the runtime id does not
    exist yet — see D-D12-ID-DEPENDENT-ENVVAR-CREATE-PATH-GAP). UpdateAgentRuntime
    keys on agentRuntimeId, not agentRuntimeName.
    """
    payload = build_create_payload(environment)
    payload.pop("agentRuntimeName", None)
    payload["agentRuntimeId"] = agent_runtime_id
    payload["environmentVariables"] = build_environment_variables(environment, runtime_id=agent_runtime_id)
    return client.update_agent_runtime(**payload)


def poll_until_ready(client, agent_runtime_id: str, timeout_seconds: int = POLL_TIMEOUT_SECONDS) -> dict:
    """Poll GetAgentRuntime until status∈{READY, CREATE_FAILED} or timeout.

    Logs every status transition. Raises TimeoutError on timeout.
    """
    deadline = time.time() + timeout_seconds
    last_status = None
    while time.time() < deadline:
        resp = client.get_agent_runtime(agentRuntimeId=agent_runtime_id)
        status = resp.get("status")
        if status != last_status:
            print(f"  [{time.strftime('%H:%M:%S')}] status={status}", flush=True)
            last_status = status
        if status in ("READY", "CREATE_FAILED"):
            return resp
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"polling exceeded {timeout_seconds}s; last status={last_status!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--environment", default="PRD", choices=["PRD", "DEV", "TST"])
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve inputs + build payload + print redacted; exit before CreateAgentRuntime")
    ap.add_argument("--update", action="store_true",
                    help="update an existing runtime in place (UpdateAgentRuntime) instead of "
                         "erroring on COLLISION / no-op on EXISTS — use after pushing a new image")
    args = ap.parse_args()

    # first — fail fast on bad creds
    ok, detail = d019_cred_check()
    if not ok:
        print
        return EXIT_CREATE_FAILED
    print

    payload = build_create_payload(args.environment)
    agent_runtime_name = payload["agentRuntimeName"]

    if args.dry_run:
        redacted_payload = _redact_payload(payload)
        print(f"--dry-run: resolved payload for {agent_runtime_name!r}:")
        print(json.dumps(redacted_payload, indent=2))
        dryrun_artifact_path = "/tmp/gate14_dryrun_payload.json"
        with open(dryrun_artifact_path, "w") as f:
            json.dump(redacted_payload, f, indent=2, sort_keys=True)
        print(f"[dry-run] redacted payload written to {dryrun_artifact_path}", file=sys.stderr)
        print("--dry-run: exit before CreateAgentRuntime")
        return EXIT_OK

    client = boto3.client("bedrock-agentcore-control", region_name=REGION)
    state, detail = check_runtime_state(client, agent_runtime_name, payload)
    print(f"idempotency probe: {state}")

    # --update: update an existing runtime in place (handles both EXISTS and COLLISION).
    if state in ("EXISTS", "COLLISION") and args.update:
        rid = detail.get("agentRuntimeId") if state == "EXISTS" else _runtime_id_by_name(client, agent_runtime_name)
        if not rid:
            print("--update: could not resolve runtime id", file=sys.stderr)
            return EXIT_CREATE_FAILED
        print(f"--update: updating runtime {agent_runtime_name!r} (id={rid})...")
        update_runtime(client, rid, args.environment)
        try:
            final = poll_until_ready(client, rid)
        except TimeoutError as e:
            print(f"TIMEOUT: {e}", file=sys.stderr)
            return EXIT_TIMEOUT
        if final.get("status") == "READY":
            print(f"READY (updated): arn={final.get('agentRuntimeArn')}")
            return EXIT_OK
        print(f"UPDATE_FAILED: final_status={final.get('status')}", file=sys.stderr)
        return EXIT_CREATE_FAILED

    if state == "COLLISION":
        print("COLLISION diff (existing runtime diverges from planned); "
              "re-run with --update to update it in place:", file=sys.stderr)
        print(json.dumps(detail, indent=2, default=str), file=sys.stderr)
        return EXIT_COLLISION

    if state == "EXISTS":
        print(f"EXISTS: runtime already present, skipping create. arn={detail.get('agentRuntimeArn')}")
        return EXIT_OK

    # CLEAR → create + poll
    print(f"CLEAR: creating runtime {agent_runtime_name!r}...")
    create_resp = create_runtime(client, payload)
    agent_runtime_id = create_resp["agentRuntimeId"]
    agent_runtime_arn = create_resp["agentRuntimeArn"]
    print(f"created: id={agent_runtime_id}, arn={agent_runtime_arn}, initial status={create_resp.get('status')}")

    try:
        final = poll_until_ready(client, agent_runtime_id)
    except TimeoutError as e:
        print(f"TIMEOUT: {e}", file=sys.stderr)
        return EXIT_TIMEOUT

    if final.get("status") != "READY":
        print(f"CREATE_FAILED: arn={agent_runtime_arn}, final_status={final.get('status')}", file=sys.stderr)
        return EXIT_CREATE_FAILED

    # Bind the ID-dependent observability env var (OTEL_EXPORTER_OTLP_LOGS_HEADERS needs the
    # runtime id, only known post-create) via a follow-up UpdateAgentRuntime — parity with the
    # live PRD runtime. Non-fatal: the runtime is already READY if this step fails.
    try:
        print("binding ID-dependent observability env vars via UpdateAgentRuntime...")
        update_runtime(client, agent_runtime_id, args.environment)
        poll_until_ready(client, agent_runtime_id)
        print("observability env bound.")
    except Exception as e:  # noqa: BLE001 — observability bind must never mask a successful create
        print(f"WARNING: post-create observability bind failed (runtime is READY regardless): {e}",
              file=sys.stderr)

    print(f"READY: arn={agent_runtime_arn}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
