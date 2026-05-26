"""Integration-ish tests for ``copilot.agent.patterns.node``.

We mock the LLM via ``stub_llm_factory``; the detectors run for real
because they're pure numpy. The goal is to pin the integration
contract:

* When detectors return findings, the node calls the LLM exactly once.
* The LLM response gets parsed and bullets get PREPENDED to insight.
* On parse failure, the deterministic fallback kicks in (bullets are
  still present).
* On LLM exception, same — fallback bullets, no cost charged.
* When the feature flag is off, no detector OR LLM call happens.
* When the result is empty / KPI / non-numeric, the node returns
  ``{}`` and never calls the LLM.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from copilot.agent import feature_flags
from copilot.agent.patterns import node as node_mod
from copilot.agent.patterns.node import (
    _fallback_bullet,
    _merge_into_insight,
    detect_patterns_node,
    parse_render_response,
)

# ---------------------------------------------------------------------------
# parse_render_response
# ---------------------------------------------------------------------------


def test_parse_happy_returns_bullets() -> None:
    raw = json.dumps({"bullets": ["Bullet 1.", "Bullet 2."]})
    out = parse_render_response(raw, expected_count=2)
    assert out == ["Bullet 1.", "Bullet 2."]


def test_parse_strips_fences() -> None:
    raw = "```json\n" + json.dumps({"bullets": ["x"]}) + "\n```"
    out = parse_render_response(raw, expected_count=1)
    assert out == ["x"]


def test_parse_truncates_excess_bullets() -> None:
    raw = json.dumps({"bullets": ["a", "b", "c", "d"]})
    out = parse_render_response(raw, expected_count=2)
    assert out == ["a", "b"]


def test_parse_pads_missing_bullets_by_repeating_last() -> None:
    raw = json.dumps({"bullets": ["only one"]})
    out = parse_render_response(raw, expected_count=3)
    assert out == ["only one", "only one", "only one"]


def test_parse_invalid_json_returns_none() -> None:
    assert parse_render_response("not json", expected_count=2) is None


def test_parse_drops_empty_strings_and_non_strings() -> None:
    raw = json.dumps({"bullets": ["ok", "", None, 42, "also ok"]})
    out = parse_render_response(raw, expected_count=2)
    assert out == ["ok", "also ok"]


def test_parse_empty_bullet_list_returns_none() -> None:
    raw = json.dumps({"bullets": []})
    assert parse_render_response(raw, expected_count=2) is None


# ---------------------------------------------------------------------------
# _fallback_bullet — deterministic templates
# ---------------------------------------------------------------------------


def test_fallback_bullet_outlier_high() -> None:
    from copilot.agent.patterns.detectors import Finding

    f = Finding(
        kind="outlier",
        column="customers",
        severity="high",
        description_key="high_value_outlier",
        payload={"value": 13, "z_score": 3.1, "label": "USA"},
    )
    b = _fallback_bullet(f)
    assert "USA" in b
    assert "13" in b
    assert "above" in b


def test_fallback_bullet_outlier_low() -> None:
    from copilot.agent.patterns.detectors import Finding

    f = Finding(
        kind="outlier",
        column="n",
        severity="notable",
        description_key="low_value_outlier",
        payload={"value": 2, "z_score": -2.4, "label": "x"},
    )
    b = _fallback_bullet(f)
    assert "below" in b


def test_fallback_bullet_trend_with_pct() -> None:
    from copilot.agent.patterns.detectors import Finding

    f = Finding(
        kind="trend",
        column="revenue",
        severity="high",
        description_key="trend_up",
        payload={
            "first_value": 100,
            "last_value": 150,
            "delta_pct": 50.0,
            "r_squared": 0.95,
        },
    )
    b = _fallback_bullet(f)
    assert "revenue" in b
    assert "100" in b and "150" in b
    assert "50" in b


# ---------------------------------------------------------------------------
# _merge_into_insight
# ---------------------------------------------------------------------------


def test_merge_prepends_pattern_bullets() -> None:
    existing = {
        "headline": "h",
        "bullets": ["existing 1", "existing 2"],
        "metric_highlights": [],
    }
    out = _merge_into_insight(existing, ["pattern 1", "pattern 2"])
    assert out is not None
    assert out["bullets"][:2] == ["pattern 1", "pattern 2"]
    assert "existing 1" in out["bullets"]


def test_merge_creates_insight_when_existing_is_none() -> None:
    out = _merge_into_insight(None, ["pattern 1"])
    assert out is not None
    assert out["headline"] == ""
    assert out["bullets"] == ["pattern 1"]


def test_merge_returns_none_when_no_pattern_bullets_and_no_existing() -> None:
    assert _merge_into_insight(None, []) is None


def test_merge_caps_total_bullets() -> None:
    existing = {
        "headline": "h",
        "bullets": ["e1", "e2", "e3", "e4"],
        "metric_highlights": [],
    }
    out = _merge_into_insight(existing, ["p1", "p2", "p3"])
    assert out is not None
    assert len(out["bullets"]) <= 6


# ---------------------------------------------------------------------------
# detect_patterns_node — flag-off / empty / KPI early returns
# ---------------------------------------------------------------------------


def test_node_feature_flag_off_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feature_flags, "PATTERNS_DETECTION_ENABLED", False)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("LLM should not be called when flag is off")

    monkeypatch.setattr(node_mod, "get_llm", _boom)

    state = {
        "question": "x",
        "sql_result": [{"n": i} for i in range(20)],
    }
    out = detect_patterns_node(state)
    assert out == {}


def test_node_empty_rows_skips_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feature_flags, "PATTERNS_DETECTION_ENABLED", True)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("LLM should not be called on empty rows")

    monkeypatch.setattr(node_mod, "get_llm", _boom)

    assert detect_patterns_node({"question": "x", "sql_result": []}) == {}


def test_node_single_row_kpi_skips_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KPI shape (count(*) == 1 row) has no patterns — must not call LLM."""
    monkeypatch.setattr(feature_flags, "PATTERNS_DETECTION_ENABLED", True)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("LLM should not be called on single-row KPI")

    monkeypatch.setattr(node_mod, "get_llm", _boom)

    state = {"question": "How many?", "sql_result": [{"count": 91}]}
    assert detect_patterns_node(state) == {}


