"""Dashboard CRUD + card-extract helpers (Phase 2.1 / ADR 0020).

Two tables, both owned by us (no LangGraph entanglement this time —
``saved_conversations`` is the only place that crosses paths with
PostgresSaver, and Phase 2.1 cards are snapshots that don't need
to read LangGraph state on the hot path):

* ``dashboards``       — one row per named grid.
* ``dashboard_items``  — one row per card; foreign keys back to
                         the dashboard with ``ON DELETE CASCADE``.

The service is sync SQLAlchemy throughout, like ``saved.py``'s CRUD
piece. The only async-flavoured helper is ``extract_card_from_turn``
which reaches into LangGraph state to grab the snapshot data; it
delegates that read to ``copilot.saved.replay_conversation_async``
so there's exactly one place in the codebase that knows how
``dialogue`` is reconstructed.

Card-render contract: the FE renders ONLY from snapshot columns.
``source_thread_id`` / ``source_turn_index`` are debug breadcrumbs,
not render inputs. That's why a deleted source conversation never
breaks a card.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from copilot.db import get_engine

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dashboard:
    """One row from ``dashboards``, minus the cascade-loaded items.

    Sequence: ``GET /dashboards`` returns these; the detail endpoint
    ``GET /dashboards/{id}`` joins in the items list below."""

    id: str
    title: str
    description: str | None
    created_at: str  # ISO-8601
    updated_at: str
    item_count: int  # cheap aggregate, useful for the list page


@dataclass(frozen=True)
class DashboardItem:
    """One row from ``dashboard_items`` — a card snapshot + grid pos."""

    id: str
    dashboard_id: str
    source_thread_id: str | None
    source_turn_index: int | None
    title: str
    sql: str | None
    answer: str | None
    chart_kind: str | None
    chart_spec: dict[str, Any] | None
    rows: list[dict[str, Any]] | None
    row_count: int | None
    insight: dict[str, Any] | None
    # Phase 2.3.1 — frozen critic verdict (ADR 0021). NULL for
    # ``ok`` verdicts (the FE shows nothing anyway), populated for
    # ``suspicious`` / ``wrong`` so the badge survives extraction.
    critic: dict[str, Any] | None
    position_x: int
    position_y: int
    width: int
    height: int
    created_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_dashboard(row: Any) -> dict[str, Any]:
    return {
        "id": row[0],
        "title": row[1],
        "description": row[2],
        "created_at": row[3].isoformat() if row[3] else None,
        "updated_at": row[4].isoformat() if row[4] else None,
        "item_count": int(row[5]) if len(row) > 5 and row[5] is not None else 0,
    }


def _row_to_item(row: Any) -> dict[str, Any]:
    # SQLAlchemy returns JSONB as dict / list directly; defensive
    # ``json.loads`` covers the rare case a driver hands back a str.
    def _json(v: Any) -> Any:
        if isinstance(v, (dict, list)):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return None
        return None

    return {
        "id": row[0],
        "dashboard_id": row[1],
        "source_thread_id": row[2],
        "source_turn_index": row[3],
        "title": row[4],
        "sql": row[5],
        "answer": row[6],
        "chart_kind": row[7],
        "chart_spec": _json(row[8]),
        "rows": _json(row[9]),
        "row_count": row[10],
        "insight": _json(row[11]),
        # Phase 2.3.1 — critic verdict; new tail column so the index
        # shift is local to this row and to ``_ITEM_COLS`` below.
        "critic": _json(row[12]),
        "position_x": int(row[13]),
        "position_y": int(row[14]),
        "width": int(row[15]),
        "height": int(row[16]),
        "created_at": row[17].isoformat() if row[17] else None,
    }


_ITEM_COLS = (
    "id, dashboard_id, source_thread_id, source_turn_index, "
    "title, sql, answer, chart_kind, chart_spec, rows, row_count, "
    "insight, critic, position_x, position_y, width, height, created_at"
)


# ---------------------------------------------------------------------------
# CRUD — dashboards
# ---------------------------------------------------------------------------


def create_dashboard(
    *,
    title: str,
    description: str | None = None,
    engine: Engine | None = None,
) -> dict[str, Any]:
    """Insert a new dashboard. ``id`` is a server-generated UUID so
    callers don't accidentally collide."""
    eng = engine or get_engine()
    new_id = str(uuid.uuid4())
    with eng.begin() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO dashboards (id, title, description)
                VALUES (:id, :title, :description)
                RETURNING id, title, description, created_at, updated_at
                """
            ),
            {"id": new_id, "title": title, "description": description},
        ).fetchone()
    assert row is not None
    return _row_to_dashboard((*row, 0))  # 0 items at creation time


def list_dashboards(
    *, engine: Engine | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """All dashboards, newest-updated first, with item_count joined."""
    eng = engine or get_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT d.id, d.title, d.description, d.created_at, d.updated_at,
                       COALESCE(c.cnt, 0) AS item_count
                FROM dashboards d
                LEFT JOIN (
                    SELECT dashboard_id, count(*) AS cnt
                    FROM dashboard_items
                    GROUP BY dashboard_id
                ) c ON c.dashboard_id = d.id
                ORDER BY d.updated_at DESC
                LIMIT :lim
                """
            ),
            {"lim": int(limit)},
        ).fetchall()
    return [_row_to_dashboard(r) for r in rows]


