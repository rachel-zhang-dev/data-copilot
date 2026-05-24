"""Visualisation generation (week 8).

Given the rows that came out of ``execute_sql_node``, this module
classifies the result shape into one of five buckets and (for the
three "real chart" kinds) emits a Vega-Lite v5 specification the UI
can render directly.

Five buckets, by decreasing specificity:

* ``kpi``         — one row, ≥1 quantitative column.
* ``line``        — exactly one temporal column, ≥1 quantitative.
* ``bar``         — exactly one nominal + exactly one quantitative.
* ``grouped_bar`` — exactly one nominal + ≥2 quantitative.
* ``table``       — anything else, or zero rows, or >50 rows.

Heuristic-only by design (see ADR 0009 §"Why heuristic-first").
The classifier is fully deterministic, runs in microseconds, and is
unit-tested with one case per branch. An LLM-fallback hook is left
as future work — the wire format and state plumbing here will support
it without further refactoring.

Failure is fail-soft: if the classifier or builder raises for any
reason, ``visualize_node`` swallows the error, logs a warning, and
returns ``{"chart_kind": "table", "chart_spec": None}``. A
visualisation bug must never block a user from seeing their data.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from copilot.agent.state import AgentState

log = logging.getLogger(__name__)

ChartKind = Literal["kpi", "bar", "line", "grouped_bar", "table"]
FieldKind = Literal["quantitative", "temporal", "nominal"]

VEGA_LITE_SCHEMA_URL = "https://vega.github.io/schema/vega-lite/v5.json"
"""Pinned to v5; matches what ``react-vega`` ships with as of 2026 Q1."""

# Result sets above this row count are rendered as a table regardless
# of shape — a 200-bar chart is unreadable and a 200-row scatter is
# worse. Lowered later when we add pagination / sampling.
MAX_CHART_ROWS = 50

# ---------------------------------------------------------------------------
# Type inference
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?$")


def _is_quantitative(v: Any) -> bool:
    """``bool`` is a subclass of ``int`` in Python; we exclude it so a
    column of booleans is not mistaken for a numeric series."""
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def _is_temporal(v: Any) -> bool:
    if isinstance(v, (date, datetime)):
        return True
    if isinstance(v, str) and _ISO_DATE_RE.match(v):
        return True
    return False


def infer_field_kind(values: list[Any]) -> FieldKind:
    """Best-effort column-type inference from a list of cell values.

    Rules, applied to the **non-null** subset:

    * All quantitative                 -> ``quantitative``
    * All temporal (date / ISO string) -> ``temporal``
    * Otherwise                        -> ``nominal``

    An empty column (all-null) is treated as nominal so the chart
    builder routes it to a categorical axis rather than crashing.
    """
    non_null = [v for v in values if v is not None]
    if not non_null:
        return "nominal"
    if all(_is_quantitative(v) for v in non_null):
        return "quantitative"
    if all(_is_temporal(v) for v in non_null):
        return "temporal"
    return "nominal"


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def classify_shape(rows: list[dict[str, Any]]) -> ChartKind:
    """Pick the chart kind for a result set.

    See module docstring for the decision table.
    """
    if not rows:
        return "table"
    if len(rows) > MAX_CHART_ROWS:
        return "table"

    cols = list(rows[0].keys())
    field_kinds: dict[str, FieldKind] = {
        c: infer_field_kind([r.get(c) for r in rows]) for c in cols
    }
    quant_cols = [c for c, k in field_kinds.items() if k == "quantitative"]
    temp_cols = [c for c, k in field_kinds.items() if k == "temporal"]
    nom_cols = [c for c, k in field_kinds.items() if k == "nominal"]

    # Single-row "KPI" wins over everything as long as there's at
    # least one numeric value to display.
    if len(rows) == 1 and quant_cols:
        return "kpi"

    if len(temp_cols) == 1 and quant_cols:
        return "line"

    if len(nom_cols) == 1 and len(quant_cols) == 1:
        return "bar"

    if len(nom_cols) == 1 and len(quant_cols) >= 2:
        return "grouped_bar"

    return "table"


# ---------------------------------------------------------------------------
# Vega-Lite spec builders
# ---------------------------------------------------------------------------


def _vega_base(rows: list[dict[str, Any]], title: str) -> dict[str, Any]:
    """Common scaffold shared by every chart kind we emit."""
    return {
        "$schema": VEGA_LITE_SCHEMA_URL,
        "title": title,
        "data": {"values": rows},
    }


def _first_of(field_kinds: dict[str, FieldKind], kind: FieldKind) -> str | None:
    return next((c for c, k in field_kinds.items() if k == kind), None)


def _all_of(field_kinds: dict[str, FieldKind], kind: FieldKind) -> list[str]:
    return [c for c, k in field_kinds.items() if k == kind]


def _build_bar(
    rows: list[dict[str, Any]], field_kinds: dict[str, FieldKind], title: str
) -> dict[str, Any]:
    nominal = _first_of(field_kinds, "nominal")
    quant = _first_of(field_kinds, "quantitative")
    spec = _vega_base(rows, title)
    spec["mark"] = "bar"
    spec["encoding"] = {
        "x": {"field": nominal, "type": "nominal", "sort": "-y"},
        "y": {"field": quant, "type": "quantitative"},
        "tooltip": [
            {"field": nominal, "type": "nominal"},
            {"field": quant, "type": "quantitative"},
        ],
    }
    return spec


def _build_line(
    rows: list[dict[str, Any]], field_kinds: dict[str, FieldKind], title: str
) -> dict[str, Any]:
    temporal = _first_of(field_kinds, "temporal")
    quants = _all_of(field_kinds, "quantitative")
    spec = _vega_base(rows, title)
    spec["mark"] = {"type": "line", "point": True}
    if len(quants) == 1:
        spec["encoding"] = {
            "x": {"field": temporal, "type": "temporal"},
            "y": {"field": quants[0], "type": "quantitative"},
            "tooltip": [
                {"field": temporal, "type": "temporal"},
                {"field": quants[0], "type": "quantitative"},
            ],
        }
    else:
        # Multiple quantitative columns over time → fold into a series.
        # Vega-Lite's ``fold`` transform reshapes wide-to-long without
        # us having to re-shape the rows server-side.
        spec["transform"] = [{"fold": quants, "as": ["series", "value"]}]
        spec["encoding"] = {
            "x": {"field": temporal, "type": "temporal"},
            "y": {"field": "value", "type": "quantitative"},
            "color": {"field": "series", "type": "nominal"},
            "tooltip": [
                {"field": temporal, "type": "temporal"},
                {"field": "series", "type": "nominal"},
                {"field": "value", "type": "quantitative"},
            ],
        }
    return spec


def _build_grouped_bar(
    rows: list[dict[str, Any]], field_kinds: dict[str, FieldKind], title: str
) -> dict[str, Any]:
    nominal = _first_of(field_kinds, "nominal")
    quants = _all_of(field_kinds, "quantitative")
    spec = _vega_base(rows, title)
    spec["transform"] = [{"fold": quants, "as": ["series", "value"]}]
    spec["mark"] = "bar"
    spec["encoding"] = {
        "x": {"field": nominal, "type": "nominal"},
        "y": {"field": "value", "type": "quantitative"},
        "color": {"field": "series", "type": "nominal"},
        "xOffset": {"field": "series"},
        "tooltip": [
            {"field": nominal, "type": "nominal"},
            {"field": "series", "type": "nominal"},
            {"field": "value", "type": "quantitative"},
        ],
    }
    return spec


_Builder = Callable[[list[dict[str, Any]], dict[str, FieldKind], str], dict[str, Any]]

_BUILDERS: dict[ChartKind, _Builder] = {
    "bar": _build_bar,
    "line": _build_line,
    "grouped_bar": _build_grouped_bar,
}


def build_vega_lite_spec(
    kind: ChartKind, rows: list[dict[str, Any]], *, title: str = ""
) -> dict[str, Any] | None:
    """Return a Vega-Lite v5 spec for one of the three "real chart" kinds.

    Returns ``None`` for ``kpi`` / ``table`` (the UI renders those
    from the row data directly without a Vega-Lite spec).
    """
    builder = _BUILDERS.get(kind)
    if builder is None:
        return None
    field_kinds = {
        c: infer_field_kind([r.get(c) for r in rows]) for c in rows[0].keys()
    }
    return builder(rows, field_kinds, title)


# ---------------------------------------------------------------------------
# Graph node
# ---------------------------------------------------------------------------


def _title_from_question(question: str | None) -> str:
    """Soft-trim the user's question into a chart title.

    Capitalises the first letter, strips trailing punctuation, and
    caps to 80 chars so a paragraph-length question doesn't blow up
    the chart header. Falls back to an empty string when the
    question is missing (should not happen on the data path).
    """
    if not question:
        return ""
    trimmed = question.strip().rstrip("?.!").strip()
    if not trimmed:
        return ""
    if len(trimmed) > 80:
        trimmed = trimmed[:77].rstrip() + "..."
    return trimmed[:1].upper() + trimmed[1:]


def visualize_node(state: AgentState) -> dict[str, Any]:
    """LangGraph node: classify the result and emit a chart spec.

    Runs after ``summarize_result_node`` on the data success path.
    Chitchat, retry-exhausted-error, and HITL-rejected paths skip
    this node entirely (they have no rows to visualise).

    Failure is fail-soft: any exception inside the classifier or
    builder logs a warning and returns ``("table", None)`` so the
    user still gets their rows.
    """
    rows = state.get("sql_result") or []
    question = state.get("question")
    try:
        kind = classify_shape(rows)
        spec: dict[str, Any] | None = None
        if kind in _BUILDERS:
            spec = build_vega_lite_spec(kind, rows, title=_title_from_question(question))
        log.info(
            "visualize: kind=%s rows=%d spec=%s", kind, len(rows), "yes" if spec else "no"
        )
        return {"chart_kind": kind, "chart_spec": spec}
    except Exception as exc:
        log.warning("visualize failed (%s); falling back to table", exc)
        return {"chart_kind": "table", "chart_spec": None}