def test_node_no_numeric_column_skips_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feature_flags, "PATTERNS_DETECTION_ENABLED", True)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("LLM should not be called on non-numeric data")

    monkeypatch.setattr(node_mod, "get_llm", _boom)

    state = {
        "question": "List names",
        "sql_result": [{"name": f"n{i}"} for i in range(10)],
    }
    assert detect_patterns_node(state) == {}


# ---------------------------------------------------------------------------
# detect_patterns_node — happy path with LLM
# ---------------------------------------------------------------------------


def _outlier_rows() -> list[dict[str, Any]]:
    return [{"country": country, "customers": n} for country, n in [
        ("UK", 2), ("France", 3), ("Germany", 2), ("Spain", 3),
        ("Italy", 2), ("Brazil", 4), ("USA", 50),
    ]]


def test_node_happy_path_calls_llm_and_merges_bullets(
    stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feature_flags, "PATTERNS_DETECTION_ENABLED", True)
    stub = stub_llm_factory(
        json.dumps(
            {"bullets": ["USA (50 customers) stands out — 3σ above the mean."]}
        )
    )
    monkeypatch.setattr(node_mod, "get_llm", lambda *a, **k: stub)

    state = {
        "question": "Count customers grouped by country",
        "sql": "SELECT country, count(*)",
        "sql_result": _outlier_rows(),
        "insight": {
            "headline": "USA leads",
            "bullets": ["7 countries total"],
            "metric_highlights": [],
        },
    }
    out = detect_patterns_node(state)

    assert "patterns" in out and len(out["patterns"]) >= 1
    assert out["patterns"][0]["kind"] == "outlier"
    # Pattern bullet is prepended to existing bullets.
    merged_bullets = out["insight"]["bullets"]
    assert "USA" in merged_bullets[0]
    assert "7 countries total" in merged_bullets
    # LLM call was made → cost recorded.
    assert "cost" in out


def test_node_falls_back_to_template_on_unparsable_llm(
    stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feature_flags, "PATTERNS_DETECTION_ENABLED", True)
    stub = stub_llm_factory("not json at all")
    monkeypatch.setattr(node_mod, "get_llm", lambda *a, **k: stub)

    state = {
        "question": "?",
        "sql": "?",
        "sql_result": _outlier_rows(),
        "insight": {
            "headline": "h",
            "bullets": [],
            "metric_highlights": [],
        },
    }
    out = detect_patterns_node(state)
    # Patterns still emitted; bullets fall back to the deterministic
    # template (always English, always grounded in payload).
    assert out["patterns"]
    bullets = out["insight"]["bullets"]
    assert bullets
    assert any("USA" in b for b in bullets)
    # LLM call did happen → cost recorded.
    assert "cost" in out


def test_node_falls_back_when_llm_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feature_flags, "PATTERNS_DETECTION_ENABLED", True)

    class _Boom:
        def invoke(self, _m: Any) -> Any:
            raise RuntimeError("rate-limited")

    monkeypatch.setattr(node_mod, "get_llm", lambda *_a, **_k: _Boom())

    state = {
        "question": "?",
        "sql": "?",
        "sql_result": _outlier_rows(),
        "insight": None,
    }
    out = detect_patterns_node(state)
    # Patterns emitted, fallback bullets present, NO cost (LLM never
    # returned a response).
    assert out["patterns"]
    assert out["insight"]["bullets"]
    assert "cost" not in out


def test_node_pads_bullets_when_llm_returns_fewer(
    stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM returns 1 bullet but we have 2 findings — padding kicks in
    so each finding has a bullet."""
    monkeypatch.setattr(feature_flags, "PATTERNS_DETECTION_ENABLED", True)
    # Data with BOTH a clear trend and a clear outlier.
    rows = [{"n": v} for v in [1, 2, 3, 4, 5, 6, 7, 100]]
    stub = stub_llm_factory(json.dumps({"bullets": ["Trend rising."]}))
    monkeypatch.setattr(node_mod, "get_llm", lambda *a, **k: stub)

    state = {"question": "?", "sql": "?", "sql_result": rows, "insight": None}
    out = detect_patterns_node(state)
    n_findings = len(out["patterns"])
    n_bullets = len(out["insight"]["bullets"])
    # padded so each finding has a bullet
    assert n_bullets >= n_findings
