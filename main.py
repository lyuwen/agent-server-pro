import os
import json
import uuid
import httpx
from dotenv import load_dotenv
from datetime import datetime, timezone
from typing import Any
from fastapi import FastAPI, Request, Response

load_dotenv()

app = FastAPI()

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")
LOG_FILE = os.environ.get("LOG_FILE", "requests.jsonl")


def log_to_jsonl(record: dict[str, Any]) -> None:
    """Append a record to the JSONL log file."""
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_record(
    request: Request,
    response_status: int,
    response_body: bytes | None = None,
    request_body: bytes | None = None,
) -> dict[str, Any]:
    """Build the JSONL record with request and response data."""
    record = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request": {
            "method": request.method,
            "url": str(request.url),
            "path": request.url.path,
            "query_params": dict(request.query_params),
            "headers": dict(request.headers),
        },
        "response": {
            "status": response_status,
        },
    }

    # Add request body if present
    if request_body is not None:
        try:
            record["request"]["body"] = json.loads(request_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            try:
                record["request"]["body"] = request_body.decode("utf-8")
            except UnicodeDecodeError:
                record["request"]["body"] = "[binary request body]"

    # Add response body if present
    if response_body is not None:
        try:
            record["response"]["body"] = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            try:
                record["response"]["body"] = response_body.decode("utf-8")
            except UnicodeDecodeError:
                record["response"]["body"] = "[binary response body]"

    return record


async def forward_request(request: Request) -> tuple[int, bytes, dict[str, str], bytes]:
    """Forward the request to the remote API and return status, body, headers, and request body."""
    # Build the target URL
    target_url = API_BASE_URL + request.url.path
    if request.url.query:
        target_url += f"?{request.url.query}"

    # Forward headers (except host)
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

    # Read body if present
    body = await request.body()

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        response = await client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )

        response_headers = dict(response.headers)
        return response.status_code, response.content, response_headers, body


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(full_path: str, request: Request):
    """Catch-all endpoint that forwards all requests to the remote API."""
    # full_path is captured but we use request.url for the full path info
    try:
        status, content, response_headers, request_body = await forward_request(request)

        # Build and log the record
        record = build_record(request, status, content, request_body)
        log_to_jsonl(record)

        # Return the response as-is
        return Response(
            content=content,
            status_code=status,
            headers=response_headers,
        )
    except httpx.RequestError as e:
        # Log the error
        error_record = build_record(request, 500)
        error_record["error"] = str(e)
        log_to_jsonl(error_record)

        return Response(
            content=json.dumps({"error": "Failed to forward request", "detail": str(e)}).encode(),
            status_code=502,
            headers={"Content-Type": "application/json"},
        )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))

    class PortReportingServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            # Report the OS-assigned port to stdout (only stdout output this process emits)
            bound_port = self.servers[0].sockets[0].getsockname()[1]
            print(f"PROXY_PORT={bound_port}", flush=True)

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = PortReportingServer(config=config)

    import asyncio
    asyncio.run(server.serve())