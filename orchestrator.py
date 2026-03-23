import os
import sys
import json
import uuid
import asyncio
import shutil
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(os.environ.get("BASE_DIR", os.getcwd())).resolve()
PROXY_STARTUP_TIMEOUT = float(os.environ.get("PROXY_STARTUP_TIMEOUT", "10"))
CLAUDE_TIMEOUT = float(os.environ.get("CLAUDE_TIMEOUT", "300"))
KEEP_LOGS = os.environ.get("KEEP_LOGS", "false").lower() == "true"
LOGS_DIR = Path(__file__).parent / "logs"

app = FastAPI()


@app.on_event("startup")
async def _create_logs_dir():
    LOGS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class RunRequest(BaseModel):
    prompt: str
    work_dir: str | None = None


class RunResponse(BaseModel):
    job_id: str
    status: str
    work_dir: str | None
    claude_stdout: str | None
    claude_stderr: str | None
    claude_exit_code: int | None
    trace: list[dict[str, Any]] | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def resolve_work_dir(raw: str | None) -> Path | None:
    """Resolve and validate work_dir. Raises HTTPException on invalid input."""
    if raw is None:
        return BASE_DIR

    candidate = (BASE_DIR / raw).resolve()

    # Path traversal check (skip if BASE_DIR is root - all paths are under root)
    base_str = str(BASE_DIR)
    if base_str != "/":
        if candidate != BASE_DIR and not str(candidate).startswith(base_str + os.sep):
            raise HTTPException(status_code=400, detail=f"work_dir resolves outside BASE_DIR: {candidate}")

    # Existence check
    if not candidate.exists():
        return None  # signals invalid_work_dir status (not a 400)

    # Must be a directory
    if not candidate.is_dir():
        raise HTTPException(status_code=400, detail=f"work_dir exists but is not a directory: {candidate}")

    return candidate


def read_trace(log_file: Path) -> list[dict[str, Any]]:
    """Read JSONL log file; return [] if file does not exist."""
    if not log_file.exists():
        return []
    records = []
    with open(log_file) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


async def kill_proc(proc: asyncio.subprocess.Process, grace: float = 5.0) -> None:
    """SIGTERM → grace period → SIGKILL, then wait."""
    if proc.returncode is not None:
        return  # already exited
    try:
        proc.terminate()
    except ProcessLookupError:
        await proc.wait()
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def _read_proxy_port(proc: asyncio.subprocess.Process) -> int:
    """
    Read lines from proc.stdout until PROXY_PORT=<n> is found.
    Raises RuntimeError("proxy_start_failed") if PROXY_STARTUP_TIMEOUT expires.
    """
    async def _read():
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError("proxy_start_failed: proxy stdout closed without PROXY_PORT=")
            decoded = line.decode().strip()
            if decoded.startswith("PROXY_PORT="):
                return int(decoded.split("=", 1)[1])

    try:
        return await asyncio.wait_for(_read(), timeout=PROXY_STARTUP_TIMEOUT)
    except asyncio.TimeoutError:
        raise RuntimeError("proxy_start_failed: timeout waiting for PROXY_PORT=")


async def spawn_proxy(log_file: Path) -> tuple[asyncio.subprocess.Process, int]:
    """
    Launch main.py on an OS-assigned port, wait for it to report its port.
    Returns (process, port). Caller is responsible for termination.
    Raises RuntimeError("proxy_start_failed") on failure.
    """
    env = os.environ.copy()
    env["PORT"] = "0"
    env["LOG_FILE"] = str(log_file)

    proxy_script = Path(__file__).parent / "main.py"

    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(proxy_script),
        stdout=asyncio.subprocess.PIPE,
        stderr=None,  # inherit orchestrator stderr
        env=env,
    )

    try:
        port = await _read_proxy_port(proc)
        print(f"proxy launched at port: {port}")
    except RuntimeError:
        await kill_proc(proc)
        raise

    return proc, port


