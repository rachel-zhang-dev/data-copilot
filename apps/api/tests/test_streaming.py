"""Tests for the week-10 streaming endpoint ``/ask/stream``.

The endpoint wraps LangGraph's ``astream`` and serialises each chunk
as a Server-Sent Event. These tests pin three contracts the front-end
relies on:

1. Happy path emits one ``phase`` event per node activation and a
   final ``done`` event with the full ``AskResponse`` shape.
2. HITL pause emits a ``pending_confirmation`` event and ends the
   stream (no ``done``).
3. Errors raised inside the stream surface as an ``error`` event
   rather than a half-open socket.

We mock the compiled graph so the test doesn't need DeepSeek /
Postgres. The SSE parsing is intentionally hand-rolled here — the
front-end uses ``EventSource`` (also hand-rolled in tests there) so
sharing a third-party parser would mask wire-format bugs.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from langgraph.types import Interrupt

# ---------------------------------------------------------------------------
# SSE wire-format helpers
# ---------------------------------------------------------------------------


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse a complete SSE response body into ``[(event_name, data), ...]``.

    Only handles the subset we emit: each event is exactly ``event:`` +
    ``data:`` lines separated by a blank line. ``data`` is JSON-parsed.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        name = ""
        payload = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                payload = line[len("data: "):]
        if name:
            events.append((name, json.loads(payload) if payload else {}))
    return events


# ---------------------------------------------------------------------------
# Fixtures — TestClient with a fully-mocked graph
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """FastAPI TestClient where every external dependency is mocked.

    Mirrors ``test_health.py`` / ``test_api_ask.py``; the only extra is
    that ``conversation_lock`` is replaced with a no-op so the SSE
    handler doesn't try to talk to a real Postgres advisory-lock pool.
    """
    monkeypatch.setattr("copilot.main.get_engine", lambda: None)
    monkeypatch.setattr("copilot.main.get_schema_ddl", lambda: "")
    monkeypatch.setattr("copilot.main.dispose_engine", lambda: None)

    async def _async_noop() -> None:
        return None

    monkeypatch.setattr("copilot.main.setup_checkpointer", _async_noop)
    monkeypatch.setattr("copilot.main.dispose_checkpointer", _async_noop)
    monkeypatch.setattr("copilot.main.get_checkpointer", _async_noop)

    @asynccontextmanager
    async def _fake_lock(_thread_id: str):
        yield None

    monkeypatch.setattr("copilot.main.conversation_lock", _fake_lock)

    fake_graph = MagicMock()
    fake_graph.ainvoke = AsyncMock(return_value={"turn_index": 1})
    fake_graph.aget_state = AsyncMock()
    monkeypatch.setattr("copilot.main.build_graph", lambda **_k: fake_graph)

    from copilot.main import app

    with TestClient(app) as c:
        c._graph = fake_graph  # type: ignore[attr-defined]
        yield c


def _astream_factory(chunks: list[dict[str, Any]]) -> Any:
    """Build an async iterator that yields the canned chunks. The graph's
    ``astream`` attribute is a regular method (not an async method), so
    we return a sync function that returns an async generator."""

    async def _gen():
        for chunk in chunks:
            yield chunk

    def _astream(*_a: Any, **_k: Any):
        return _gen()

    return _astream


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_stream_emits_phase_events_per_node(client: TestClient) -> None:
    """A successful turn yields one ``phase`` per node, then ``done``
    carrying the final ``AskResponse``."""
    g = client._graph  # type: ignore[attr-defined]
    g.astream = _astream_factory([
        {"classify_intent": {"intent": "data"}},
        {"generate_sql": {"sql": "SELECT count(*) FROM customers"}},
        {"execute_sql": {"row_count": 91}},
        {"summarize_result": {"answer": "There are 91 customers."}},
    ])
    final_state = {
        "question": "How many customers?",
        "sql": "SELECT count(*) FROM customers",
        "sql_result": [{"count": 91}],
        "row_count": 91,
        "answer": "There are 91 customers.",
        "turn_index": 1,
        "chart_kind": "kpi",
    }
    snap = MagicMock()
    snap.values = final_state
    g.aget_state = AsyncMock(return_value=snap)

    with client.stream(
        "POST", "/ask/stream", json={"question": "How many customers?"}
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())

    events = _parse_sse(body)
    names = [name for name, _ in events]
    assert names == ["phase", "phase", "phase", "phase", "done"]

    # Each phase reports the node it came from.
    phase_nodes = [data["node"] for name, data in events[:4]]
    assert phase_nodes == [
        "classify_intent",
        "generate_sql",
        "execute_sql",
        "summarize_result",
    ]
    # The diff filter keeps user-facing fields.
    assert events[1][1]["diff"]["sql"] == "SELECT count(*) FROM customers"
    assert events[2][1]["diff"]["row_count"] == 91

    # ``done`` mirrors AskResponse — same shape as /ask.
    done = events[-1][1]
    assert done["status"] == "ok"
    assert done["row_count"] == 91
    assert done["answer"] == "There are 91 customers."
    assert done["chart_kind"] == "kpi"
    assert done["conversation_id"]


def test_internal_nodes_tagged_so_ui_can_hide_them(client: TestClient) -> None:
    g = client._graph  # type: ignore[attr-defined]
    g.astream = _astream_factory([
        {"reset_per_turn": {"turn_index": 1}},
        {"classify_intent": {"intent": "data"}},
        {"append_to_dialogue": {}},
    ])
    snap = MagicMock()
    snap.values = {"turn_index": 1}
    g.aget_state = AsyncMock(return_value=snap)

    with client.stream("POST", "/ask/stream", json={"question": "q"}) as r:
        body = "".join(r.iter_text())
    events = _parse_sse(body)

    by_node = {data["node"]: data for name, data in events if name == "phase"}
    assert by_node["reset_per_turn"]["internal"] is True
    assert by_node["classify_intent"]["internal"] is False
    assert by_node["append_to_dialogue"]["internal"] is True


# ---------------------------------------------------------------------------
# HITL pause
# ---------------------------------------------------------------------------


def test_stream_ends_on_pending_confirmation(client: TestClient) -> None:
    """An interrupt mid-stream emits ``pending_confirmation`` and ends —
    no ``done`` event because the turn hasn't finished."""
    g = client._graph  # type: ignore[attr-defined]
    g.astream = _astream_factory([
        {"check_risk": {"sql": "SELECT * FROM big LIMIT 100"}},
        {
            "__interrupt__": (
                Interrupt(
                    value={
                        "sql": "SELECT * FROM big LIMIT 100",
                        "total_cost": 9000.0,
                        "threshold": 1000.0,
                        "reason": "too costly",
                    },
                    id="i1",
                ),
            )
        },
    ])

    with client.stream("POST", "/ask/stream", json={"question": "show all"}) as r:
        body = "".join(r.iter_text())
    events = _parse_sse(body)

    names = [name for name, _ in events]
    assert names[-1] == "pending_confirmation"
    assert "done" not in names

    pending = events[-1][1]["pending_risk"]
    assert pending["total_cost"] == 9000.0
    assert pending["reason"] == "too costly"
    assert events[-1][1]["conversation_id"]


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_unhandled_exception_surfaces_as_error_event(client: TestClient) -> None:
    """A crash inside the generator must NOT leave the connection
    half-open — emit an ``error`` event so the client has a deterministic
    signal to render a failure UI."""
    g = client._graph  # type: ignore[attr-defined]

    async def _gen() -> Any:
        yield {"classify_intent": {"intent": "data"}}
        raise RuntimeError("kaboom")

    def _astream(*_a: Any, **_k: Any):
        return _gen()

    g.astream = _astream

    with client.stream("POST", "/ask/stream", json={"question": "q"}) as r:
        body = "".join(r.iter_text())
    events = _parse_sse(body)

    names = [name for name, _ in events]
    assert names == ["phase", "error"]
    err = events[-1][1]
    assert err["type"] == "RuntimeError"
    assert "kaboom" in err["detail"]


