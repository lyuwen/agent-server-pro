import os
import json
import uuid
import httpx
from dotenv import load_dotenv
from datetime import datetime, timezone
from typing import Any
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

load_dotenv()

app = FastAPI()

API_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
LOG_FILE = os.environ.get("LOG_FILE", "requests.jsonl")
VERBOSE_PROXY = os.environ.get("VERBOSE_PROXY", "false").lower() == "true"


def _log_verbose(prefix: str, msg: str) -> None:
    """Print to stderr if VERBOSE_PROXY is enabled."""
    if VERBOSE_PROXY:
        import sys
        sys.stderr.write(f"[proxy] {prefix} {msg}\n")
        sys.stderr.flush()


def log_to_jsonl(record: dict[str, Any]) -> None:
    """Append a record to the JSONL log file."""
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_sse_to_message(sse_chunks: list[bytes]) -> dict[str, Any]:
    """
    Parse SSE chunks and reconstruct the complete message.

    SSE format: "event: <type>\ndata: <json>\n\n"
    Key events:
    - message_start: contains initial message structure
    - content_block_start: new content block begins
    - content_block_delta: text/tool_use deltas
    - content_block_stop: block complete
    - message_delta: final usage stats
    - message_stop: stream complete
    """
    message: dict[str, Any] = {}
    content_blocks: dict[int, dict[str, Any]] = {}

    for chunk in sse_chunks:
        text = chunk.decode("utf-8", errors="replace")
        for line in text.split("\n"):
            if not line.startswith("data: "):
                continue
            data_str = line[6:]  # strip "data: "
            if data_str.strip() == "[DONE]":
                continue
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")

            if event_type == "message_start":
                message = event.get("message", {})
                message["content"] = []  # will be populated from blocks

            elif event_type == "content_block_start":
                idx = event.get("index", 0)
                block = event.get("content_block", {})
                content_blocks[idx] = block

            elif event_type == "content_block_delta":
                idx = event.get("index", 0)
                delta = event.get("delta", {})
                if idx not in content_blocks:
                    content_blocks[idx] = {"type": "text", "text": ""}

                # Handle text deltas
                if delta.get("type") == "text_delta":
                    content_blocks[idx].setdefault("text", "")
                    content_blocks[idx]["text"] += delta.get("text", "")
                # Handle thinking deltas
                elif delta.get("type") == "thinking_delta":
                    content_blocks[idx].setdefault("thinking", "")
                    content_blocks[idx]["thinking"] += delta.get("thinking", "")
                # Handle tool use deltas (partial JSON input)
                elif delta.get("type") == "input_json_delta":
                    content_blocks[idx].setdefault("partial_json", "")
                    content_blocks[idx]["partial_json"] += delta.get("partial_json", "")

            elif event_type == "content_block_stop":
                idx = event.get("index", 0)
                if idx in content_blocks:
                    block = content_blocks[idx]
                    # Finalize tool_use blocks by parsing accumulated JSON
                    if block.get("type") == "tool_use" and "partial_json" in block:
                        try:
                            block["input"] = json.loads(block.pop("partial_json"))
                        except json.JSONDecodeError:
                            block["input"] = block.pop("partial_json")

            elif event_type == "message_delta":
                # Update stop_reason and usage
                delta = event.get("delta", {})
                if "stop_reason" in delta:
                    message["stop_reason"] = delta["stop_reason"]
                if "usage" in event:
                    message.setdefault("usage", {}).update(event["usage"])

    # Assemble content blocks in order
    if content_blocks:
        message["content"] = [content_blocks[i] for i in sorted(content_blocks.keys())]

    return message


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
        # Remove content-encoding since httpx already decompresses the response
        response_headers.pop("content-encoding", None)
        response_headers.pop("content-length", None)  # Length changed after decompression
        return response.status_code, response.content, response_headers, body


async def forward_request_streaming(
    request: Request,
) -> tuple[int, dict[str, str], bytes, httpx.Response, httpx.AsyncClient]:
    """Forward request with streaming, returning status, headers, request body, and response stream."""
    target_url = API_BASE_URL + request.url.path
    if request.url.query:
        target_url += f"?{request.url.query}"

    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    body = await request.body()

    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))
    req = client.build_request(method=request.method, url=target_url, headers=headers, content=body)
    response = await client.send(req, stream=True)

    response_headers = dict(response.headers)
    response_headers.pop("content-encoding", None)
    response_headers.pop("content-length", None)

    return response.status_code, response_headers, body, response, client


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(full_path: str, request: Request):
    """Catch-all endpoint that forwards all requests to the remote API."""
    _ = full_path  # captured by route but we use request.url for full path info

    # Check if client requested streaming
    request_body_bytes = await request.body()
    is_streaming_request = False
    try:
        req_json = json.loads(request_body_bytes)
        is_streaming_request = req_json.get("stream", False)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass

    # Restore body for forwarding (need to recreate request scope)
    async def receive():
        return {"type": "http.request", "body": request_body_bytes}
    request._receive = receive

    if not is_streaming_request:
        # Non-streaming: simple forward
        try:
            status, content, response_headers, req_body = await forward_request(request)
            _log_verbose(">>>", f"{request.method} {request.url.path} ({len(req_body)} bytes)")
            _log_verbose("<<<", f"{status} ({len(content)} bytes): {content[:500].decode(errors='replace')}")
            record = build_record(request, status, content, req_body)
            log_to_jsonl(record)
            return Response(content=content, status_code=status, headers=response_headers)
        except httpx.RequestError as e:
            error_record = build_record(request, 500)
            error_record["error"] = str(e)
            log_to_jsonl(error_record)
            return Response(
                content=json.dumps({"error": "Failed to forward request", "detail": str(e)}).encode(),
                status_code=502,
                headers={"Content-Type": "application/json"},
            )

    # Streaming: forward chunks while buffering for logging
    try:
        status, response_headers, req_body, response, client = await forward_request_streaming(request)
    except httpx.RequestError as e:
        error_record = build_record(request, 500)
        error_record["error"] = str(e)
        log_to_jsonl(error_record)
        return Response(
            content=json.dumps({"error": "Failed to forward request", "detail": str(e)}).encode(),
            status_code=502,
            headers={"Content-Type": "application/json"},
        )

    sse_buffer: list[bytes] = []

    async def stream_and_buffer():
        try:
            _log_verbose(">>>", f"{request.method} {request.url.path} (streaming, {len(req_body)} bytes)")
            async for chunk in response.aiter_bytes():
                sse_buffer.append(chunk)
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

            # Log the reconstructed message
            reconstructed = parse_sse_to_message(sse_buffer)
            _log_verbose("<<<", f"stream complete: {json.dumps(reconstructed, ensure_ascii=False)[:500]}")
            record = build_record(request, status, None, req_body)
            record["response"]["body"] = reconstructed
            record["response"]["streaming"] = True
            log_to_jsonl(record)

    return StreamingResponse(
        stream_and_buffer(),
        status_code=status,
        headers=response_headers,
        media_type=response_headers.get("content-type", "text/event-stream"),
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))

    class PortReportingServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if not self.started:
                return  # lifespan failed; self.servers was never set
            # Report the OS-assigned port to stdout (only stdout output this process emits)
            bound_port = self.servers[0].sockets[0].getsockname()[1]
            print(f"PROXY_PORT={bound_port}", flush=True)

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = PortReportingServer(config=config)

    import asyncio
    asyncio.run(server.serve())
