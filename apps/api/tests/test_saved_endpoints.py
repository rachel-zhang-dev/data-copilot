"""HTTP-level tests for the Phase 1.4 saved-conversation endpoints.

Mirrors the pattern used by ``test_health.py`` / ``test_api_ask.py``:
DB-touching helpers are monkey-patched so the suite stays mock-only
and runs without Postgres. The real CRUD round-trip is verified in
the integration suite.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """FastAPI TestClient with DB-touching deps stubbed.

    Same pattern as ``test_health.py``: the lifespan hooks expect
    async helpers, so the no-ops have to be coroutines."""
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
# POST /conversations/{id}/save
# ---------------------------------------------------------------------------


def test_save_endpoint_passes_payload_to_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Body fields propagate verbatim to ``save_conversation``. Title
    was explicitly supplied so the auto-title path doesn't fire."""
    captured: dict[str, Any] = {}

    def _fake_save(
        thread_id: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        notes: str | None = None,
        first_question: str | None = None,
    ) -> dict[str, Any]:
        captured.update(
            {
                "thread_id": thread_id,
                "title": title,
                "tags": tags,
                "notes": notes,
                "first_question": first_question,
            }
        )
        return {
            "thread_id": thread_id,
            "title": title or "auto-derived",
            "tags": tags or [],
            "notes": notes,
            "pinned_at": "2026-06-01T00:00:00+00:00",
            "updated_at": "2026-06-01T00:00:00+00:00",
        }

    monkeypatch.setattr("copilot.main.save_conversation", _fake_save)
    # Even with explicit title, the endpoint may still try to fetch
    # the first question depending on path — install a stub.
    async def _fake_first(_g: Any, _t: str) -> str | None:
        return "Imaginary first question"

    monkeypatch.setattr("copilot.main.first_question_async", _fake_first)
    from copilot.main import app
    app.state.sql_graph = MagicMock()

    r = client.post(
        "/conversations/abc-123/save",
        json={"title": "My pin", "tags": ["sales", "1997"], "notes": "demo"},
    )
    assert r.status_code == 200
    assert captured["thread_id"] == "abc-123"
    assert captured["title"] == "My pin"
    assert captured["tags"] == ["sales", "1997"]
    assert captured["notes"] == "demo"
    # With explicit title, first_question_async should NOT have been
    # consulted — captured["first_question"] stays None.
    assert captured["first_question"] is None
    body = r.json()
    assert body["title"] == "My pin"


def test_save_endpoint_auto_title_zero_friction(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty body (the Pin button's default) → the endpoint calls
    ``first_question_async`` and passes the result into the service
    so ``derive_title`` can run."""
    captured: dict[str, Any] = {}

    def _fake_save(
        thread_id: str,
        *,
        title: str | None = None,
        first_question: str | None = None,
        **_,
    ) -> dict[str, Any]:
        captured.update({"title": title, "first_question": first_question})
        return {
            "thread_id": thread_id,
            "title": "How many customers are there?",
            "tags": [],
            "notes": None,
            "pinned_at": "x",
            "updated_at": "x",
        }

    async def _fake_first(_g: Any, _t: str) -> str | None:
        return "How many customers are there?"

    monkeypatch.setattr("copilot.main.save_conversation", _fake_save)
    monkeypatch.setattr("copilot.main.first_question_async", _fake_first)
    from copilot.main import app
    app.state.sql_graph = MagicMock()

    r = client.post("/conversations/abc/save", json={})
    assert r.status_code == 200
    assert captured["title"] is None
    assert captured["first_question"] == "How many customers are there?"
    assert r.json()["title"].startswith("How many")


# ---------------------------------------------------------------------------
# DELETE /conversations/{id}/save
# ---------------------------------------------------------------------------


def test_unsave_endpoint_404_when_not_pinned(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("copilot.main.unsave_conversation", lambda tid: False)
    r = client.delete("/conversations/never-pinned/save")
    assert r.status_code == 404
    assert r.json()["detail"] == "not pinned"


def test_unsave_endpoint_200_when_removed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("copilot.main.unsave_conversation", lambda tid: True)
    r = client.delete("/conversations/abc/save")
    assert r.status_code == 200
    assert r.json() == {"unsaved": True}


# ---------------------------------------------------------------------------
# GET /conversations/saved
# ---------------------------------------------------------------------------


def test_list_saved_returns_items_with_preview(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint composes a sync ``list_saved`` (rows) with an
    async ``add_previews_async`` (preview block) — both should
    show up in the response."""
    monkeypatch.setattr(
        "copilot.main.list_saved",
        lambda: [
            {
                "thread_id": "a",
                "title": "T1",
                "tags": [],
                "notes": None,
                "pinned_at": "2026-06-01T00:00:00+00:00",
                "updated_at": "2026-06-01T00:00:00+00:00",
                "last_question": None,
                "last_answer": None,
                "turn_count": 0,
            }
        ],
    )

    async def _fake_previews(_g: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {**r, "last_question": "q", "last_answer": "a", "turn_count": 1}
            for r in rows
        ]

    monkeypatch.setattr("copilot.main.add_previews_async", _fake_previews)
    from copilot.main import app
    app.state.sql_graph = MagicMock()

    r = client.get("/conversations/saved")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["title"] == "T1"
    assert item["last_question"] == "q"
    assert item["turn_count"] == 1


def test_list_saved_empty(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("copilot.main.list_saved", lambda: [])

    async def _fake_previews(_g: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return rows

    monkeypatch.setattr("copilot.main.add_previews_async", _fake_previews)
    from copilot.main import app
    app.state.sql_graph = MagicMock()

    r = client.get("/conversations/saved")
    assert r.status_code == 200
    assert r.json() == {"items": []}


# ---------------------------------------------------------------------------
# GET /conversations/{id}/messages
# ---------------------------------------------------------------------------


def test_replay_endpoint_returns_messages(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_replay(_g: Any, _t: str) -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a", "sql": "SELECT 1", "row_count": 1},
        ]

    monkeypatch.setattr("copilot.main.replay_conversation_async", _fake_replay)
    # The lifespan stub left ``sql_graph`` unset; set one so the handler
    # doesn't AttributeError.
    from copilot.main import app
    app.state.sql_graph = MagicMock()

    r = client.get("/conversations/abc/messages")
    assert r.status_code == 200
    body = r.json()
    assert body["thread_id"] == "abc"
    assert len(body["messages"]) == 2
    assert body["messages"][1]["sql"] == "SELECT 1"


def test_replay_endpoint_404_on_unknown_thread(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_replay_raises(_g: Any, _t: str) -> list[dict[str, Any]]:
        raise KeyError("nope")

    monkeypatch.setattr("copilot.main.replay_conversation_async", _fake_replay_raises)
    from copilot.main import app
    app.state.sql_graph = MagicMock()

    r = client.get("/conversations/nope/messages")
    assert r.status_code == 404
