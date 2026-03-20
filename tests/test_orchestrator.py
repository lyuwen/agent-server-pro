import asyncio
import sys
import pytest
from httpx import AsyncClient, ASGITransport
from orchestrator import app


@pytest.mark.asyncio
async def test_run_rejects_missing_prompt():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/run", json={})
    assert resp.status_code == 422  # FastAPI validation error for missing required field


@pytest.mark.asyncio
async def test_run_invalid_work_dir_outside_base(tmp_path):
    import orchestrator as orch
    original_base = orch.BASE_DIR
    orch.BASE_DIR = tmp_path.resolve()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/run", json={"prompt": "hi", "work_dir": "../../etc"})
        assert resp.status_code == 400
        assert "outside" in resp.json()["detail"].lower()
    finally:
        orch.BASE_DIR = original_base


import sys
from orchestrator import spawn_proxy, kill_proc


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
async def test_spawn_proxy_timeout_raises(tmp_path):
    """If proxy never prints PROXY_PORT=, _read_proxy_port raises RuntimeError."""
    import orchestrator as orch
    old = orch.PROXY_STARTUP_TIMEOUT
    orch.PROXY_STARTUP_TIMEOUT = 0.5
    try:
        proc = await asyncio.create_subprocess_exec(
            "sleep", "60",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        with pytest.raises(RuntimeError, match="proxy_start_failed"):
            await orch._read_proxy_port(proc)
    finally:
        orch.PROXY_STARTUP_TIMEOUT = old
        proc.terminate()
        await proc.wait()


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
