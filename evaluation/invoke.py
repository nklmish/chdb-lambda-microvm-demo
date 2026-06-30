"""evaluation/invoke.py — Invoke an AgentCore-deployed agent runtime."""
import json
import os
import uuid
import boto3

def invoke_agent(
    agent_arn: str,
    prompt: str,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
) -> dict:
    """Invoke a deployed AgentCore agent runtime.

    When called from inside a Langfuse observation, pass trace_id and parent_span_id
    extracted from langfuse.get_client().get_current_trace_id() and .get_current_observation_id().
    The agent's /invocations route uses these to reopen the parent span in DEV/TST —
    Langfuse v4 expects parent_span_id (NOT parent_observation_id, which is a v2/v3
    key name the new SDK silently ignores).
    """
    # Spec-amendment: see stamp
    # Region resolution: env-var AWS_REGION with us-east-1 fallback, matching
    # scripts/create_runtime.py + scripts/create_memory.py convention.
    client = boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-east-1"))

    payload = {"prompt": prompt}
    if trace_id:
        payload["trace_id"] = trace_id
        payload["parent_span_id"] = parent_span_id

    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=agent_arn,
            payload=json.dumps(payload),
            contentType="application/json",
        )
        # boto3 bedrock-agentcore returns streaming body under key
        # "response" (not "body") and session id under "runtimeSessionId" (not
        # "sessionId"). Verified via introspection at Phase C C.1 2026-04-22.
        body = json.loads(response["response"].read())
        return {
            "response": body.get("response", ""),
            "session_id": response.get("runtimeSessionId", str(uuid.uuid4())),
            "content_type": response.get("contentType", "application/json"),
        }
    except Exception as e:
        return {"error": str(e)}
