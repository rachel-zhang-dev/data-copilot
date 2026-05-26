"""Unit tests for the Phase 1.1 schema explorer node."""

from __future__ import annotations

import json
from typing import Any

import pytest
from copilot.agent import explore as explore_mod
from copilot.agent.explore import (
    _fallback_tour,
    explore_schema_node,
    parse_explore_response,
)

# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_explore_response_happy() -> None:
    raw = json.dumps(
        {
            "headline": "Northwind: orders, customers, products.",
            "topics": [
                {
                    "name": "Customers & Sales",
                    "tables": ["customers", "orders", "order_details"],
                    "summary": "Who buys what.",
                },
                {
                    "name": "Products",
                    "tables": ["products", "categories", "suppliers"],
                    "summary": "What we sell.",
                },
            ],
            "sample_questions": [
                "How many orders shipped last month?",
                "Top 5 products by revenue?",
            ],
        }
    )
    out = parse_explore_response(raw)
    assert out is not None
    assert out["headline"].startswith("Northwind")
    assert len(out["topics"]) == 2
    assert out["topics"][0]["name"] == "Customers & Sales"
    assert "customers" in out["topics"][0]["tables"]
    assert len(out["sample_questions"]) == 2


def test_parse_explore_response_requires_headline_and_topics() -> None:
    # No headline.
    assert parse_explore_response(json.dumps({"topics": []})) is None
    # Topics not a list.
    assert (
        parse_explore_response(json.dumps({"headline": "x", "topics": "nope"}))
        is None
    )


def test_parse_explore_response_drops_invalid_topic_entries() -> None:
    raw = json.dumps(
        {
            "headline": "ok",
            "topics": [
                {"name": "", "tables": ["t"]},  # empty name → drop
                {"name": "valid", "tables": []},  # empty tables → drop
                {"name": "ok", "tables": ["a", "b"]},
                "not an object",  # wrong type → drop
            ],
            "sample_questions": [],
        }
    )
    out = parse_explore_response(raw)
    assert out is not None
    assert len(out["topics"]) == 1
    assert out["topics"][0]["name"] == "ok"


def test_parse_explore_response_strips_markdown_fence() -> None:
    raw = (
        "```json\n"
        + json.dumps({"headline": "h", "topics": [{"name": "t", "tables": ["x"]}]})
        + "\n```"
    )
    out = parse_explore_response(raw)
    assert out is not None
    assert out["headline"] == "h"


def test_parse_explore_response_invalid_json_returns_none() -> None:
    assert parse_explore_response("nope") is None
    assert parse_explore_response("") is None


# ---------------------------------------------------------------------------
# explore_schema_node — happy path
# ---------------------------------------------------------------------------


def _fake_profile_for(monkeypatch: pytest.MonkeyPatch, tables: list[str]) -> None:
    monkeypatch.setattr(explore_mod, "list_tables", lambda: tables)
    monkeypatch.setattr(
        explore_mod,
        "load_profile",
        lambda tbls: {t: [{"column_name": "*", "row_count": 10}] for t in tbls},
    )
    # Pretend the renderer always produces non-empty profile text so
    # the node sticks to the LLM branch.
    monkeypatch.setattr(
        explore_mod,
        "format_profile_for_llm",
        lambda _by_table: "Table: customers (91 rows)\n  - id (int)",
    )


def test_explore_uses_llm_response(
    stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_profile_for(monkeypatch, ["customers", "orders"])
    stub = stub_llm_factory(
        json.dumps(
            {
                "headline": "Sales DB.",
                "topics": [
                    {
                        "name": "Sales",
                        "tables": ["customers", "orders"],
                        "summary": "Who buys what.",
                    }
                ],
                "sample_questions": ["How many orders?"],
            }
        )
    )
    monkeypatch.setattr("copilot.agent.explore.get_llm", lambda *a, **k: stub)

    out = explore_schema_node({"question": "What's in this DB?"})
    assert out["answer"] == "Sales DB."
    assert out["coverage"]["verdict"] == "explore"
    assert out["coverage"]["topics"][0]["name"] == "Sales"
    assert out["coverage"]["suggested_questions"] == ["How many orders?"]
    assert "cost" in out


# ---------------------------------------------------------------------------
# explore_schema_node — fallback paths
# ---------------------------------------------------------------------------


def test_explore_falls_back_when_llm_returns_garbage(
    stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_profile_for(monkeypatch, ["customers", "orders"])
    stub = stub_llm_factory("not json")
    monkeypatch.setattr("copilot.agent.explore.get_llm", lambda *a, **k: stub)

    out = explore_schema_node({"question": "what is here?"})
    # Fallback still names the tables it found.
    assert "2 tables" in out["answer"] or "tables" in out["answer"]
    assert out["coverage"]["verdict"] == "explore"
    # LLM was called → cost charged.
    assert "cost" in out


def test_explore_falls_back_when_llm_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_profile_for(monkeypatch, ["customers"])

    class _Boom:
        def invoke(self, _m: Any) -> Any:
            raise RuntimeError("api down")

    monkeypatch.setattr(explore_mod, "get_llm", lambda *_a, **_k: _Boom())

    out = explore_schema_node({"question": "?"})
    assert out["answer"]
    assert out["coverage"]["verdict"] == "explore"
    # LLM call failed before producing → no cost.
    assert "cost" not in out


def test_explore_falls_back_when_profile_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(explore_mod, "list_tables", lambda: ["customers"])
    monkeypatch.setattr(explore_mod, "load_profile", lambda _t: {})
    monkeypatch.setattr(explore_mod, "format_profile_for_llm", lambda _b: "")

    # If the LLM is invoked we want to know — empty profile should
    # short-circuit BEFORE the LLM call.
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("LLM should not be called when profile is empty")

    monkeypatch.setattr(explore_mod, "get_llm", _boom)

    out = explore_schema_node({"question": "?"})
    assert out["answer"]
    assert out["coverage"]["verdict"] == "explore"


def test_fallback_tour_when_no_tables() -> None:
    # No monkeypatch — list_tables is called for real. Just assert the
    # function returns the expected shape; on a real test DB this may
    # or may not have tables, so we accept both branches.
    out = _fallback_tour("?")
    assert "headline" in out
    assert "topics" in out
    assert "sample_questions" in out
