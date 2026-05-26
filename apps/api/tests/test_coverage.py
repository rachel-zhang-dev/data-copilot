"""Unit tests for the Phase 1.1 coverage gate.

Mocks the LLM via ``stub_llm_factory`` and the profile loader via
``monkeypatch``, so these tests run without Postgres.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from copilot.agent import coverage as coverage_mod
from copilot.agent import feature_flags
from copilot.agent.coverage import (
    coverage_check_node,
    explain_uncovered_node,
    parse_coverage_response,
    parse_uncovered_response,
    route_after_coverage,
)

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_coverage_response_happy_ok() -> None:
    raw = json.dumps(
        {
            "verdict": "ok",
            "reason": "schema covers customers + orders",
            "missing_concepts": [],
            "suggested_questions": [],
        }
    )
    out = parse_coverage_response(raw)
    assert out is not None
    assert out["verdict"] == "ok"
    assert out["missing_concepts"] == []


def test_parse_coverage_response_happy_refuse() -> None:
    raw = json.dumps(
        {
            "verdict": "refuse",
            "reason": "no conversion funnel data",
            "missing_concepts": ["conversion rate", "funnel"],
            "suggested_questions": ["Top customers by revenue?"],
        }
    )
    out = parse_coverage_response(raw)
    assert out is not None
    assert out["verdict"] == "refuse"
    assert out["missing_concepts"] == ["conversion rate", "funnel"]
    assert out["suggested_questions"] == ["Top customers by revenue?"]


def test_parse_coverage_response_strips_markdown_fences() -> None:
    raw = '```json\n{"verdict":"ok","reason":"","missing_concepts":[],"suggested_questions":[]}\n```'
    out = parse_coverage_response(raw)
    assert out is not None
    assert out["verdict"] == "ok"


def test_parse_coverage_response_unknown_verdict_returns_none() -> None:
    raw = json.dumps({"verdict": "maybe"})
    assert parse_coverage_response(raw) is None


def test_parse_coverage_response_invalid_json_returns_none() -> None:
    assert parse_coverage_response("not json") is None
    assert parse_coverage_response("") is None
    assert parse_coverage_response("   ") is None


def test_parse_coverage_response_filters_non_string_list_items() -> None:
    raw = json.dumps(
        {
            "verdict": "refuse",
            "reason": "x",
            "missing_concepts": ["valid", 42, None, "", "another"],
            "suggested_questions": [{"nested": "obj"}, "ok"],
        }
    )
    out = parse_coverage_response(raw)
    assert out is not None
    assert out["missing_concepts"] == ["valid", "another"]
    assert out["suggested_questions"] == ["ok"]


def test_parse_coverage_response_caps_list_lengths() -> None:
    raw = json.dumps(
        {
            "verdict": "refuse",
            "reason": "",
            "missing_concepts": [f"thing_{i}" for i in range(20)],
            "suggested_questions": [f"q_{i}" for i in range(20)],
        }
    )
    out = parse_coverage_response(raw)
    assert out is not None
    assert len(out["missing_concepts"]) == 5
    assert len(out["suggested_questions"]) == 5


def test_parse_uncovered_response_requires_headline() -> None:
    assert parse_uncovered_response(json.dumps({"headline": ""})) is None
    assert (
        parse_uncovered_response(json.dumps({"headline": "ok", "bullets": []}))
        is not None
    )


# ---------------------------------------------------------------------------
# coverage_check_node — fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``_load_profile_text`` with a non-empty string so the
    gate doesn't fail-open on profile-empty."""
    monkeypatch.setattr(
        coverage_mod, "_load_profile_text", lambda tables: "Table: customers (91 rows)"
    )


# ---------------------------------------------------------------------------
# coverage_check_node — fail-open paths (no LLM call expected)
# ---------------------------------------------------------------------------


