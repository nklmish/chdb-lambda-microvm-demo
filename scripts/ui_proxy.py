"""scripts/ui_proxy.py — Serve the chat UI locally, talk to the AWS AgentCore Runtime.

This lets a browser on the dev machine interact with the *deployed* agent (full
data baked into the runtime image, AgentCore Memory, everything on AWS). The
browser hits http://localhost:8080; this proxy forwards each message to the
deployed runtime via bedrock-agentcore:InvokeAgentRuntime (SigV4-signed by the
caller's AWS credentials).

It serves the existing static/index.html and implements just the 3 routes the UI
needs: GET /health, POST /chat, POST /chat/stream (SSE). The runtime's /invocations
is synchronous, so /chat/stream sends the full answer chunked for a streaming feel.

Resolve the runtime: AGENTCORE_RUNTIME_ARN env var, else auto-discover a READY
runtime named nyc_taxi_agent_* in the region (AWS_REGION, default us-east-1).

Run:  AWS_REGION=us-east-1 .venv/bin/python scripts/ui_proxy.py
Then open http://localhost:8080
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import boto3
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
MODEL = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_HERE, "static")

# One session id per proxy run → AgentCore Memory keeps conversational context
# across the browser's messages. (Must be >= 33 chars.)
SESSION_ID = "ui-proxy-" + uuid.uuid4().hex


def _resolve_runtime_arn() -> str:
    arn = os.getenv("AGENTCORE_RUNTIME_ARN")
    if arn:
        return arn
    ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
    candidates = [
        r for r in ctl.list_agent_runtimes(maxResults=100).get("agentRuntimes", [])
        if r.get("agentRuntimeName", "").startswith("nyc_taxi_agent")
    ]
    ready = [r for r in candidates if r.get("status") == "READY"] or candidates
    if not ready:
        raise RuntimeError(
            f"No nyc_taxi_agent runtime found in {REGION}. Deploy first (deploy-agentcore) "
            f"or set AGENTCORE_RUNTIME_ARN."
        )
    return ready[0]["agentRuntimeArn"]


RUNTIME_ARN = _resolve_runtime_arn()
_dp = boto3.client("bedrock-agentcore", region_name=REGION)

app = FastAPI()
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


def _invoke(text: str) -> str:
    """Call the deployed AgentCore runtime and return the answer text."""
    resp = _dp.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        qualifier="DEFAULT",
        runtimeSessionId=SESSION_ID,
        contentType="application/json",
        accept="application/json",
        payload=json.dumps({"text": text}).encode(),
    )
    body = resp["response"].read()
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    try:
        return json.loads(body).get("response", body)
    except json.JSONDecodeError:
        return body


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(os.path.join(_STATIC, "index.html"))


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "status": f"healthy — proxying to AWS runtime ({REGION})",
        "version": "proxy-1.0",
        "rows_available": None,  # full dataset lives in the deployed runtime image
    })


@app.get("/metrics/{trace_id}")
async def metrics(trace_id: str) -> JSONResponse:
    # The proxy doesn't surface per-trace metrics; the UI only calls this when a
    # non-null trace_id is returned, and we always return null below.
    return JSONResponse({})


@app.post("/chat")
async def chat(request: Request) -> JSONResponse:
    body = await request.json()
    text = (body or {}).get("text") or (body or {}).get("prompt") or ""
    answer = await asyncio.to_thread(_invoke, text)
    return JSONResponse({"response": answer, "request_id": str(uuid.uuid4()), "timings": []})


@app.post("/chat/stream")
async def chat_stream(request: Request):
    body = await request.json()
    text = (body or {}).get("text") or (body or {}).get("prompt") or ""

    async def gen():
        answer = await asyncio.to_thread(_invoke, text)
        # Chunk the answer for a streaming feel (the runtime itself is synchronous).
        step = 48
        for i in range(0, len(answer), step):
            chunk = answer[i:i + step]
            yield f"data: {json.dumps({'type': 'text', 'content': chunk})}\n\n"
            await asyncio.sleep(0.01)
        done = {
            "type": "done", "timings": [],
            "metrics": {
                "tokens_in": 0, "tokens_out": 0, "tools_used": [],
                "model": MODEL, "trace_id": None, "trace_url": None,
            },
        }
        yield f"data: {json.dumps(done)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    print(f"[ui-proxy] region={REGION}")
    print(f"[ui-proxy] runtime={RUNTIME_ARN}")
    print("[ui-proxy] open http://localhost:8080")
    uvicorn.run(app, host="127.0.0.1", port=8080, workers=1)