def get_dashboard(
    dashboard_id: str, *, engine: Engine | None = None
) -> dict[str, Any]:
    """Return one dashboard + its items list in render order.

    Raises ``KeyError`` if the dashboard does not exist; the HTTP
    layer maps that to 404.
    """
    eng = engine or get_engine()
    with eng.connect() as conn:
        head = conn.execute(
            text(
                """
                SELECT id, title, description, created_at, updated_at
                FROM dashboards
                WHERE id = :id
                """
            ),
            {"id": dashboard_id},
        ).fetchone()
        if head is None:
            raise KeyError(dashboard_id)
        items = conn.execute(
            text(
                f"""
                SELECT {_ITEM_COLS}
                FROM dashboard_items
                WHERE dashboard_id = :id
                ORDER BY created_at ASC
                """
            ),
            {"id": dashboard_id},
        ).fetchall()
    return {
        **_row_to_dashboard((*head, len(items))),
        "items": [_row_to_item(r) for r in items],
    }


def update_dashboard(
    dashboard_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    engine: Engine | None = None,
) -> dict[str, Any]:
    """Patch the title / description. Fields left as ``None`` keep
    their current value; bumping ``updated_at`` is automatic so the
    sorted list reflects the recent edit.

    Raises ``KeyError`` if the row doesn't exist.
    """
    eng = engine or get_engine()
    with eng.begin() as conn:
        existing = conn.execute(
            text("SELECT title, description FROM dashboards WHERE id = :id"),
            {"id": dashboard_id},
        ).fetchone()
        if existing is None:
            raise KeyError(dashboard_id)
        new_title = title if title is not None else existing[0]
        new_desc = description if description is not None else existing[1]
        row = conn.execute(
            text(
                """
                UPDATE dashboards
                   SET title = :title,
                       description = :description,
                       updated_at = now()
                 WHERE id = :id
                RETURNING id, title, description, created_at, updated_at
                """
            ),
            {"id": dashboard_id, "title": new_title, "description": new_desc},
        ).fetchone()
    assert row is not None
    return _row_to_dashboard((*row, 0))


def delete_dashboard(
    dashboard_id: str, *, engine: Engine | None = None
) -> bool:
    """Cascade-delete dashboard and all its items. Returns True iff
    a dashboard row was actually removed."""
    eng = engine or get_engine()
    with eng.begin() as conn:
        result = conn.execute(
            text("DELETE FROM dashboards WHERE id = :id"),
            {"id": dashboard_id},
        )
        return result.rowcount > 0


# ---------------------------------------------------------------------------
# CRUD — items
# ---------------------------------------------------------------------------


