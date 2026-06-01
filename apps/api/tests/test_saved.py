"""Unit tests for ``copilot.saved``.

The pure-Python helpers (``derive_title``, the dialogue-shape parser)
are tested directly; the SQL-issuing CRUD is exercised against the
real Postgres in ``test_agent_integration.py``-style integration
runs only when ``-m integration`` is selected. Here we mock the
engine.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from copilot import saved
from copilot.saved import (
    SavedConversation,
    add_previews_async,
    derive_title,
    first_question_async,
    replay_conversation_async,
    state_preview_async,
)


# ---------------------------------------------------------------------------
# derive_title — the zero-friction auto-title heuristic
# ---------------------------------------------------------------------------


def test_derive_title_short_question_kept_verbatim() -> None:
    assert derive_title("How many customers?") == "How many customers?"


def test_derive_title_long_question_truncates_with_ellipsis() -> None:
    q = "Investigate the drop in Beverages sales during Q3 1997 across all regions and channels"
    out = derive_title(q)
    assert out.endswith("…")
    assert len(out) <= 80


def test_derive_title_none_returns_placeholder() -> None:
    assert derive_title(None) == "Untitled conversation"
    assert derive_title("") == "Untitled conversation"
    assert derive_title("   ") == "Untitled conversation"


def test_derive_title_strips_newlines() -> None:
    """Multi-line questions get joined on a single line so they don't
    break the drawer row's single-line ellipsis."""
    assert derive_title("Line 1\nLine 2") == "Line 1 Line 2"


# ---------------------------------------------------------------------------
# Async LangGraph helpers — first_question / state_preview / replay
# All read through ``aget_state`` so the test fixtures snapshot a
# state dict the same way.
# ---------------------------------------------------------------------------


def _stub_graph(dialogue: list[dict[str, Any]]) -> Any:
    """Build a MagicMock graph whose ``aget_state`` returns a snapshot
    whose ``.values["dialogue"]`` is the supplied dialogue list."""
    snapshot = MagicMock()
    snapshot.values = {"dialogue": dialogue, "question": "?", "turn_index": 1}
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=snapshot)
    return graph


@pytest.mark.asyncio
async def test_first_question_async_returns_first_user_turn() -> None:
    graph = _stub_graph(
        [
            {"role": "user", "content": "How many customers?"},
            {"role": "assistant", "content": "91."},
            {"role": "user", "content": "And in Germany?"},
        ]
    )
    out = await first_question_async(graph, "t1")
    assert out == "How many customers?"


@pytest.mark.asyncio
async def test_first_question_async_returns_none_on_empty_dialogue() -> None:
    graph = _stub_graph([])
    assert await first_question_async(graph, "t1") is None


@pytest.mark.asyncio
async def test_first_question_async_skips_non_user_turns() -> None:
    """``role:"user"`` is the only valid match; assistant-only history
    (e.g. a thread that started with a system message somehow) yields
    ``None`` rather than picking the assistant's reply."""
    graph = _stub_graph(
        [
            {"role": "assistant", "content": "I picked the wrong first turn"},
            {"role": "user", "content": "actual question"},
        ]
    )
    out = await first_question_async(graph, "t1")
    assert out == "actual question"


@pytest.mark.asyncio
async def test_state_preview_async_extracts_last_q_a_and_pair_count() -> None:
    graph = _stub_graph(
        [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
    )
    out = await state_preview_async(graph, "t1")
    assert out == {
        "last_question": "q2",
        "last_answer": "a2",
        "turn_count": 2,
    }


@pytest.mark.asyncio
async def test_state_preview_async_swallows_aget_state_errors() -> None:
    """If the graph throws (e.g. thread has no checkpoint), the
    preview returns all-empty rather than blowing up the saved
    list."""
    graph = MagicMock()
    graph.aget_state = AsyncMock(side_effect=RuntimeError("no such thread"))
    out = await state_preview_async(graph, "missing")
    assert out == {"last_question": None, "last_answer": None, "turn_count": 0}


@pytest.mark.asyncio
async def test_add_previews_async_fills_each_row() -> None:
    """The endpoint helper merges preview fields into the row dicts
    that ``list_saved`` returned (which start with all-empty preview)."""
    graph = _stub_graph(
        [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
    )
    rows = [
        {
            "thread_id": "t1",
            "title": "T1",
            "tags": [],
            "notes": None,
            "pinned_at": None,
            "updated_at": None,
            "last_question": None,
            "last_answer": None,
            "turn_count": 0,
        }
    ]
    out = await add_previews_async(graph, rows)
    assert len(out) == 1
    assert out[0]["last_question"] == "q"
    assert out[0]["last_answer"] == "a"
    assert out[0]["turn_count"] == 1


# ---------------------------------------------------------------------------
# replay_conversation_async — the FE's replay path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_async_uses_aget_state_then_extracts_dialogue() -> None:
    snapshot = MagicMock()
    snapshot.values = {
        "dialogue": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a", "sql": "SELECT 1"},
        ]
    }
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=snapshot)

    out = await replay_conversation_async(graph, "thread-x")
    assert len(out) == 2
    assert out[1]["sql"] == "SELECT 1"
    graph.aget_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_replay_async_raises_on_empty_state() -> None:
    snapshot = MagicMock()
    snapshot.values = {}
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=snapshot)

    with pytest.raises(KeyError):
        await replay_conversation_async(graph, "t")


@pytest.mark.asyncio
async def test_replay_async_raises_on_no_dialogue() -> None:
    snapshot = MagicMock()
    snapshot.values = {"dialogue": []}
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=snapshot)

    with pytest.raises(KeyError):
        await replay_conversation_async(graph, "t")


# ---------------------------------------------------------------------------
# CRUD against ``saved_conversations`` — covered in integration tests.
# Here we just smoke-test the dataclass plumbing.
# ---------------------------------------------------------------------------


def test_saved_conversation_dataclass_is_frozen() -> None:
    from datetime import datetime, timezone

    sc = SavedConversation(
        thread_id="t1",
        title="x",
        tags=(),
        notes=None,
        pinned_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    # frozen dataclass: mutation raises
    with pytest.raises(Exception):
        sc.title = "y"  # type: ignore[misc]
