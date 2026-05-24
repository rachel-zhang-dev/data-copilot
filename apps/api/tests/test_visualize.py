"""Unit tests for the week-8 visualisation classifier + Vega-Lite builders.

The classifier is pure (no I/O), so the test surface is exhaustive: one
case per branch of the decision table, plus type-inference edges. The
spec builders are checked for "Vega-Lite v5 shape" — schema URL, mark
type, encoding fields — rather than byte-for-byte equality, so refactors
inside ``visualize.py`` don't churn the tests.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from copilot.agent.visualize import (
    MAX_CHART_ROWS,
    VEGA_LITE_SCHEMA_URL,
    build_vega_lite_spec,
    classify_shape,
    infer_field_kind,
    visualize_node,
)

# ---------------------------------------------------------------------------
# infer_field_kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values,expected",
    [
        ([1, 2, 3], "quantitative"),
        ([1.5, Decimal("2.5")], "quantitative"),
        ([True, False], "nominal"),  # bool is excluded from quantitative
        (["Germany", "France"], "nominal"),
        ([date(2024, 1, 1), date(2024, 2, 1)], "temporal"),
        ([datetime(2024, 1, 1, 12, 0)], "temporal"),
        (["2024-01-01", "2024-02-01"], "temporal"),
        (["2024-01-01T12:00:00", "2024-02-01T13:00"], "temporal"),
        ([None, None], "nominal"),  # all-null defaults to nominal
        ([1, "two"], "nominal"),  # mixed → nominal
        ([1, None, 2], "quantitative"),  # nulls ignored
    ],
)
def test_infer_field_kind(values: list[Any], expected: str) -> None:
    assert infer_field_kind(values) == expected


# ---------------------------------------------------------------------------
# classify_shape — decision table
# ---------------------------------------------------------------------------


def test_empty_rows_classify_as_table() -> None:
    assert classify_shape([]) == "table"


def test_single_row_with_quantitative_is_kpi() -> None:
    assert classify_shape([{"count": 91}]) == "kpi"


def test_single_row_multi_quantitative_still_kpi() -> None:
    assert classify_shape([{"min": 1, "max": 10, "avg": 5.5}]) == "kpi"


def test_single_row_no_quantitative_is_table() -> None:
    """Edge case: a one-row result with no number to highlight has no
    KPI to show — fall back to the table view."""
    assert classify_shape([{"name": "Alice"}]) == "table"


def test_one_nominal_one_quantitative_is_bar() -> None:
    rows = [{"country": "Germany", "count": 11}, {"country": "France", "count": 7}]
    assert classify_shape(rows) == "bar"


def test_one_nominal_multi_quantitative_is_grouped_bar() -> None:
    rows = [
        {"country": "Germany", "orders": 100, "revenue": 5000},
        {"country": "France", "orders": 80, "revenue": 3200},
    ]
    assert classify_shape(rows) == "grouped_bar"


def test_one_temporal_one_quantitative_is_line() -> None:
    rows = [
        {"month": date(2024, 1, 1), "revenue": 100},
        {"month": date(2024, 2, 1), "revenue": 120},
        {"month": date(2024, 3, 1), "revenue": 130},
    ]
    assert classify_shape(rows) == "line"


def test_one_temporal_multi_quantitative_is_line() -> None:
    rows = [
        {"month": "2024-01-01", "orders": 100, "revenue": 5000},
        {"month": "2024-02-01", "orders": 120, "revenue": 6000},
    ]
    assert classify_shape(rows) == "line"


def test_too_many_rows_fall_back_to_table() -> None:
    rows = [{"country": f"C{i}", "n": i} for i in range(MAX_CHART_ROWS + 5)]
    assert classify_shape(rows) == "table"


def test_two_nominal_columns_fall_back_to_table() -> None:
    """No quantitative to chart against → table."""
    rows = [{"a": "x", "b": "y"}, {"a": "z", "b": "w"}]
    assert classify_shape(rows) == "table"


def test_two_nominal_one_quantitative_falls_back_to_table() -> None:
    """Could be heatmap; ADR 0009 explicitly defers that shape."""
    rows = [
        {"country": "DE", "category": "Beverages", "n": 3},
        {"country": "DE", "category": "Produce", "n": 5},
        {"country": "FR", "category": "Beverages", "n": 2},
    ]
    assert classify_shape(rows) == "table"


# ---------------------------------------------------------------------------
# build_vega_lite_spec — spec shape
# ---------------------------------------------------------------------------


def _is_vega_lite(spec: dict[str, Any]) -> bool:
    return spec.get("$schema") == VEGA_LITE_SCHEMA_URL


def test_build_returns_none_for_kpi_and_table() -> None:
    assert build_vega_lite_spec("kpi", [{"a": 1}]) is None
    assert build_vega_lite_spec("table", [{"a": 1}]) is None


def test_build_bar_has_correct_encoding() -> None:
    rows = [{"country": "Germany", "n": 11}, {"country": "France", "n": 7}]
    spec = build_vega_lite_spec("bar", rows, title="Customers by country")
    assert spec is not None
    assert _is_vega_lite(spec)
    assert spec["mark"] == "bar"
    assert spec["encoding"]["x"]["field"] == "country"
    assert spec["encoding"]["x"]["type"] == "nominal"
    assert spec["encoding"]["y"]["field"] == "n"
    assert spec["encoding"]["y"]["type"] == "quantitative"
    assert spec["data"]["values"] == rows
    assert spec["title"] == "Customers by country"


def test_build_line_single_series() -> None:
    rows = [{"d": "2024-01-01", "r": 1}, {"d": "2024-02-01", "r": 2}]
    spec = build_vega_lite_spec("line", rows)
    assert spec is not None
    assert spec["mark"]["type"] == "line"
    assert spec["encoding"]["x"]["type"] == "temporal"
    assert spec["encoding"]["y"]["type"] == "quantitative"
    # Single-series line does NOT use fold
    assert "transform" not in spec


def test_build_line_multi_series_uses_fold() -> None:
    rows = [
        {"d": "2024-01-01", "orders": 10, "revenue": 100},
        {"d": "2024-02-01", "orders": 12, "revenue": 130},
    ]
    spec = build_vega_lite_spec("line", rows)
    assert spec is not None
    assert spec["transform"][0]["fold"] == ["orders", "revenue"]
    assert spec["encoding"]["color"]["field"] == "series"


def test_build_grouped_bar_uses_fold() -> None:
    rows = [
        {"country": "DE", "orders": 100, "revenue": 5000},
        {"country": "FR", "orders": 80, "revenue": 3200},
    ]
    spec = build_vega_lite_spec("grouped_bar", rows)
    assert spec is not None
    assert spec["mark"] == "bar"
    assert spec["transform"][0]["fold"] == ["orders", "revenue"]
    assert spec["encoding"]["xOffset"]["field"] == "series"


# ---------------------------------------------------------------------------
# visualize_node — fail-soft contract
# ---------------------------------------------------------------------------


def test_visualize_node_returns_kind_and_spec() -> None:
    rows = [{"country": "Germany", "n": 11}]
    out = visualize_node({"sql_result": rows, "question": "customers by country"})
    # 1 row, 1 quant → kpi (so chart_spec is None)
    assert out["chart_kind"] == "kpi"
    assert out["chart_spec"] is None


def test_visualize_node_emits_spec_for_bar() -> None:
    rows = [{"country": "Germany", "n": 11}, {"country": "France", "n": 7}]
    out = visualize_node({"sql_result": rows, "question": "customers by country"})
    assert out["chart_kind"] == "bar"
    assert out["chart_spec"] is not None
    assert out["chart_spec"]["mark"] == "bar"


def test_visualize_node_empty_rows_safe() -> None:
    out = visualize_node({"sql_result": [], "question": "anything"})
    assert out["chart_kind"] == "table"
    assert out["chart_spec"] is None


def test_visualize_node_fails_soft_on_bad_input() -> None:
    """A malformed row (non-dict) inside the list should not crash the
    node; we fall back to a table verdict."""
    out = visualize_node({"sql_result": [42, "rows", None], "question": "?"})
    assert out["chart_kind"] == "table"
    assert out["chart_spec"] is None


def test_visualize_node_long_title_truncated() -> None:
    rows = [{"x": "a", "n": 1}, {"x": "b", "n": 2}]
    very_long = "Q" * 500
    out = visualize_node({"sql_result": rows, "question": very_long})
    spec = out["chart_spec"]
    assert spec is not None
    assert len(spec["title"]) <= 80
    assert spec["title"].endswith("...")
