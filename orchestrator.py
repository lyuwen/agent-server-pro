import os
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
LOGS_DIR = Path(os.getcwd()) / "logs"

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
def resolve_work_dir(raw: str | None) -> Path:
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
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
