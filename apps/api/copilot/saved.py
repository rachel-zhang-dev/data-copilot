"""Saved-conversation CRUD + dialogue replay (Phase 1.4 / ADR 0019).

Sits one level **above** LangGraph's PostgresSaver — never writes
into the checkpoint tables, only reads from them. Owns its own
``saved_conversations`` table whose only relationship to LangGraph
is via ``thread_id`` (== ``conversation_id``).

The split exists for two reasons:

* The PostgresSaver schema is LangGraph's private surface; future
  versions may change the columns / migration logic. Bookmarks are
  user data and outlive any single checkpointer version.
* Decoupling lets us delete the bookmark without truncating the
  underlying conversation state (so a re-pin is a one-row insert,
  not a graph replay).

Public surface (consumed by ``main.py``):

* ``save_conversation``     — pin / upsert a bookmark.
* ``unsave_conversation``   — delete the bookmark row.
* ``list_saved``            — fetch all bookmarks with a tiny preview
                              (last_question / last_answer / turn_count)
                              for the FE drawer.
* ``replay_conversation``   — rebuild the user-visible dialogue from
                              the latest PostgresSaver checkpoint, so
                              the FE can re-render history when the
                              user clicks a bookmark.

Failure-mode policy: every helper raises ``KeyError`` if the
``thread_id`` is unknown (LangGraph has no checkpoint for it OR the
bookmark row is absent, depending on which side missed). The HTTP
layer maps that to 404 — never silently returns empty.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import text
from sqlalchemy.engine import Engine

from copilot.db import get_engine

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_TITLE_FROM_QUESTION_MAX = 80
"""Auto-title cap. Long-enough to keep meaning ("Top 10 products by
revenue in 1997"), short-enough that a saved-list row doesn't wrap
on a normal-width sidebar."""


def derive_title(first_question: str | None) -> str:
    """Generate a sensible default title when the user didn't supply one.

    Behaviour matches the Phase 1.4 zero-friction contract: pinning
    never blocks on a dialog. If the first question is empty (an
    edge case but possible for replay-from-empty-thread) we return a
    fixed placeholder the FE can replace inline.
    """
    if not first_question:
        return "Untitled conversation"
    cleaned = first_question.strip().replace("\n", " ")
    if not cleaned:
        # All-whitespace input still counts as empty.
        return "Untitled conversation"
    if len(cleaned) <= _TITLE_FROM_QUESTION_MAX:
        return cleaned
    return cleaned[: _TITLE_FROM_QUESTION_MAX - 1].rstrip() + "…"


@dataclass(frozen=True)
class SavedConversation:
    """One row from ``saved_conversations`` + a tiny preview computed
    on the fly from the LangGraph state.

    The FE's drawer renders these — keep the field set small."""

    thread_id: str
    title: str
    tags: tuple[str, ...]
    notes: str | None
    pinned_at: datetime
    updated_at: datetime
    # Preview fields — derived from the latest PostgresSaver checkpoint.
    # ``None`` if the thread has no checkpoint (shouldn't happen for
    # something the user just pinned, but stays robust if it does).
    last_question: str | None = None
    last_answer: str | None = None
    turn_count: int = 0


@dataclass(frozen=True)
class DialogueTurn:
    """One assistant + user pair as the FE wants to render it on replay."""

    role: str  # "user" | "assistant"
    content: str
    sql: str | None = None
    row_count: int | None = None


# ---------------------------------------------------------------------------
# CRUD against ``saved_conversations``
# ---------------------------------------------------------------------------


def save_conversation(
    thread_id: str,
    *,
    title: str | None = None,
    tags: list[str] | None = None,
    notes: str | None = None,
    first_question: str | None = None,
    engine: Engine | None = None,
) -> dict[str, Any]:
    """Pin (or update) a saved-conversation bookmark.

    Idempotent: a second call updates ``title`` / ``tags`` / ``notes``
    and bumps ``updated_at`` without resetting ``pinned_at``. This
    keeps the drawer's "recently pinned" ordering stable across
    minor edits (the user expects "I pinned this yesterday" to stay
    yesterday after a typo fix in the title).

    When ``title`` is ``None`` and the row doesn't yet exist, we auto-
    derive one from ``first_question`` (the caller fetches it through
    the LangGraph ``aget_state`` API and passes it in — we keep
    ``saved.py`` free of LangGraph specifics so the CRUD stays a
    boring sync function). When the row already exists, ``None``
    means "leave the title unchanged".
    """
    eng = engine or get_engine()
    effective_title = title
    effective_tags = list(tags) if tags is not None else None

    with eng.begin() as conn:
        existing = conn.execute(
            text("SELECT title, tags, notes FROM saved_conversations WHERE thread_id = :t"),
            {"t": thread_id},
        ).fetchone()

        if existing is None:
            # First-time pin — auto-derive title if the caller didn't
            # provide one. The caller is responsible for supplying
            # ``first_question`` (typically via the async helper
            # ``first_question_async`` against LangGraph state).
            if effective_title is None:
                effective_title = derive_title(first_question)
            row = conn.execute(
                text(
                    """
                    INSERT INTO saved_conversations (thread_id, title, tags, notes)
                    VALUES (:t, :title, :tags, :notes)
                    RETURNING thread_id, title, tags, notes, pinned_at, updated_at
                    """
                ),
                {
                    "t": thread_id,
                    "title": effective_title,
                    "tags": effective_tags or [],
                    "notes": notes,
                },
            ).fetchone()
        else:
            # Update existing row: preserve fields the caller left as None.
            new_title = effective_title if effective_title is not None else existing[0]
            new_tags = effective_tags if effective_tags is not None else list(existing[1])
            new_notes = notes if notes is not None else existing[2]
            row = conn.execute(
                text(
                    """
                    UPDATE saved_conversations
                       SET title = :title,
                           tags = :tags,
                           notes = :notes,
                           updated_at = now()
                     WHERE thread_id = :t
                    RETURNING thread_id, title, tags, notes, pinned_at, updated_at
                    """
                ),
                {
                    "t": thread_id,
                    "title": new_title,
                    "tags": new_tags,
                    "notes": new_notes,
                },
            ).fetchone()

    assert row is not None  # the RETURNING clause guarantees this
    return {
        "thread_id": row[0],
        "title": row[1],
        "tags": list(row[2]),
        "notes": row[3],
        "pinned_at": row[4].isoformat() if row[4] else None,
        "updated_at": row[5].isoformat() if row[5] else None,
    }


def unsave_conversation(
    thread_id: str, *, engine: Engine | None = None
) -> bool:
    """Drop the bookmark. Returns True iff a row was actually deleted.

    Underlying LangGraph state is left intact — the same thread_id
    can be re-pinned later and the dialogue history will be back.
    """
    eng = engine or get_engine()
    with eng.begin() as conn:
        result = conn.execute(
            text("DELETE FROM saved_conversations WHERE thread_id = :t"),
            {"t": thread_id},
        )
        return result.rowcount > 0


def list_saved(
    *, engine: Engine | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Return every bookmark, newest-first, without any LangGraph-side
    preview. Callers that want preview blocks ``await`` the async
    helper ``add_previews_async`` against the same rows.

    Split into a sync "row fetch" and async "preview" because:

    * LangGraph's ``aget_state`` is the only correct way to read the
      ``dialogue`` reducer field (raw SQL on ``checkpoints`` sees
      ``channel_values`` only, which is shallow scalars; reducer-
      driven fields live in ``checkpoint_blobs`` as msgpack and are
      LangGraph-internal serialisation we don't want to touch).
    * Putting the LangGraph call inside ``saved.py``'s sync API
      would force every consumer through an async event loop. The
      split keeps the CRUD boring + the LangGraph hop where it
      belongs (the async FastAPI handler).
    """
    eng = engine or get_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT thread_id, title, tags, notes, pinned_at, updated_at
                FROM saved_conversations
                ORDER BY pinned_at DESC
                LIMIT :lim
                """
            ),
            {"lim": int(limit)},
        ).fetchall()

    return [
        {
            "thread_id": r[0],
            "title": r[1],
            "tags": list(r[2]),
            "notes": r[3],
            "pinned_at": r[4].isoformat() if r[4] else None,
            "updated_at": r[5].isoformat() if r[5] else None,
            "last_question": None,
            "last_answer": None,
            "turn_count": 0,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Async helpers — read from LangGraph state via aget_state.
#
# ``aget_state`` is the ONLY supported way to read reducer-driven
# state fields (``dialogue`` is one). Raw SQL on the ``checkpoints``
# table only exposes ``channel_values`` which holds shallow scalars;
# anything wrapped in a reducer lives in ``checkpoint_blobs`` as
# msgpack bytes that LangGraph deserialises internally.
# ---------------------------------------------------------------------------


async def first_question_async(
    sql_graph: CompiledStateGraph[Any, Any, Any, Any], thread_id: str
) -> str | None:
    """Return the first user question for ``thread_id`` (used by the
    Pin endpoint to auto-derive a title)."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    snapshot = await sql_graph.aget_state(config)
    values = getattr(snapshot, "values", None) or {}
    dialogue = values.get("dialogue") or []
    for turn in dialogue:
        if not isinstance(turn, dict):
            continue
        if turn.get("role") != "user":
            continue
        content = turn.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return None


async def state_preview_async(
    sql_graph: CompiledStateGraph[Any, Any, Any, Any], thread_id: str
) -> dict[str, Any]:
    """``{last_question, last_answer, turn_count}`` for sidebar rows.

    Robust to missing state — returns all-None / zero rather than
    raising. The sidebar list keeps showing the bookmark title;
    only the preview row is empty."""
    try:
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        snapshot = await sql_graph.aget_state(config)
    except Exception as exc:  # noqa: BLE001
        log.warning("state_preview_async: aget_state failed for %s: %s", thread_id, exc)
        return {"last_question": None, "last_answer": None, "turn_count": 0}
    values = getattr(snapshot, "values", None) or {}
    dialogue = values.get("dialogue") or []
    last_q: str | None = None
    last_a: str | None = None
    pair_count = 0
    for turn in dialogue:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = turn.get("content")
        if role == "user" and isinstance(content, str):
            last_q = content
        elif role == "assistant" and isinstance(content, str):
            last_a = content
            pair_count += 1
    return {
        "last_question": last_q,
        "last_answer": last_a,
        "turn_count": pair_count,
    }


async def add_previews_async(
    sql_graph: CompiledStateGraph[Any, Any, Any, Any],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fill in ``last_question`` / ``last_answer`` / ``turn_count``
    on each row returned by ``list_saved``. Serial rather than
    ``asyncio.gather`` because the per-row cost is dominated by a
    single SQL roundtrip and we already pool the saver connections
    with a small ``max_size`` — bursting N concurrent reads would
    starve other in-flight ``/ask`` calls."""
    out: list[dict[str, Any]] = []
    for r in rows:
        preview = await state_preview_async(sql_graph, r["thread_id"])
        out.append({**r, **preview})
    return out


async def replay_conversation_async(
    sql_graph: CompiledStateGraph[Any, Any, Any, Any], thread_id: str
) -> list[dict[str, Any]]:
    """Async-flavoured replay that goes through LangGraph's
    ``aget_state`` rather than reading the checkpoint table directly.

    This is the preferred path inside the FastAPI handler because:

    * It honours any future PostgresSaver schema migration (LangGraph
      owns the projection from row bytes → ``AgentState``).
    * It can pick the right run-time view of the state (e.g. respect
      a pending interrupt) without ``saved.py`` knowing about HITL.

    ``main.py`` injects the SQL Specialist graph (the one with the
    checkpointer attached), keeping ``saved.py`` decoupled from
    application startup.
    """
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    snapshot = await sql_graph.aget_state(config)
    values = getattr(snapshot, "values", None) or {}
    dialogue = values.get("dialogue") or []
    out: list[dict[str, Any]] = []
    for turn in dialogue:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        if role not in ("user", "assistant"):
            continue
        entry: dict[str, Any] = {
            "role": role,
            "content": str(turn.get("content", "")),
        }
        if "sql" in turn and turn["sql"]:
            entry["sql"] = str(turn["sql"])
        if "row_count" in turn and turn["row_count"] is not None:
            entry["row_count"] = int(turn["row_count"])
        out.append(entry)
    if not out:
        raise KeyError(thread_id)
    return out
