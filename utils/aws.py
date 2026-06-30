"""utils/aws.py — Fetch parameters from AWS SSM Parameter Store."""
import os
import boto3

_ssm = None

def get_ssm_parameter(name: str) -> str:
    """Fetch a parameter from SSM. Caches the client."""
    global _ssm
    if _ssm is None:
        # Spec-amendment: see stamp
        # Region resolution: env-var AWS_REGION with us-east-1 fallback, matching
        # scripts/create_runtime.py + scripts/create_memory.py convention.
        _ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    response = _ssm.get_parameter(Name=name, WithDecryption=True)
    return response["Parameter"]["Value"]
