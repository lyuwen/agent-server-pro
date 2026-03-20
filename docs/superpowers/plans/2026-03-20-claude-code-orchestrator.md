# Claude Code Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `orchestrator.py`, a FastAPI service that accepts a prompt + work_dir, spawns an isolated proxy and Claude Code subprocess per job, and returns the full agentic trace as a structured JSON response.

**Architecture:** Two-process-per-job model. The orchestrator spawns `main.py` as a child process on an OS-assigned port, reads the port via a stdout handshake (`PROXY_PORT=<n>`), then launches Claude Code with env vars redirecting its API traffic through that proxy. All three lifecycle paths (normal, timeout, proxy crash) use SIGTERM → 5s grace → SIGKILL escalation.

**Tech Stack:** Python 3.10+, FastAPI, uvicorn, asyncio, httpx (already in proxy), pytest, pytest-asyncio

---

## File Map

| File | Role |
|---|---|
| `main.py` | Modified: subclass `uvicorn.Server`, override `startup()` to print `PROXY_PORT=<n>` to stdout |
| `orchestrator.py` | New: FastAPI app with `POST /run`, full subprocess lifecycle management |
| `tests/test_main_port_reporting.py` | New: verifies proxy prints PROXY_PORT= on startup |
| `tests/test_orchestrator.py` | New: integration tests for orchestrator endpoint |
| `.gitignore` | Modified: add `logs/` |

---

## Task 1: Project Setup

**Files:**
- Modify: `.gitignore`
- Create: `tests/__init__.py`
- Create: `requirements.txt`

- [ ] **Step 1: Install dependencies**

```bash
pip install fastapi uvicorn httpx python-dotenv pytest pytest-asyncio httpx
```

- [ ] **Step 2: Create requirements.txt**

```
fastapi
uvicorn[standard]
httpx
python-dotenv
pytest
pytest-asyncio
```

Save to `/data/lfu/llm/git-projects/proxy/requirements.txt`.

- [ ] **Step 3: Create test package and gitignore**

Create `/data/lfu/llm/git-projects/proxy/tests/__init__.py` (empty file).

Add to `/data/lfu/llm/git-projects/proxy/.gitignore`:
```
logs/
__pycache__/
*.pyc
.env
.pytest_cache/
```

- [ ] **Step 4: Create pytest config**

Create `/data/lfu/llm/git-projects/proxy/pytest.ini`:
```ini
[pytest]
asyncio_mode = auto
```

- [ ] **Step 5: Commit**

```bash
cd /data/lfu/llm/git-projects/proxy
git add requirements.txt tests/__init__.py .gitignore pytest.ini
git commit -m "chore: add project setup for orchestrator"
```

---

## Task 2: Modify `main.py` — Port Reporting on Startup

The proxy must print `PROXY_PORT=<n>` to stdout (flushed) immediately after uvicorn binds. This is the only stdout output the proxy ever emits; all uvicorn banners go to stderr.

**Files:**
- Modify: `main.py`
- Create: `tests/test_main_port_reporting.py`

- [ ] **Step 1: Write the failing test**

Create `/data/lfu/llm/git-projects/proxy/tests/test_main_port_reporting.py`:

```python
import subprocess
import sys
import os
import time


def test_proxy_prints_port_on_startup():
    """Proxy must print PROXY_PORT=<n> to stdout within 5 seconds of startup."""
    env = os.environ.copy()
    env["PORT"] = "0"
    env["LOG_FILE"] = "/tmp/test_proxy_port.jsonl"
    env["API_BASE_URL"] = "http://localhost:9999"  # dummy upstream

    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        text=True,
        cwd=os.path.dirname(os.path.dirname(__file__)),
    )
    try:
        port_line = None
        deadline = time.time() + 5
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if line.startswith("PROXY_PORT="):
                port_line = line.strip()
                break

        assert port_line is not None, "Proxy never printed PROXY_PORT="
        port_str = port_line.split("=")[1]
        assert port_str.isdigit(), f"Port is not a number: {port_str!r}"
        port = int(port_str)
        assert 1024 <= port <= 65535, f"Port out of range: {port}"
    finally:
        proc.terminate()
        proc.wait()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /data/lfu/llm/git-projects/proxy
pytest tests/test_main_port_reporting.py -v
```

