#!/usr/bin/env python3
"""scripts/microvm_ui_proxy.py — serve the chat UI locally, proxy to a running Lambda MicroVM.

The browser talks to http://localhost:PORT (same-origin, no CORS/auth juggling in
the page), and this proxy forwards each request to the MicroVM's HTTPS endpoint,
injecting the per-request auth token (``X-aws-proxy-auth``) and ``X-aws-proxy-port:
8080`` that the Lambda MicroVMs proxy layer requires.

Unlike scripts/ui_proxy.py (AgentCore, which fakes streaming), this does a REAL
SSE passthrough of ``/chat/stream``, so the browser receives live token + tool
lifecycle events and renders the per-tool latency waterfall exactly as it would
against a local server. ``/health`` and ``/info`` are proxied straight through,
so the info panel shows the MicroVM's real row_count and version.

The auth token is minted from the caller's AWS credentials and refreshed
automatically (a couple of minutes before expiry, or on any 401/403).

Usage (ids come from what scripts/deploy_microvm.py printed):
  python scripts/microvm_ui_proxy.py \
    --microvm-id microvm-xxxx \
    --endpoint  xxxx.lambda-microvm.us-west-2.on.aws \
    --region us-west-2
  # then open http://localhost:8080
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_HERE, "static")


class TokenManager:
    """Mints + caches a MicroVM auth token, refreshing before it expires."""

    def __init__(self, microvm_id: str, region: str, ttl_minutes: int = 30):
        self.microvm_id = microvm_id
        self.region = region
        self.ttl = ttl_minutes
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def get(self, *, force: bool = False) -> str:
        with self._lock:
            now = time.time()
            if force or self._token is None or now >= self._expires:
                out = subprocess.run(
                    ["aws", "lambda-microvms", "create-microvm-auth-token",
                     "--microvm-identifier", self.microvm_id,
                     "--expiration-in-minutes", str(self.ttl),
                     "--allowed-ports", json.dumps([{"port": 8080}]),
                     "--region", self.region],
                    capture_output=True, text=True,
                )
                if out.returncode != 0:
                    raise RuntimeError((out.stderr or "").strip())
                self._token = json.loads(out.stdout)["authToken"]["X-aws-proxy-auth"]
                # Refresh two minutes before the real expiry.
                self._expires = now + max(60, (self.ttl - 2) * 60)
            return self._token


def build_app(endpoint: str, tokens: TokenManager) -> FastAPI:
    app = FastAPI()
    base_url = f"https://{endpoint}"

    def _auth_headers(token: str, *, json_body: bool = False) -> dict:
        h = {"X-aws-proxy-auth": token, "X-aws-proxy-port": "8080"}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    async def _forward(method: str, path: str, request: Request) -> Response:
        body = await request.body() if method in ("POST", "PUT") else None
        for attempt in range(2):  # retry once with a fresh token on auth failure
            token = tokens.get(force=(attempt == 1))
            headers = _auth_headers(token, json_body=bool(body))
            async with httpx.AsyncClient(base_url=base_url, timeout=180.0) as client:
                r = await client.request(method, path, content=body, headers=headers)
            if r.status_code not in (401, 403):
                break
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    @app.get("/")
    async def index() -> FileResponse:
        # The MicroVM serves the same static/index.html; serve the local copy to
        # skip a round-trip. All data endpoints below are proxied to the VM.
        return FileResponse(os.path.join(_STATIC, "index.html"))

    @app.get("/health")
    async def health(request: Request) -> Response:
        return await _forward("GET", "/health", request)

    @app.get("/info")
    async def info(request: Request) -> Response:
        return await _forward("GET", "/info", request)

    @app.get("/ping")
    async def ping(request: Request) -> Response:
        return await _forward("GET", "/ping", request)

    @app.get("/metrics/{trace_id}")
    async def metrics(trace_id: str, request: Request) -> Response:
        return await _forward("GET", f"/metrics/{trace_id}", request)

    @app.post("/chat")
    async def chat(request: Request) -> Response:
        return await _forward("POST", "/chat", request)

    @app.post("/invocations")
    async def invocations(request: Request) -> Response:
        return await _forward("POST", "/invocations", request)

    @app.post("/chat/stream")
    async def chat_stream(request: Request) -> StreamingResponse:
        body = await request.body()

        async def gen():
            token = tokens.get()
            headers = _auth_headers(token, json_body=True)
            async with httpx.AsyncClient(base_url=base_url, timeout=None) as client:
                async with client.stream(
                    "POST", "/chat/stream", content=body, headers=headers
                ) as r:
                    async for chunk in r.aiter_raw():
                        yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def main() -> int:
    ap = argparse.ArgumentParser(description="Serve the chat UI, proxy to a Lambda MicroVM")
    ap.add_argument("--microvm-id", required=True, help="microvm-xxxx (from deploy output)")
    ap.add_argument("--endpoint", required=True, help="xxxx.lambda-microvm.<region>.on.aws")
    ap.add_argument("--region", default=os.getenv("AWS_REGION", "us-west-2"))
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    tokens = TokenManager(args.microvm_id, args.region)
    tokens.get()  # fail fast if the id/region/creds are wrong
    app = build_app(args.endpoint, tokens)
    print(f"proxying browser → MicroVM {args.microvm_id} ({args.region})")
    print(f"open http://localhost:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
