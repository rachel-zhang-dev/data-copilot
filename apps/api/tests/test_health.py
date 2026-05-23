"""Smoke test for the API health endpoint.

We mock the DB-related lifespan dependencies because they require a
running Postgres. The integration suite covers the real wiring.

Run with::

    uv run pytest -m "not integration"
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A FastAPI test client where DB lifespan hooks are no-ops.

    The checkpointer helpers became ``async`` in week 5 (to match
    LangGraph's ``ainvoke`` dispatch), so the no-op replacements must
    themselves be coroutines — patching with a plain lambda would
    cause ``await None`` and a TypeError during app startup.
    """
    monkeypatch.setattr("copilot.main.get_engine", lambda: None)
    monkeypatch.setattr("copilot.main.get_schema_ddl", lambda: "")
    monkeypatch.setattr("copilot.main.dispose_engine", lambda: None)

    async def _async_noop() -> None:
        return None

    async def _async_none() -> None:
        return None

    monkeypatch.setattr("copilot.main.setup_checkpointer", _async_noop)
    monkeypatch.setattr("copilot.main.dispose_checkpointer", _async_noop)
    monkeypatch.setattr("copilot.main.get_checkpointer", _async_none)

    from copilot.main import app

    with TestClient(app) as c:
        yield c


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body
