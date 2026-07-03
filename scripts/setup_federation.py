#!/usr/bin/env python3
"""scripts/setup_federation.py — post-deploy wiring for the Aurora federation leg.

Runs after `cdk deploy NycTaxiFederation`. Using the /federation/* SSM handles the
stack published, it:

  1. Creates the Lambda MicroVMs VPC egress network connector (`lambda-core`, not
     lambda-microvms) bound to the stack's private subnets + connector SG, and
     polls it to ACTIVE.
  2. Seeds Aurora over the RDS Data API (HTTPS — no VPC access needed): creates the
     `taxi_zones` table, loads the 265-row lookup, and creates a read-only app user.
  3. Publishes /postgres/* to SSM (us-west-2) for the app's cloud_sources.pg_config,
     plus /federation/EGRESS_CONNECTOR_ARN for the agentic console.

Idempotent and safe to re-run. Region defaults to us-west-2 (the fleet's region).

Usage:
  python scripts/setup_federation.py --region us-west-2
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "scripts" / "postgres_init" / "taxi_zone_lookup.csv"
FED_PREFIX = "/federation"
PG_PREFIX = "/postgres"
DB_NAME = "nyctaxi"
APP_USER = "taxi_ro"


def _ssm_get(ssm, name: str, decrypt: bool = False) -> str:
    return ssm.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]


def _ssm_put(ssm, name: str, value: str, secure: bool = False) -> None:
    ssm.put_parameter(
        Name=name, Value=value,
        Type="SecureString" if secure else "String", Overwrite=True)


# ── 1. egress network connector (lambda-core; shelled — newish service) ────────

def create_connector(region: str, subnets: list[str], sg: str, operator_role: str) -> str:
    """Create (or reuse) the VPC egress connector; return its ARN once ACTIVE."""
    existing = _find_connector(region)
    if existing:
        print(f"  connector exists: {existing}")
        return _wait_active(region, existing)

    cfg = json.dumps({"VpcEgressConfiguration": {
        "SubnetIds": subnets, "SecurityGroupIds": [sg],
        "NetworkProtocol": "IPv4", "AssociatedComputeResourceTypes": ["MicroVm"]}})
    out = _aws("lambda-core", "create-network-connector",
               "--name", "nyctaxi-microvm-egress",
               "--configuration", cfg,
               "--operator-role", operator_role, region=region)
    arn = out["Arn"]
    print(f"  created connector: {arn}")
    return _wait_active(region, arn)


def _find_connector(region: str) -> str | None:
    try:
        out = _aws("lambda-core", "list-network-connectors", region=region)
    except Exception:  # noqa: BLE001
        return None
    for c in out.get("NetworkConnectors", []):
        if c.get("Name") == "nyctaxi-microvm-egress":
            return c.get("Arn")
    return None


def _wait_active(region: str, arn: str, timeout_s: int = 300) -> str:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        out = _aws("lambda-core", "get-network-connector",
                   "--identifier", arn, region=region)
        state = out.get("State")
        if state != last:
            print(f"    connector state: {state}")
            last = state
        if state == "ACTIVE":
            return arn
        if state in ("FAILED", "DELETE_FAILED"):
            raise RuntimeError(
                f"connector {arn} -> {state}: {out.get('StateReason')}")
        time.sleep(6)
    raise TimeoutError(f"connector not ACTIVE within {timeout_s}s (last={last})")


# ── 2. seed Aurora via the Data API ───────────────────────────────────────────

def seed_aurora(region: str, cluster_arn: str, secret_arn: str, app_password: str) -> None:
    """Create the taxi_zones table, load the lookup, and a read-only app user."""
    rds = boto3.client("rds-data", region_name=region)

    def exe(sql: str, params: list | None = None) -> dict:
        return _data_api_retry(rds, cluster_arn, secret_arn, sql, params)

    exe("CREATE TABLE IF NOT EXISTS taxi_zones ("
        "location_id INTEGER PRIMARY KEY, borough TEXT, zone TEXT, service_zone TEXT)")
    exe("TRUNCATE taxi_zones")

    rows = _load_csv()
    for chunk in _chunks(rows, 100):
        rds.batch_execute_statement(
            resourceArn=cluster_arn, secretArn=secret_arn, database=DB_NAME,
            sql=("INSERT INTO taxi_zones (location_id, borough, zone, service_zone) "
                 "VALUES (:location_id, :borough, :zone, :service_zone)"),
            parameterSets=chunk)
    print(f"  loaded {len(rows)} taxi_zones rows")

    # Read-only app user (least privilege). The password is alphanumeric (see
    # main(): url-safe token with -/_ mapped out), so it is safe to inline in a
    # single-quoted literal — Data API can't bind params inside role DDL. CREATE
    # first (ignore "already exists"), then ALTER to set/rotate the password.
    try:
        exe(f"CREATE ROLE {APP_USER} LOGIN PASSWORD '{app_password}'")
    except Exception as e:  # noqa: BLE001
        if "already exists" not in str(e):
            raise
    exe(f"ALTER ROLE {APP_USER} WITH LOGIN PASSWORD '{app_password}'")
    exe(f"GRANT CONNECT ON DATABASE {DB_NAME} TO {APP_USER}")
    exe(f"GRANT USAGE ON SCHEMA public TO {APP_USER}")
    exe(f"GRANT SELECT ON taxi_zones TO {APP_USER}")
    print(f"  ensured read-only user {APP_USER!r} with SELECT on taxi_zones")


def _data_api_retry(rds, cluster_arn, secret_arn, sql, params, tries: int = 6) -> dict:
    """Execute one statement; retry while Aurora resumes from 0 ACU (~15s cold)."""
    last = None
    for i in range(tries):
        try:
            return rds.execute_statement(
                resourceArn=cluster_arn, secretArn=secret_arn, database=DB_NAME,
                sql=sql, parameters=params or [])
        except rds.exceptions.DatabaseResumingException as e:  # cold-start wake
            last = e
            print(f"    Aurora resuming… retry {i + 1}")
            time.sleep(12)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "resuming" in msg.lower() or "not currently available" in msg.lower():
                last = e
                time.sleep(12)
                continue
            raise
    raise RuntimeError(f"Data API statement failed after retries: {last}")


def _load_csv() -> list[list[dict]]:
    with CSV_PATH.open() as f:
        reader = csv.DictReader(f)
        out = []
        for r in reader:
            out.append([
                {"name": "location_id", "value": {"longValue": int(r["LocationID"])}},
                {"name": "borough", "value": {"stringValue": r["Borough"]}},
                {"name": "zone", "value": {"stringValue": r["Zone"]}},
                {"name": "service_zone", "value": {"stringValue": r["service_zone"]}},
            ])
        return out


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


# ── AWS CLI shell (lambda-core; boto3 may lack it) ─────────────────────────────

def _aws(*args: str, region: str) -> dict:
    out = subprocess.run(["aws", *args, "--region", region, "--output", "json"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "aws failed").strip())
    body = (out.stdout or "").strip()
    return json.loads(body) if body else {}


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Post-deploy wiring for the Aurora federation leg")
    ap.add_argument("--region", default=os.getenv("AWS_REGION", "us-west-2"))
    args = ap.parse_args()
    region = args.region
    ssm = boto3.client("ssm", region_name=region)

    print("[1/4] resolve stack handles")
    subnets = _ssm_get(ssm, f"{FED_PREFIX}/PRIVATE_SUBNET_IDS").split(",")
    sg = _ssm_get(ssm, f"{FED_PREFIX}/CONNECTOR_SG_ID")
    operator_role = _ssm_get(ssm, f"{FED_PREFIX}/CONNECTOR_OPERATOR_ROLE_ARN")
    secret_arn = _ssm_get(ssm, f"{FED_PREFIX}/AURORA_SECRET_ARN")
    cluster_arn = _ssm_get(ssm, f"{FED_PREFIX}/AURORA_CLUSTER_ARN")
    endpoint = _ssm_get(ssm, f"{FED_PREFIX}/AURORA_ENDPOINT")

    print("[2/4] egress network connector")
    connector_arn = create_connector(region, subnets, sg, operator_role)

    print("[3/4] seed Aurora (Data API)")
    app_password = secrets.token_urlsafe(24).replace("-", "x").replace("_", "y")
    seed_aurora(region, cluster_arn, secret_arn, app_password)

    print("[4/4] publish /postgres/* + connector ARN to SSM")
    _ssm_put(ssm, f"{PG_PREFIX}/POSTGRES_HOST", endpoint)
    _ssm_put(ssm, f"{PG_PREFIX}/POSTGRES_PORT", "5432")
    _ssm_put(ssm, f"{PG_PREFIX}/POSTGRES_DB", DB_NAME)
    _ssm_put(ssm, f"{PG_PREFIX}/POSTGRES_USER", APP_USER)
    _ssm_put(ssm, f"{PG_PREFIX}/POSTGRES_PASSWORD", app_password, secure=True)
    _ssm_put(ssm, f"{FED_PREFIX}/EGRESS_CONNECTOR_ARN", connector_arn)

    print(f"\nDONE. Aurora endpoint {endpoint}")
    print(f"  connector: {connector_arn}")
    print(f"  /postgres/* published in {region} (POSTGRES_SSM_REGION={region})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