def test_coverage_check_disabled_returns_ok_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the feature flag is off, no LLM call happens — guard the
    eval baseline's clean comparison against the pre-Phase-1.1 path."""
    monkeypatch.setattr(feature_flags, "COVERAGE_CHECK_ENABLED", False)

    # If get_llm is invoked, this assertion fails the test.
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("LLM should not be called when gate is disabled")

    monkeypatch.setattr(coverage_mod, "get_llm", _boom)

    out = coverage_check_node(
        {"question": "How many customers?", "relevant_tables": ["customers"]}
    )
    assert out["coverage"]["verdict"] == "ok"
    assert "cost" not in out


def test_coverage_check_no_profile_returns_ok_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the profile table is empty, fail-open without an LLM call."""
    monkeypatch.setattr(coverage_mod, "_load_profile_text", lambda tables: "")

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("LLM should not be called when profile is empty")

    monkeypatch.setattr(coverage_mod, "get_llm", _boom)

    out = coverage_check_node(
        {"question": "How many customers?", "relevant_tables": ["customers"]}
    )
    assert out["coverage"]["verdict"] == "ok"
    assert "profile" in out["coverage"]["reason"]


def test_coverage_check_unparsable_llm_reply_fails_open(
    fake_profile, stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feature_flags, "COVERAGE_CHECK_ENABLED", True)
    monkeypatch.setattr("copilot.agent.coverage.get_llm", lambda *a, **k: stub_llm_factory("not json"))

    out = coverage_check_node(
        {"question": "x", "relevant_tables": ["customers"]}
    )
    assert out["coverage"]["verdict"] == "ok"
    # Cost is still charged for the failed parse — the LLM call happened.
    assert "cost" in out


def test_coverage_check_llm_exception_fails_open(
    fake_profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feature_flags, "COVERAGE_CHECK_ENABLED", True)

    class _ExplodingLLM:
        def invoke(self, _messages: Any) -> Any:
            raise RuntimeError("siliconflow is down")

    monkeypatch.setattr(coverage_mod, "get_llm", lambda *_a, **_k: _ExplodingLLM())

    out = coverage_check_node(
        {"question": "x", "relevant_tables": ["customers"]}
    )
    assert out["coverage"]["verdict"] == "ok"
    # No cost recorded — the call never produced a response.
    assert "cost" not in out


# ---------------------------------------------------------------------------
# coverage_check_node — real verdicts
# ---------------------------------------------------------------------------


def test_coverage_check_ok_verdict_propagates(
    fake_profile, stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feature_flags, "COVERAGE_CHECK_ENABLED", True)
    stub = stub_llm_factory(
        json.dumps(
            {
                "verdict": "ok",
                "reason": "looks good",
                "missing_concepts": [],
                "suggested_questions": [],
            }
        )
    )
    monkeypatch.setattr("copilot.agent.coverage.get_llm", lambda *a, **k: stub)

    out = coverage_check_node(
        {"question": "How many customers?", "relevant_tables": ["customers"]}
    )
    assert out["coverage"]["verdict"] == "ok"
    assert out["coverage"]["reason"] == "looks good"


def test_coverage_check_refuse_verdict_propagates(
    fake_profile, stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feature_flags, "COVERAGE_CHECK_ENABLED", True)
    stub = stub_llm_factory(
        json.dumps(
            {
                "verdict": "refuse",
                "reason": "no funnel data",
                "missing_concepts": ["conversion rate"],
                "suggested_questions": ["Top customers by orders?"],
            }
        )
    )
    monkeypatch.setattr("copilot.agent.coverage.get_llm", lambda *a, **k: stub)

    out = coverage_check_node(
        {"question": "Why is conversion dropping?", "relevant_tables": ["customers"]}
    )
    assert out["coverage"]["verdict"] == "refuse"
    assert "conversion rate" in out["coverage"]["missing_concepts"]
    assert out["coverage"]["suggested_questions"] == ["Top customers by orders?"]


# ---------------------------------------------------------------------------
# route_after_coverage
# ---------------------------------------------------------------------------


def test_route_after_coverage_ok_to_generate_sql() -> None:
    assert (
        route_after_coverage({"coverage": {"verdict": "ok"}}) == "generate_sql"
    )


def test_route_after_coverage_refuse_to_explain() -> None:
    assert (
        route_after_coverage({"coverage": {"verdict": "refuse"}})
        == "explain_uncovered"
    )


def test_route_after_coverage_missing_state_defaults_to_generate_sql() -> None:
    # No ``coverage`` key (e.g. fail-open silently dropped it) must
    # NOT crash the router.
    assert route_after_coverage({}) == "generate_sql"


def test_route_after_coverage_unknown_verdict_defaults_to_generate_sql() -> None:
    assert (
        route_after_coverage({"coverage": {"verdict": "maybe"}}) == "generate_sql"
    )


# ---------------------------------------------------------------------------
# explain_uncovered_node
# ---------------------------------------------------------------------------


def test_explain_uncovered_uses_llm_response(
    fake_profile, stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = stub_llm_factory(
        json.dumps(
            {
                "headline": "This database doesn't have conversion data.",
                "bullets": ["No funnel events", "No campaign attribution"],
                "suggested_questions": [
                    "Top 5 customers by total order value?",
                    "Monthly revenue trend in 1997?",
                ],
            }
        )
    )
    monkeypatch.setattr("copilot.agent.coverage.get_llm", lambda *a, **k: stub)

    state = {
        "question": "Why is conversion dropping?",
        "relevant_tables": ["customers", "orders"],
        "coverage": {
            "verdict": "refuse",
            "reason": "no funnel",
            "missing_concepts": ["conversion rate"],
            "suggested_questions": ["Top customers by orders?"],
        },
    }
    out = explain_uncovered_node(state)
    assert "conversion" in out["answer"]
    assert out["coverage"]["verdict"] == "refuse"
    assert "No funnel events" in out["coverage"]["bullets"]
    # Suggestions: LLM-first, then gate-side, dedup, cap 3.
    chips = out["coverage"]["suggested_questions"]
    assert len(chips) <= 3
    assert "Top 5 customers by total order value?" in chips
    assert "Top customers by orders?" in chips


def test_explain_uncovered_falls_back_to_template_on_llm_failure(
    fake_profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the LLM raises, we still set ``answer`` from the gate's
    missing_concepts. ``append_to_dialogue`` depends on ``answer``
    being non-empty."""

    class _ExplodingLLM:
        def invoke(self, _messages: Any) -> Any:
            raise RuntimeError("rate-limited")

    monkeypatch.setattr(coverage_mod, "get_llm", lambda *_a, **_k: _ExplodingLLM())

    state = {
        "question": "Why is conversion dropping?",
        "relevant_tables": ["customers"],
        "coverage": {
            "verdict": "refuse",
            "reason": "no funnel",
            "missing_concepts": ["conversion rate", "funnel"],
            "suggested_questions": ["Top customers by orders?"],
        },
    }
    out = explain_uncovered_node(state)
    # Headline includes the missing concept.
    assert "conversion rate" in out["answer"]
    # No LLM cost since the call failed before producing a response.
    assert "cost" not in out
    # Suggestions are preserved from the gate-side list.
    assert out["coverage"]["suggested_questions"] == ["Top customers by orders?"]


def test_explain_uncovered_unparsable_llm_reply_uses_template(
    fake_profile, stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = stub_llm_factory("not json")
    monkeypatch.setattr("copilot.agent.coverage.get_llm", lambda *a, **k: stub)

    state = {
        "question": "Why is conversion dropping?",
        "relevant_tables": ["customers"],
        "coverage": {
            "verdict": "refuse",
            "reason": "no funnel",
            "missing_concepts": [],
            "suggested_questions": [],
        },
    }
    out = explain_uncovered_node(state)
    assert out["answer"]  # non-empty
    assert "cost" in out  # LLM call happened, parse failed