def add_item(
    dashboard_id: str,
    *,
    snapshot: dict[str, Any],
    engine: Engine | None = None,
) -> dict[str, Any]:
    """Insert a card on ``dashboard_id`` from a snapshot dict.

    ``snapshot`` is the projected assistant-turn payload (built by the
    ``main.py`` endpoint from one entry of ``replay_conversation_async``
    + optional caller-supplied title / position). Fields:

    * ``title``                — required
    * ``sql`` / ``answer`` / ``chart_kind`` / ``chart_spec`` /
      ``rows`` / ``row_count`` / ``insight`` — snapshot of the turn
    * ``source_thread_id`` / ``source_turn_index`` — provenance
    * ``position_x`` / ``position_y`` / ``width`` / ``height`` —
      optional, defaults from the DDL

    Raises ``KeyError`` when the dashboard doesn't exist.
    """
    eng = engine or get_engine()
    new_id = str(uuid.uuid4())
    with eng.begin() as conn:
        # Guard against orphan items — easy to miss with no FK check
        # because we're INSERTing literal IDs.
        exists = conn.execute(
            text("SELECT 1 FROM dashboards WHERE id = :id"),
            {"id": dashboard_id},
        ).fetchone()
        if exists is None:
            raise KeyError(dashboard_id)

        row = conn.execute(
            text(
                f"""
                INSERT INTO dashboard_items (
                    id, dashboard_id,
                    source_thread_id, source_turn_index,
                    title, sql, answer, chart_kind,
                    chart_spec, rows, row_count, insight, critic,
                    position_x, position_y, width, height
                )
                VALUES (
                    :id, :dashboard_id,
                    :source_thread_id, :source_turn_index,
                    :title, :sql, :answer, :chart_kind,
                    CAST(:chart_spec AS jsonb),
                    CAST(:rows AS jsonb),
                    :row_count,
                    CAST(:insight AS jsonb),
                    CAST(:critic AS jsonb),
                    :position_x, :position_y, :width, :height
                )
                RETURNING {_ITEM_COLS}
                """
            ),
            {
                "id": new_id,
                "dashboard_id": dashboard_id,
                "source_thread_id": snapshot.get("source_thread_id"),
                "source_turn_index": snapshot.get("source_turn_index"),
                "title": snapshot["title"],
                "sql": snapshot.get("sql"),
                "answer": snapshot.get("answer"),
                "chart_kind": snapshot.get("chart_kind"),
                "chart_spec": json.dumps(snapshot["chart_spec"])
                if snapshot.get("chart_spec") is not None
                else None,
                "rows": json.dumps(snapshot["rows"])
                if snapshot.get("rows") is not None
                else None,
                "row_count": snapshot.get("row_count"),
                "insight": json.dumps(snapshot["insight"])
                if snapshot.get("insight") is not None
                else None,
                # Phase 2.3.1 — store the critic verdict alongside
                # the snapshot. ``ok`` verdicts are stored too (NULL
                # only when the caller didn't supply one) so a future
                # consumer can distinguish "critic ran and said ok"
                # from "critic didn't run on this turn".
                "critic": json.dumps(snapshot["critic"])
                if snapshot.get("critic") is not None
                else None,
                "position_x": int(snapshot.get("position_x", 0)),
                "position_y": int(snapshot.get("position_y", 0)),
                "width": int(snapshot.get("width", 4)),
                "height": int(snapshot.get("height", 3)),
            },
        ).fetchone()

        # Bump the parent dashboard's updated_at so list ordering moves it
        # to the top — the user just touched it by adding a card.
        conn.execute(
            text("UPDATE dashboards SET updated_at = now() WHERE id = :id"),
            {"id": dashboard_id},
        )

    assert row is not None
    return _row_to_item(row)