async def spawn_claude(
    prompt: str,
    work_dir: Path,
    proxy_port: int,
    claude_binary: str | None = None,
) -> asyncio.subprocess.Process:
    """
    Launch Claude Code non-interactively with API traffic routed through the proxy.
    Raises RuntimeError("claude_not_found") if the binary is not in PATH.

    The binary is resolved in order: claude_binary param > CLAUDE_BINARY env var > "claude"
    """
    if claude_binary is None:
        claude_binary = os.environ.get("CLAUDE_BINARY", "claude")

    if not shutil.which(claude_binary):
        raise RuntimeError(f"claude_not_found: '{claude_binary}' not found in PATH")

    proxy_url = f"http://127.0.0.1:{proxy_port}"
    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = proxy_url
    env["IS_DEMO"] = "true"  # Skip onboarding flow
    # Remove any system proxy settings that would interfere with Claude reaching our local proxy
    for key in ["http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
        env.pop(key, None)

    return await asyncio.create_subprocess_exec(
        claude_binary,
        "--dangerously-skip-permissions",
        "--setting-sources", "project,local",  # skip ~/.claude, allow work_dir settings
        "--print",
        "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(work_dir),
        env=env,
    )


async def run_job(
    job_id: str,
    prompt: str,
    work_dir: Path,
) -> RunResponse:
    """
    Full job lifecycle: spawn proxy → spawn Claude → monitor → collect trace → cleanup.
    """
    log_file = LOGS_DIR / f"{job_id}.jsonl"

    # --- Spawn proxy ---
    try:
        proxy_proc, proxy_port = await spawn_proxy(log_file)
    except RuntimeError:
        return RunResponse(
            job_id=job_id,
            status="proxy_start_failed",
            work_dir=str(work_dir),
            claude_stdout=None,
            claude_stderr=None,
            claude_exit_code=None,
            trace=None,
        )

    # --- Spawn Claude ---
    try:
        claude_proc = await spawn_claude(prompt, work_dir, proxy_port)
    except RuntimeError:
        await kill_proc(proxy_proc)
        return RunResponse(
            job_id=job_id,
            status="claude_not_found",
            work_dir=str(work_dir),
            claude_stdout=None,
            claude_stderr=None,
            claude_exit_code=None,
            trace=None,
        )

    # --- Monitor both processes concurrently ---
    claude_stdout_chunks: list[bytes] = []
    claude_stderr_chunks: list[bytes] = []
    status = "completed"

    async def _stream_and_capture(stream, chunks: list[bytes], prefix: str, out_stream):
        """Read from stream line-by-line, print with prefix, and capture."""
        while True:
            line = await stream.readline()
            if not line:
                break
            chunks.append(line)
            # Print to orchestrator's stdout/stderr in real-time
            out_stream.write(f"{prefix}{line.decode(errors='replace')}")
            out_stream.flush()

    async def _wait_claude():
        await asyncio.gather(
            _stream_and_capture(claude_proc.stdout, claude_stdout_chunks, "[claude] ", sys.stdout),
            _stream_and_capture(claude_proc.stderr, claude_stderr_chunks, "[claude-err] ", sys.stderr),
        )
        await claude_proc.wait()

    async def _watch_proxy():
        await proxy_proc.wait()

    claude_task = asyncio.create_task(_wait_claude())
    proxy_task = asyncio.create_task(_watch_proxy())

    try:
        done, pending = await asyncio.wait(
            {claude_task, proxy_task},
            timeout=CLAUDE_TIMEOUT,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            # Timeout — neither finished within CLAUDE_TIMEOUT
            status = "timed_out"
            claude_task.cancel()
            proxy_task.cancel()
            # Await cancelled tasks before killing processes so communicate()
            # releases its stdout pipe reference cleanly (avoids ResourceWarning / hang)
            await asyncio.gather(claude_task, proxy_task, return_exceptions=True)
            await kill_proc(claude_proc)
            await kill_proc(proxy_proc)

        elif proxy_task in done and claude_task not in done:
            # Proxy crashed before Claude finished
            status = "proxy_crashed"
            claude_task.cancel()
            await asyncio.gather(claude_task, return_exceptions=True)
            await kill_proc(claude_proc)

        elif claude_task in done:
            # Claude finished normally — shut down proxy
            proxy_task.cancel()
            await asyncio.gather(proxy_task, return_exceptions=True)
            await kill_proc(proxy_proc)

            exit_code = claude_proc.returncode
            if exit_code != 0:
                status = "failed"

    except Exception:
        # Safety net — ensure processes are cleaned up
        await kill_proc(claude_proc)
        await kill_proc(proxy_proc)
        raise

    # Collect stdout/stderr from captured chunks
    claude_stdout_b = b"".join(claude_stdout_chunks)
    claude_stderr_b = b"".join(claude_stderr_chunks)
    claude_stdout = claude_stdout_b.decode(errors="replace") if claude_stdout_b else None
    claude_stderr = claude_stderr_b.decode(errors="replace") if claude_stderr_b else None
    claude_exit_code = claude_proc.returncode if status not in ("timed_out", "proxy_crashed") else None

    # Read trace
    trace = read_trace(log_file)

    # Cleanup log file
    if not KEEP_LOGS and log_file.exists():
        log_file.unlink()

    return RunResponse(
        job_id=job_id,
        status=status,
        work_dir=str(work_dir),
        claude_stdout=claude_stdout,
        claude_stderr=claude_stderr,
        claude_exit_code=claude_exit_code,
        trace=trace,
    )


@app.post("/run", response_model=RunResponse)
async def run(request: RunRequest):
    job_id = str(uuid.uuid4())

    # Resolve and validate work_dir
    resolved = resolve_work_dir(request.work_dir)
    if resolved is None:
        # Path does not exist
        return RunResponse(
            job_id=job_id,
            status="invalid_work_dir",
            work_dir=None,
            claude_stdout=None,
            claude_stderr=None,
            claude_exit_code=None,
            trace=None,
        )

    return await run_job(job_id=job_id, prompt=request.prompt, work_dir=resolved)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
