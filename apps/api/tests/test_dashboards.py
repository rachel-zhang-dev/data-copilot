"""Unit tests for ``copilot.dashboards``.

We only test the pure-Python helpers (``_row_to_item`` JSON
coercion, ``snapshot_from_replay_turn``). SQL-issuing CRUD is
covered against a real Postgres in the integration suite.
"""

from __future__ import annotations

from copilot.dashboards import _row_to_item, snapshot_from_replay_turn


# ---------------------------------------------------------------------------
# _row_to_item — defensive JSONB coercion
# ---------------------------------------------------------------------------


def _row(**overrides: object) -> tuple[object, ...]:
    """Build a row tuple shaped like the ``_ITEM_COLS`` SELECT.

    Phase 2.3.1 added ``critic`` between ``insight`` and ``position_x``;
    callers that don't override it default to ``None`` (mirrors how
    the DB defaults the new JSONB column on insert)."""
    defaults: dict[str, object] = {
        "id": "i1",
        "dashboard_id": "d1",
        "source_thread_id": None,
        "source_turn_index": None,
        "title": "T",
        "sql": None,
        "answer": None,
        "chart_kind": None,
        "chart_spec": None,
        "rows": None,
        "row_count": None,
        "insight": None,
        "critic": None,
        "position_x": 0,
        "position_y": 0,
        "width": 4,
        "height": 3,
        "created_at": None,
    }
    defaults.update(overrides)
    return tuple(defaults.values())


def test_row_to_item_returns_jsonb_as_dict() -> None:
    row = _row(
        chart_spec={"$schema": "vega-lite-v5", "mark": "bar"},
        insight={"headline": "h", "bullets": []},
    )
    out = _row_to_item(row)
    assert isinstance(out["chart_spec"], dict)
    assert out["chart_spec"]["mark"] == "bar"
    assert out["insight"]["headline"] == "h"


def test_row_to_item_parses_json_strings() -> None:
    """psycopg usually hands back JSONB as dict, but some drivers /
    older versions return ``str``. _row_to_item recovers."""
    row = _row(rows='[{"a": 1}, {"a": 2}]')
    out = _row_to_item(row)
    assert isinstance(out["rows"], list)
    assert out["rows"][1]["a"] == 2


def test_row_to_item_drops_malformed_json_silently() -> None:
    """A row with garbage in a JSONB column shouldn't blow up the
    whole endpoint — better to return ``None`` for that one field
    and let the FE render an empty card than 500 the list."""
    row = _row(chart_spec="not valid json")
    out = _row_to_item(row)
    assert out["chart_spec"] is None


def test_row_to_item_passes_ints_and_strings_through() -> None:
    row = _row(
        id="abc",
        title="Top customers",
        sql="SELECT 1",
        position_x=2,
        position_y=4,
        width=6,
        height=5,
    )
    out = _row_to_item(row)
    assert out["id"] == "abc"
    assert out["title"] == "Top customers"
    assert out["sql"] == "SELECT 1"
    assert out["position_x"] == 2
    assert out["width"] == 6


# ---------------------------------------------------------------------------
# snapshot_from_replay_turn — title-derivation + field projection
# ---------------------------------------------------------------------------


def test_snapshot_uses_user_question_as_default_title() -> None:
    snap = snapshot_from_replay_turn(
        thread_id="t1",
        turn_index=2,
        user_question="How many customers in Germany?",
        assistant_turn={"content": "11 customers in Germany.", "sql": "SELECT 1"},
    )
    assert snap["title"] == "How many customers in Germany?"
    assert snap["source_thread_id"] == "t1"
    assert snap["source_turn_index"] == 2
    assert snap["sql"] == "SELECT 1"
    assert snap["answer"] == "11 customers in Germany."


def test_snapshot_explicit_title_overrides_default() -> None:
    snap = snapshot_from_replay_turn(
        thread_id="t1",
        turn_index=1,
        user_question="something long here",
        assistant_turn={"content": "..."},
        title="Custom title",
    )
    assert snap["title"] == "Custom title"


def test_snapshot_title_truncates_long_question_with_ellipsis() -> None:
    long_q = (
        "Investigate why the Beverages category lost market share to "
        "competing soft drinks in the third quarter of 1997 across all "
        "European markets including Germany, UK, France, and Italy"
    )
    snap = snapshot_from_replay_turn(
        thread_id="t1",
        turn_index=1,
        user_question=long_q,
        assistant_turn={},
    )
    assert snap["title"].endswith("…")
    assert len(snap["title"]) <= 80


def test_snapshot_empty_question_falls_back_to_placeholder() -> None:
    snap = snapshot_from_replay_turn(
        thread_id="t1",
        turn_index=1,
        user_question="",
        assistant_turn={},
    )
    assert snap["title"] == "Untitled card"


def test_snapshot_propagates_chart_and_insight_when_present() -> None:
    """If the caller hands the helper a full assistant payload (e.g.
    extracted from a live ``AskResponse`` not from replayed dialogue),
    the snapshot carries every renderable field forward."""
    snap = snapshot_from_replay_turn(
        thread_id="t1",
        turn_index=1,
        user_question="Top customers",
        assistant_turn={
            "content": "USA leads",
            "sql": "SELECT country FROM customers",
            "chart_kind": "bar",
            "chart_spec": {"mark": "bar"},
            "rows": [{"country": "USA"}],
            "row_count": 1,
            "insight": {"headline": "h", "bullets": []},
        },
    )
    assert snap["chart_kind"] == "bar"
    assert snap["chart_spec"] == {"mark": "bar"}
    assert snap["rows"] == [{"country": "USA"}]
    assert snap["row_count"] == 1
    assert snap["insight"]["headline"] == "h"


def test_row_to_item_round_trips_critic_verdict() -> None:
    """Phase 2.3.1 — the critic JSONB column must come through
    ``_row_to_item`` as a dict so the FE can render its CriticBadge."""
    row = _row(
        critic={
            "verdict": "suspicious",
            "reason": "JOIN may fan out duplicates",
            "concerns": ["consider DISTINCT"],
        }
    )
    out = _row_to_item(row)
    assert isinstance(out["critic"], dict)
    assert out["critic"]["verdict"] == "suspicious"
    assert out["critic"]["concerns"] == ["consider DISTINCT"]


def test_row_to_item_critic_null_for_pre_phase_2_3_rows() -> None:
    """Cards extracted before Phase 2.3.1 have NULL in the new
    column; the projection must not crash."""
    row = _row(critic=None)
    out = _row_to_item(row)
    assert out["critic"] is None


def test_snapshot_accepts_answer_field_as_alias_for_content() -> None:
    """Some upstream shapes use ``answer`` (live AskResponse) and some
    use ``content`` (replayed Turn). The helper handles both."""
    snap = snapshot_from_replay_turn(
        thread_id="t1",
        turn_index=1,
        user_question="q",
        assistant_turn={"answer": "from AskResponse"},
    )
    assert snap["answer"] == "from AskResponse"