def update_item(
    item_id: str,
    *,
    title: str | None = None,
    position_x: int | None = None,
    position_y: int | None = None,
    width: int | None = None,
    height: int | None = None,
    engine: Engine | None = None,
) -> dict[str, Any]:
    """Patch the user-editable bits of a card. Snapshot fields
    (sql/answer/chart_*) are deliberately NOT in the surface — the FE
    can't accidentally rewrite a card's data, only its title and
    grid position.

    Raises ``KeyError`` when the item doesn't exist."""
    eng = engine or get_engine()
    with eng.begin() as conn:
        existing = conn.execute(
            text(
                """
                SELECT title, position_x, position_y, width, height, dashboard_id
                FROM dashboard_items WHERE id = :id
                """
            ),
            {"id": item_id},
        ).fetchone()
        if existing is None:
            raise KeyError(item_id)

        new_title = title if title is not None else existing[0]
        new_x = position_x if position_x is not None else existing[1]
        new_y = position_y if position_y is not None else existing[2]
        new_w = width if width is not None else existing[3]
        new_h = height if height is not None else existing[4]
        parent_id = existing[5]

        row = conn.execute(
            text(
                f"""
                UPDATE dashboard_items
                   SET title = :title,
                       position_x = :x,
                       position_y = :y,
                       width = :w,
                       height = :h
                 WHERE id = :id
                RETURNING {_ITEM_COLS}
                """
            ),
            {
                "id": item_id,
                "title": new_title,
                "x": int(new_x),
                "y": int(new_y),
                "w": int(new_w),
                "h": int(new_h),
            },
        ).fetchone()

        # Same updated_at bump as add_item — any item edit floats the
        # parent dashboard to the top of the list.
        conn.execute(
            text("UPDATE dashboards SET updated_at = now() WHERE id = :id"),
            {"id": parent_id},
        )
    assert row is not None
    return _row_to_item(row)


def delete_item(item_id: str, *, engine: Engine | None = None) -> bool:
    """Drop one card. Returns True iff it actually existed.

    The parent dashboard's ``updated_at`` is bumped to reflect the
    layout change."""
    eng = engine or get_engine()
    with eng.begin() as conn:
        existing = conn.execute(
            text("SELECT dashboard_id FROM dashboard_items WHERE id = :id"),
            {"id": item_id},
        ).fetchone()
        if existing is None:
            return False
        parent_id = existing[0]
        conn.execute(
            text("DELETE FROM dashboard_items WHERE id = :id"),
            {"id": item_id},
        )
        conn.execute(
            text("UPDATE dashboards SET updated_at = now() WHERE id = :id"),
            {"id": parent_id},
        )
    return True


# ---------------------------------------------------------------------------
# Card-extract helper — bridges a conversation turn into a snapshot
# ---------------------------------------------------------------------------


def snapshot_from_replay_turn(
    *,
    thread_id: str,
    turn_index: int,
    user_question: str,
    assistant_turn: dict[str, Any],
    title: str | None = None,
) -> dict[str, Any]:
    """Project an assistant ``Turn`` (the same shape
    ``replay_conversation_async`` returns) into the snapshot dict
    ``add_item`` expects.

    The endpoint pulls a fresh replay, picks the K-th assistant turn,
    pairs it with the K-th user question, and hands the pair here.
    Returning a plain dict (rather than a model object) keeps the
    endpoint simple — it forwards verbatim to ``add_item``.

    ``title`` defaults to the user's question, capped at 80 chars
    like ``derive_title`` in ``saved.py``. The user is expected to
    rename inline from the dashboard grid.
    """
    raw_title = title if title is not None else user_question
    cleaned = raw_title.strip().replace("\n", " ") if raw_title else ""
    if not cleaned:
        effective_title = "Untitled card"
    elif len(cleaned) <= 80:
        effective_title = cleaned
    else:
        effective_title = cleaned[:79].rstrip() + "…"

    return {
        "source_thread_id": thread_id,
        "source_turn_index": turn_index,
        "title": effective_title,
        "sql": assistant_turn.get("sql"),
        "answer": assistant_turn.get("content") or assistant_turn.get("answer"),
        "chart_kind": assistant_turn.get("chart_kind"),
        "chart_spec": assistant_turn.get("chart_spec"),
        "rows": assistant_turn.get("rows"),
        "row_count": assistant_turn.get("row_count"),
        "insight": assistant_turn.get("insight"),
    }
