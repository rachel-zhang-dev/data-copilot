"""Smoke test for the API health endpoint.

Run with:
    uv run pytest -m "not integration"
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from copilot.main import app


def test_health_endpoint() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
