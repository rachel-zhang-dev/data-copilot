"""HTTP-level tests for the Phase 2.1 dashboard endpoints.

Same monkey-patch pattern as ``test_saved_endpoints.py`` — service
functions are stubbed, no Postgres needed."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("copilot.main.get_engine", lambda: None)
    monkeypatch.setattr("copilot.main.get_schema_ddl", lambda: "")
    monkeypatch.setattr("copilot.main.dispose_engine", lambda: None)

    async def _async_noop() -> None:
        return None

    monkeypatch.setattr("copilot.main.setup_checkpointer", _async_noop)
    monkeypatch.setattr("copilot.main.dispose_checkpointer", _async_noop)
    monkeypatch.setattr("copilot.main.get_checkpointer", _async_noop)

    from copilot.main import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# POST /dashboards
# ---------------------------------------------------------------------------


def test_create_requires_title(client: TestClient) -> None:
    r = client.post("/dashboards", json={"description": "no title"})
    assert r.status_code == 400


def test_create_strips_whitespace_and_calls_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def _fake_create(*, title: str, description: str | None) -> dict[str, Any]:
        captured.update({"title": title, "description": description})
        return {
            "id": "d1",
            "title": title,
            "description": description,
            "created_at": "x",
            "updated_at": "x",
            "item_count": 0,
        }

    monkeypatch.setattr("copilot.main.create_dashboard", _fake_create)
    r = client.post("/dashboards", json={"title": "  Q3 Report  "})
    assert r.status_code == 200
    assert captured["title"] == "Q3 Report"
    assert r.json()["id"] == "d1"


# ---------------------------------------------------------------------------
# GET /dashboards + GET /dashboards/{id}
# ---------------------------------------------------------------------------


def test_list_dashboards(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "copilot.main.list_dashboards",
        lambda: [{"id": "d1", "title": "T", "item_count": 3}],
    )
    r = client.get("/dashboards")
    assert r.status_code == 200
    assert r.json()["items"][0]["item_count"] == 3


def test_get_dashboard_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_id: str) -> dict[str, Any]:
        raise KeyError("d-nope")

    monkeypatch.setattr("copilot.main.get_dashboard", _raise)
    r = client.get("/dashboards/d-nope")
    assert r.status_code == 404


def test_get_dashboard_returns_items(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "copilot.main.get_dashboard",
        lambda _id: {
            "id": _id,
            "title": "T",
            "items": [{"id": "i1", "title": "Card 1"}],
        },
    )
    r = client.get("/dashboards/d1")
    body = r.json()
    assert body["items"][0]["title"] == "Card 1"


# ---------------------------------------------------------------------------
# PATCH / DELETE /dashboards/{id}
# ---------------------------------------------------------------------------


def test_patch_dashboard_passes_optional_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def _fake_update(
        dashboard_id: str, *, title: str | None, description: str | None
    ) -> dict[str, Any]:
        captured.update(
            {"dashboard_id": dashboard_id, "title": title, "description": description}
        )
        return {"id": dashboard_id, "title": title, "description": description}

    monkeypatch.setattr("copilot.main.update_dashboard", _fake_update)
    r = client.patch("/dashboards/d1", json={"title": "Renamed"})
    assert r.status_code == 200
    assert captured["title"] == "Renamed"
    assert captured["description"] is None


def test_patch_dashboard_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise KeyError("nope")

    monkeypatch.setattr("copilot.main.update_dashboard", _raise)
    r = client.patch("/dashboards/x", json={"title": "Y"})
    assert r.status_code == 404


def test_delete_dashboard_200(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("copilot.main.delete_dashboard", lambda _id: True)
    r = client.delete("/dashboards/d1")
    assert r.status_code == 200
    assert r.json() == {"deleted": True}


def test_delete_dashboard_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("copilot.main.delete_dashboard", lambda _id: False)
    r = client.delete("/dashboards/d1")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Items — POST / PATCH / DELETE
# ---------------------------------------------------------------------------


def test_add_item_forwards_snapshot_to_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def _fake_add(dashboard_id: str, *, snapshot: dict[str, Any]) -> dict[str, Any]:
        captured.update({"dashboard_id": dashboard_id, "snapshot": snapshot})
        return {"id": "i1", **snapshot}

    monkeypatch.setattr("copilot.main.dashboard_add_item", _fake_add)
    r = client.post(
        "/dashboards/d1/items",
        json={
            "title": "USA outlier",
            "sql": "SELECT 1",
            "chart_kind": "bar",
            "chart_spec": {"mark": "bar"},
            "rows": [{"a": 1}],
            "row_count": 1,
            "source_thread_id": "tid-1",
            "source_turn_index": 1,
            "position_x": 2,
            "position_y": 0,
        },
    )
    assert r.status_code == 200
    snap = captured["snapshot"]
    assert snap["title"] == "USA outlier"
    assert snap["chart_kind"] == "bar"
    assert snap["chart_spec"] == {"mark": "bar"}
    assert snap["position_x"] == 2
    assert snap["width"] == 4  # default


def test_add_item_404_when_dashboard_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise KeyError("d-nope")

    monkeypatch.setattr("copilot.main.dashboard_add_item", _raise)
    r = client.post("/dashboards/d-nope/items", json={"title": "T"})
    assert r.status_code == 404


def test_patch_item_does_not_accept_snapshot_columns(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshot columns are not in DashboardItemPatch — sending them
    should silently get ignored by pydantic (model is strict on
    fields it knows, lenient on unknowns). The service must NEVER
    receive ``sql=...`` from a PATCH."""
    captured: dict[str, Any] = {}

    def _fake_update(item_id: str, **kwargs: Any) -> dict[str, Any]:
        captured.update({"item_id": item_id, **kwargs})
        return {"id": item_id, **kwargs}

    monkeypatch.setattr("copilot.main.dashboard_update_item", _fake_update)
    r = client.patch(
        "/dashboards/d1/items/i1",
        json={"title": "Renamed", "sql": "DROP TABLE x"},
    )
    assert r.status_code == 200
    # Service only got what DashboardItemPatch defined.
    assert "sql" not in captured
    assert captured["title"] == "Renamed"


def test_delete_item_200(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("copilot.main.dashboard_delete_item", lambda _id: True)
    r = client.delete("/dashboards/d1/items/i1")
    assert r.status_code == 200
    assert r.json() == {"deleted": True}


def test_delete_item_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("copilot.main.dashboard_delete_item", lambda _id: False)
    r = client.delete("/dashboards/d1/items/i1")
    assert r.status_code == 404