def test_resume_on_non_paused_thread_400(client: TestClient) -> None:
    g = client._graph  # type: ignore[attr-defined]
    snap = MagicMock()
    snap.interrupts = []
    g.aget_state = AsyncMock(return_value=snap)
    r = client.post("/ask/stream", json={"conversation_id": "abc", "resume": "approve"})
    assert r.status_code == 400
    assert "no pending confirmation" in r.json()["detail"]


def test_heartbeat_emitted_during_quiet_periods(client: TestClient) -> None:
    """The streaming generator wraps ``astream`` in ``asyncio.wait_for``
    so reverse-proxy idle timers don't drop the connection. Drive a
    slow stream and assert at least one ``: heartbeat`` comment line
    appears in the response body."""
    import asyncio

    g = client._graph  # type: ignore[attr-defined]

    # Patch the module's heartbeat interval down to ~50ms for the test
    # so we don't have to wait the production 15s default.
    import copilot.main as main_mod

    original_interval = main_mod._HEARTBEAT_INTERVAL_S
    main_mod._HEARTBEAT_INTERVAL_S = 0.05

    async def _slow_gen():
        # First chunk after enough quiet time that ≥2 heartbeats fire.
        await asyncio.sleep(0.2)
        yield {"classify_intent": {"intent": "data"}}

    def _astream(*_a: Any, **_k: Any):
        return _slow_gen()

    g.astream = _astream
    snap = MagicMock()
    snap.values = {"turn_index": 1}
    g.aget_state = AsyncMock(return_value=snap)

    try:
        with client.stream("POST", "/ask/stream", json={"question": "q"}) as r:
            body = "".join(r.iter_text())
    finally:
        main_mod._HEARTBEAT_INTERVAL_S = original_interval

    # SSE comment lines start with ":" — count at least one in the body.
    heartbeats = [line for line in body.splitlines() if line.startswith(": heartbeat")]
    assert len(heartbeats) >= 1, f"expected ≥1 heartbeat, got body: {body!r}"


