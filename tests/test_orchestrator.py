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
