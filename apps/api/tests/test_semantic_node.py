"""Unit tests for the semantic-router + resolver LangGraph nodes (Phase 3.1).

Following the same posture as ``test_coverage.py`` and ``test_critic.py``:

* Feature flag off → no LLM call, immediate fallback envelope.
* LLM exception   → fail-open to fallback.
* Unparsable JSON → fail-open with cost recorded (the call happened).
* Router declined → fallback path.
* Router answerable + good spec → semantic_layer path with compiled SQL
  ready for the next node.
* Resolver compile error → flip to fallback so the graph re-routes to
  generate_sql (defense in depth).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from copilot.agent import feature_flags
from copilot.agent import semantic_node as semantic_mod
from copilot.agent.semantic_node import (
    metric_resolver_node,
    metric_router_node,
    route_after_metric_resolver,
    route_after_metric_router,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubLLM:
    def __init__(self, response_text: str) -> None:
        self._text = response_text
        self.calls: list[Any] = []

    def invoke(self, messages: Any) -> _StubMessage:
        self.calls.append(messages)
        return _StubMessage(self._text)


def _install_stub_llm(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | str) -> _StubLLM:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    stub = _StubLLM(text)
    monkeypatch.setattr(semantic_mod, "get_llm", lambda *_a, **_k: stub)
    return stub


# ---------------------------------------------------------------------------
# Fail-open paths
# ---------------------------------------------------------------------------


def test_router_disabled_returns_fallback_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feature_flags, "SEMANTIC_LAYER_ENABLED", False)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("LLM must not be called when flag is off")

    monkeypatch.setattr(semantic_mod, "get_llm", _boom)

    out = metric_router_node({"question": "How many customers?"})
    assert out["semantic"]["path"] == "fallback"
    assert "cost" not in out


def test_router_llm_exception_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feature_flags, "SEMANTIC_LAYER_ENABLED", True)

    class _ExplodingLLM:
        def invoke(self, _messages: Any) -> Any:
            raise RuntimeError("deepseek is down")

    monkeypatch.setattr(semantic_mod, "get_llm", lambda *_a, **_k: _ExplodingLLM())

    out = metric_router_node({"question": "How many customers?"})
    assert out["semantic"]["path"] == "fallback"
    assert "cost" not in out


def test_router_unparsable_json_fails_open_with_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feature_flags, "SEMANTIC_LAYER_ENABLED", True)
    _install_stub_llm(monkeypatch, "not json at all")

    out = metric_router_node({"question": "How many customers?"})
    assert out["semantic"]["path"] == "fallback"
    # Cost IS recorded — the LLM call completed, only parsing failed.
    assert "cost" in out


def test_router_non_object_json_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feature_flags, "SEMANTIC_LAYER_ENABLED", True)
    _install_stub_llm(monkeypatch, "[1, 2, 3]")  # JSON array, not an object

    out = metric_router_node({"question": "How many customers?"})
    assert out["semantic"]["path"] == "fallback"


# ---------------------------------------------------------------------------
# Real verdicts
# ---------------------------------------------------------------------------


def test_router_declines_when_answerable_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feature_flags, "SEMANTIC_LAYER_ENABLED", True)
    _install_stub_llm(
        monkeypatch,
        {
            "answerable": False,
            "reason": "no conversion-rate metric defined",
        },
    )

    out = metric_router_node({"question": "What is our conversion rate?"})
    assert out["semantic"]["path"] == "fallback"
    assert "conversion" in out["semantic"]["reason"]


def test_router_happy_path_emits_validated_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feature_flags, "SEMANTIC_LAYER_ENABLED", True)
    _install_stub_llm(
        monkeypatch,
        {
            "answerable": True,
            "reason": "asked for customer count by country",
            "metric": "customer_count",
            "dimensions": ["country"],
            "time_range": None,
            "filters": [],
        },
    )

    out = metric_router_node({"question": "How many customers per country?"})
    assert out["semantic"]["path"] == "semantic_layer"
    spec = out["semantic"]["spec"]
    assert spec["metric"] == "customer_count"
    assert spec["dimensions"] == ["country"]


def test_router_spec_validation_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM said answerable but the spec is malformed → fall back so
    the LLM text-to-SQL pipeline can still try."""
    monkeypatch.setattr(feature_flags, "SEMANTIC_LAYER_ENABLED", True)
    _install_stub_llm(
        monkeypatch,
        {
            "answerable": True,
            "reason": "",
            "metric": None,  # required, but None
            "dimensions": ["country"],
        },
    )

    out = metric_router_node({"question": "?"})
    assert out["semantic"]["path"] == "fallback"
    assert "spec invalid" in out["semantic"]["reason"]


# ---------------------------------------------------------------------------
# Resolver node
# ---------------------------------------------------------------------------


def test_resolver_compiles_sql_on_happy_path() -> None:
    state = {
        "question": "Customers by country",
        "semantic": {
            "path": "semantic_layer",
            "answerable": True,
            "reason": "x",
            "spec": {
                "metric": "customer_count",
                "dimensions": ["country"],
                "time_range": None,
                "filters": [],
                "limit": 100,
            },
        },
    }
    out = metric_resolver_node(state)
    assert "GROUP BY c.country" in out["sql"]
    assert out["semantic"]["path"] == "semantic_layer"
    assert "sql" in out["semantic"]


def test_resolver_missing_spec_falls_back() -> None:
    out = metric_resolver_node({"semantic": {"path": "semantic_layer"}})
    # No sql produced; path flipped to fallback.
    assert out["semantic"]["path"] == "fallback"
    assert "sql" not in out


def test_resolver_unknown_metric_falls_back() -> None:
    state = {
        "semantic": {
            "path": "semantic_layer",
            "spec": {
                "metric": "imaginary_metric",
                "dimensions": [],
                "time_range": None,
                "filters": [],
                "limit": 100,
            },
        },
    }
    out = metric_resolver_node(state)
    assert out["semantic"]["path"] == "fallback"
    assert "compile_error" in out["semantic"]


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------


def test_route_after_router_semantic_path_goes_to_resolver() -> None:
    assert (
        route_after_metric_router({"semantic": {"path": "semantic_layer"}})
        == "metric_resolver"
    )


def test_route_after_router_fallback_goes_to_generate_sql() -> None:
    assert (
        route_after_metric_router({"semantic": {"path": "fallback"}})
        == "generate_sql"
    )


def test_route_after_router_missing_semantic_defaults_to_generate_sql() -> None:
    """Defensive: state without ``semantic`` (the router didn't run)
    must fall through to the LLM path without crashing."""
    assert route_after_metric_router({}) == "generate_sql"


def test_route_after_resolver_with_sql_goes_to_validate() -> None:
    state = {
        "sql": "SELECT 1",
        "semantic": {"path": "semantic_layer"},
    }
    assert route_after_metric_resolver(state) == "validate_sql"


def test_route_after_resolver_compile_failed_routes_to_generate_sql() -> None:
    state = {
        "semantic": {"path": "fallback", "compile_error": "x"},
    }
    assert route_after_metric_resolver(state) == "generate_sql"