Expected: FAIL — proxy does not yet print `PROXY_PORT=`.

- [ ] **Step 3: Modify `main.py` to report port**

Replace the `if __name__ == "__main__":` block in `main.py` with:

```python
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
```

Note: `log_level="warning"` keeps uvicorn's startup banners off stdout. They go to stderr at warning level, which is inherited and visible during development.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /data/lfu/llm/git-projects/proxy
pytest tests/test_main_port_reporting.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main_port_reporting.py
git commit -m "feat: report OS-assigned port on proxy startup via PROXY_PORT= stdout"
```

---

## Task 3: Build `orchestrator.py` — Core Structure & Config

Stand up the FastAPI app, define the request/response Pydantic models, read env config, and create the `logs/` directory at startup.

**Files:**
- Create: `orchestrator.py`
- Create: `tests/test_orchestrator.py` (skeleton)

- [ ] **Step 1: Write the failing test (config & startup)**

Create `/data/lfu/llm/git-projects/proxy/tests/test_orchestrator.py`:

```python
import pytest
from httpx import AsyncClient, ASGITransport
from orchestrator import app


@pytest.mark.asyncio
async def test_run_rejects_missing_prompt():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/run", json={})
    assert resp.status_code == 422  # FastAPI validation error for missing required field


@pytest.mark.asyncio
async def test_run_invalid_work_dir_outside_base(tmp_path, monkeypatch):
    monkeypatch.setenv("BASE_DIR", str(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/run", json={"prompt": "hi", "work_dir": "../../etc"})
    assert resp.status_code == 400
    assert "outside" in resp.json()["detail"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /data/lfu/llm/git-projects/proxy
pytest tests/test_orchestrator.py::test_run_rejects_missing_prompt -v
```

Expected: FAIL — `orchestrator.py` does not exist.

- [ ] **Step 3: Create `orchestrator.py` with models and config**

Create `/data/lfu/llm/git-projects/proxy/orchestrator.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /data/lfu/llm/git-projects/proxy
pytest tests/test_orchestrator.py -v
```

Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add orchestrator.py tests/test_orchestrator.py
git commit -m "feat: add orchestrator skeleton with models, config, and work_dir validation"
```

---

## Task 4: Port Discovery — Spawn Proxy & Read Port

Implement the function that spawns `main.py` as a child process and reads the `PROXY_PORT=` line from its stdout within the startup timeout.

**Files:**
- Modify: `orchestrator.py`
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_orchestrator.py`:

```python
import sys
from orchestrator import spawn_proxy


@pytest.mark.asyncio
async def test_spawn_proxy_returns_port(tmp_path):
    """spawn_proxy must return a valid port number and a live process."""
    log_file = tmp_path / "test.jsonl"
    proc, port = await spawn_proxy(log_file)
    try:
        assert isinstance(port, int)
        assert 1024 <= port <= 65535
        assert proc.returncode is None  # still running
    finally:
        await kill_proc(proc)


@pytest.mark.asyncio
async def test_spawn_proxy_timeout_raises(monkeypatch, tmp_path):
    """If proxy never prints PROXY_PORT=, spawn_proxy raises RuntimeError."""
    monkeypatch.setenv("PROXY_STARTUP_TIMEOUT", "1")
    # Reload module to pick up new env var
    import importlib, orchestrator
    importlib.reload(orchestrator)
    from orchestrator import spawn_proxy as sp

    # Point to a script that never prints PROXY_PORT=
    log_file = tmp_path / "x.jsonl"
    with pytest.raises(RuntimeError, match="proxy_start_failed"):
        # Temporarily patch PROXY_STARTUP_TIMEOUT in module
        import orchestrator as orch
        old = orch.PROXY_STARTUP_TIMEOUT
        orch.PROXY_STARTUP_TIMEOUT = 0.5
        try:
            # Spawn sleep as the "proxy" — it never prints anything
            proc = await asyncio.create_subprocess_exec(
                "sleep", "60",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            port = await orch._read_proxy_port(proc)
        except RuntimeError:
            raise
        finally:
            orch.PROXY_STARTUP_TIMEOUT = old
            proc.terminate()
            await proc.wait()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /data/lfu/llm/git-projects/proxy
pytest tests/test_orchestrator.py::test_spawn_proxy_returns_port -v
```

Expected: FAIL — `spawn_proxy` not defined.

- [ ] **Step 3: Implement `spawn_proxy` and `_read_proxy_port`**

Append to `orchestrator.py` (before the route definitions):

```python
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
        "python3", str(proxy_script),
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /data/lfu/llm/git-projects/proxy
pytest tests/test_orchestrator.py::test_spawn_proxy_returns_port -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator.py tests/test_orchestrator.py
git commit -m "feat: implement proxy spawn and port discovery handshake"
```

---

## Task 5: Claude Subprocess Launch

Implement the function that spawns Claude Code with the correct env and working directory, and captures its stdout/stderr.

**Files:**
- Modify: `orchestrator.py`
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_orchestrator.py`:

```python
from orchestrator import spawn_claude


@pytest.mark.asyncio
async def test_spawn_claude_not_found_raises(tmp_path):
    """When claude binary is missing, spawn_claude raises RuntimeError."""
    with pytest.raises(RuntimeError, match="claude_not_found"):
        await spawn_claude(
            prompt="hello",
            work_dir=tmp_path,
            proxy_port=9999,
            claude_binary="__definitely_not_claude__",
        )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /data/lfu/llm/git-projects/proxy
pytest tests/test_orchestrator.py::test_spawn_claude_not_found_raises -v
```

Expected: FAIL — `spawn_claude` not defined.

- [ ] **Step 3: Implement `spawn_claude`**

Append to `orchestrator.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /data/lfu/llm/git-projects/proxy
pytest tests/test_orchestrator.py::test_spawn_claude_not_found_raises -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator.py tests/test_orchestrator.py
git commit -m "feat: implement Claude Code subprocess launcher with env patching"
```

---

## Task 6: Subprocess Lifecycle Manager

Implement `run_job()` — the core async function that orchestrates proxy + Claude, handles all termination paths (normal, timeout, proxy crash), reads the trace, and cleans up.

**Files:**
- Modify: `orchestrator.py`

This function has no isolated unit test (it requires both real subprocesses), so we test it end-to-end in Task 7. The structure is straightforward enough to implement directly.

- [ ] **Step 1: Implement `run_job()`**

Append to `orchestrator.py`:

```python
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
    claude_stdout_b = b""
    claude_stderr_b = b""
    status = "completed"

    async def _wait_claude():
        nonlocal claude_stdout_b, claude_stderr_b
        claude_stdout_b, claude_stderr_b = await claude_proc.communicate()

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
            await kill_proc(claude_proc)

        elif claude_task in done:
            # Claude finished normally — shut down proxy
            proxy_task.cancel()
            await kill_proc(proxy_proc)

            exit_code = claude_proc.returncode
            if exit_code != 0:
                status = "failed"

    except Exception:
        # Safety net — ensure processes are cleaned up
        await kill_proc(claude_proc)
        await kill_proc(proxy_proc)
        raise

    # Collect stdout/stderr if captured
    claude_stdout = claude_stdout_b.decode(errors="replace") if claude_stdout_b else None
    claude_stderr = claude_stderr_b.decode(errors="replace") if claude_stderr_b else None
    claude_exit_code = claude_proc.returncode if status not in ("timed_out",) else None

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
```

- [ ] **Step 2: Run existing tests to confirm no regressions**

```bash
cd /data/lfu/llm/git-projects/proxy
pytest tests/ -v
```

Expected: all previous tests PASS

- [ ] **Step 3: Commit**

```bash
git add orchestrator.py
git commit -m "feat: implement run_job() with full subprocess lifecycle management"
```

---

## Task 7: Wire Up `POST /run` Endpoint

Connect `run_job()` to the FastAPI endpoint, add work_dir resolution, and write end-to-end tests.

**Files:**
- Modify: `orchestrator.py`
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing end-to-end tests**

Append to `tests/test_orchestrator.py`:

```python
@pytest.mark.asyncio
async def test_run_invalid_work_dir_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("BASE_DIR", str(tmp_path))
    import importlib, orchestrator as orch
    orch.BASE_DIR = tmp_path.resolve()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/run", json={"prompt": "hi", "work_dir": "nonexistent"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "invalid_work_dir"
    assert body["trace"] is None
    assert body["work_dir"] is None


@pytest.mark.asyncio
async def test_run_claude_not_found(tmp_path, monkeypatch):
    """When claude binary doesn't exist, response status is claude_not_found."""
    monkeypatch.setenv("BASE_DIR", str(tmp_path))
    import orchestrator as orch
    orch.BASE_DIR = tmp_path.resolve()

    # Patch spawn_claude to simulate missing binary without needing real claude
    original = orch.spawn_claude
    async def fake_spawn_claude(*args, **kwargs):
        raise RuntimeError("claude_not_found: not in PATH")
    orch.spawn_claude = fake_spawn_claude

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/run", json={"prompt": "hi"})
        body = resp.json()
        assert body["status"] == "claude_not_found"
        assert body["trace"] is None
    finally:
        orch.spawn_claude = original
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /data/lfu/llm/git-projects/proxy
pytest tests/test_orchestrator.py::test_run_invalid_work_dir_not_found -v
```

Expected: FAIL — `/run` endpoint not defined yet.

- [ ] **Step 3: Add the `POST /run` endpoint**

Append to `orchestrator.py`:

```python
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
```

- [ ] **Step 4: Run all tests**

```bash
cd /data/lfu/llm/git-projects/proxy
pytest tests/ -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator.py tests/test_orchestrator.py
git commit -m "feat: wire POST /run endpoint to run_job with work_dir validation"
```

---

## Task 8: Orchestrator Entry Point & Manual Smoke Test

Add the `__main__` block so the orchestrator can be run directly, then do a quick manual smoke test against a real Claude invocation.

**Files:**
- Modify: `orchestrator.py`

- [ ] **Step 1: Add `__main__` block**

Append to `orchestrator.py`:

```python
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
```

- [ ] **Step 2: Run the orchestrator**

```bash
cd /data/lfu/llm/git-projects/proxy
ANTHROPIC_API_KEY=<your-key> python3 orchestrator.py
```

Expected: server starts on port 8080.

- [ ] **Step 3: Send a smoke-test request**

In a second terminal:

```bash
curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Print hello world to stdout and exit", "work_dir": "."}' \
  | python3 -m json.tool
```

Expected: JSON response with `"status": "completed"`, non-empty `trace` array, `claude_stdout` containing the output.

- [ ] **Step 4: Run full test suite**

```bash
cd /data/lfu/llm/git-projects/proxy
pytest tests/ -v
```

Expected: all PASS

- [ ] **Step 5: Final commit**

```bash
git add orchestrator.py
git commit -m "feat: add orchestrator __main__ entry point"
```

---

## Task 9: Final Cleanup

- [ ] **Step 1: Verify `.gitignore` covers logs/**

```bash
cd /data/lfu/llm/git-projects/proxy
mkdir -p logs
git check-ignore -v logs/
```

Expected: output shows `logs/` is ignored (already added in Task 1 — no need to add again).

- [ ] **Step 2: Run full test suite one final time**

```bash
pytest tests/ -v
```

Expected: all PASS, no warnings.

- [ ] **Step 3: Final commit**

```bash
git add .gitignore
git commit -m "chore: ensure logs/ directory is gitignored"
```