def test_non_dict_diff_is_tolerated(client: TestClient) -> None:
    """LangGraph occasionally surfaces ``None`` (or other non-mapping
    values) as a chunk's diff — observed in the wild on the chitchat
    branch where ``compact_history`` returns ``{}`` and a subsequent
    terminal-state update arrives with ``diff=None``. The stream must
    not 500 on this; treat it as an empty diff and keep going.

    Regression test for the AttributeError raised by ``_phase_payload``
    when it called ``.items()`` on ``None``.
    """
    g = client._graph  # type: ignore[attr-defined]
    g.astream = _astream_factory([
        {"classify_intent": {"intent": "chitchat"}},
        {"small_talk": {"answer": "hi"}},
        {"append_to_dialogue": {}},
        {"compact_history": None},
    ])
    snap = MagicMock()
    snap.values = {"answer": "hi", "turn_index": 1}
    g.aget_state = AsyncMock(return_value=snap)

    with client.stream("POST", "/ask/stream", json={"question": "hello"}) as r:
        body = "".join(r.iter_text())
    events = _parse_sse(body)

    names = [name for name, _ in events]
    assert names[-1] == "done", f"expected stream to finish, got {names}"
    assert "error" not in names

    phase_by_node = {data["node"]: data for name, data in events if name == "phase"}
    assert phase_by_node["compact_history"]["diff"] == {}


def test_resume_on_paused_thread_streams_continuation(client: TestClient) -> None:
    """Resume after pause: stream picks up at the interrupted node and
    runs to completion."""
    g = client._graph  # type: ignore[attr-defined]
    snap = MagicMock()
    snap.interrupts = (Interrupt(value={"sql": "x"}, id="i1"),)
    g.aget_state = AsyncMock(return_value=snap)
    g.astream = _astream_factory([
        {"await_confirmation": {"risk_decision": "approved"}},
        {"execute_sql": {"row_count": 5}},
        {"summarize_result": {"answer": "Done."}},
    ])
    final = MagicMock()
    final.values = {"answer": "Done.", "turn_index": 1, "row_count": 5, "sql": "x"}
    g.aget_state = AsyncMock(side_effect=[snap, final])

    with client.stream(
        "POST",
        "/ask/stream",
        json={"conversation_id": "abc-1", "resume": "approve"},
    ) as r:
        body = "".join(r.iter_text())
    events = _parse_sse(body)

    names = [name for name, _ in events]
    assert names[-1] == "done"
    assert events[-1][1]["answer"] == "Done."
