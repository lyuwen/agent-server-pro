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

    # Path traversal check
    if candidate != BASE_DIR and not str(candidate).startswith(str(BASE_DIR) + os.sep):
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
    except RuntimeError:
        await kill_proc(proc)
        raise

    return proc, port


async def spawn_claude(
    prompt: str,
    work_dir: Path,
    proxy_port: int,
    claude_binary: str = "claude",
) -> asyncio.subprocess.Process:
    """
    Launch Claude Code non-interactively with API traffic routed through the proxy.
    Raises RuntimeError("claude_not_found") if the binary is not in PATH.
    """
    if not shutil.which(claude_binary):
        raise RuntimeError(f"claude_not_found: '{claude_binary}' not found in PATH")

    proxy_url = f"http://127.0.0.1:{proxy_port}"
    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = proxy_url
    env["HTTP_PROXY"] = proxy_url
    env["HTTPS_PROXY"] = proxy_url

    return await asyncio.create_subprocess_exec(
        claude_binary,
        "--dangerously-skip-permissions",
        "--print",
        "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(work_dir),
        env=env,
    )
