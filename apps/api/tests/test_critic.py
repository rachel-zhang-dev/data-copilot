"""Unit tests for the Phase 2.3 SQL critic (ADR 0021).

We exercise the four code paths a node like this can take:

1. Feature flag disabled → ok, no LLM call.
2. LLM raises             → ok (fail-open), no cost recorded.
3. LLM returns garbage    → ok (fail-open), cost recorded.
4. LLM returns a verdict  → propagates straight through.

Plus the router and the bookkeeping node that converts a "wrong"
verdict into a proper Attempt + error string on the way back to
``generate_sql``.

All tests run without Postgres or a real LLM — we patch
``copilot.agent.critic.get_llm`` directly (the ``stub_llm_factory``
fixture only reaches ``nodes``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from copilot.agent import critic as critic_mod
from copilot.agent import feature_flags
from copilot.agent.critic import (
    CriticVerdict,
    critique_sql_node,
    parse_critic_verdict,
    record_critic_rejection_node,
    route_after_critic,
)
from copilot.agent.state import Attempt

# ---------------------------------------------------------------------------
# parse_critic_verdict
# ---------------------------------------------------------------------------


def test_parse_critic_verdict_ok() -> None:
    raw = json.dumps({"verdict": "ok", "reason": "all good", "concerns": []})
    out = parse_critic_verdict(raw)
    assert out is not None
    assert out.verdict == "ok"
    assert out.concerns == []


def test_parse_critic_verdict_suspicious_with_concerns() -> None:
    raw = json.dumps(
        {
            "verdict": "suspicious",
            "reason": "JOIN may fan out",
            "concerns": ["customers JOIN orders without DISTINCT"],
        }
    )
    out = parse_critic_verdict(raw)
    assert out is not None
    assert out.verdict == "suspicious"
    assert "fan out" in out.reason


def test_parse_critic_verdict_wrong() -> None:
    raw = json.dumps(
        {
            "verdict": "wrong",
            "reason": "user asked 1997 but SQL filters 1998",
            "concerns": ["WHERE extract(year from o.order_date) = 1998"],
        }
    )
    out = parse_critic_verdict(raw)
    assert out is not None
    assert out.verdict == "wrong"


def test_parse_critic_verdict_strips_fences() -> None:
    raw = '```json\n{"verdict":"ok","reason":"","concerns":[]}\n```'
    out = parse_critic_verdict(raw)
    assert out is not None and out.verdict == "ok"


def test_parse_critic_verdict_rejects_unknown_verdict() -> None:
    raw = json.dumps({"verdict": "definitely-broken", "reason": "", "concerns": []})
    assert parse_critic_verdict(raw) is None


def test_parse_critic_verdict_rejects_invalid_json() -> None:
    assert parse_critic_verdict("not json") is None
    assert parse_critic_verdict("") is None
    assert parse_critic_verdict("   ") is None


def test_parse_critic_verdict_caps_concern_length() -> None:
    long_concern = "x" * 500
    raw = json.dumps(
        {"verdict": "suspicious", "reason": "y", "concerns": [long_concern]}
    )
    out = parse_critic_verdict(raw)
    assert out is not None
    assert len(out.concerns[0]) <= 200


def test_parse_critic_verdict_caps_concern_count() -> None:
    raw = json.dumps(
        {
            "verdict": "suspicious",
            "reason": "y",
            "concerns": [f"c{i}" for i in range(10)],
        }
    )
    # Pydantic raises on list-length > max; parser returns None.
    assert parse_critic_verdict(raw) is None


# ---------------------------------------------------------------------------
# critique_sql_node — fail-open paths
# ---------------------------------------------------------------------------


def test_critique_sql_disabled_returns_ok_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the feature flag is off, no LLM call happens — mirrors the
    eval baseline's clean comparison against the pre-Phase-2.3 path."""
    monkeypatch.setattr(feature_flags, "CRITIC_ENABLED", False)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("LLM should not be called when critic is disabled")

    monkeypatch.setattr(critic_mod, "get_llm", _boom)

    out = critique_sql_node(
        {
            "question": "How many customers?",
            "sql": "SELECT count(*) FROM customers",
            "sql_result": [{"count": 91}],
            "row_count": 1,
            "relevant_schema": "table customers",
        }
    )
    assert out["critic"]["verdict"] == "ok"
    assert "cost" not in out


def test_critique_sql_no_sql_returns_ok_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If somehow the critic sees a turn without SQL (defensive — the
    graph shouldn't route here without SQL), short-circuit clean."""
    monkeypatch.setattr(feature_flags, "CRITIC_ENABLED", True)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("LLM should not be called when there's no SQL")

    monkeypatch.setattr(critic_mod, "get_llm", _boom)

    out = critique_sql_node({"question": "x", "sql": "", "sql_result": []})
    assert out["critic"]["verdict"] == "ok"


def test_critique_sql_llm_exception_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feature_flags, "CRITIC_ENABLED", True)

    class _ExplodingLLM:
        def invoke(self, _messages: Any) -> Any:
            raise RuntimeError("deepseek is down")

    monkeypatch.setattr(critic_mod, "get_llm", lambda *_a, **_k: _ExplodingLLM())

    out = critique_sql_node(
        {
            "question": "x",
            "sql": "SELECT 1",
            "sql_result": [{"?": 1}],
            "row_count": 1,
            "relevant_schema": "",
        }
    )
    assert out["critic"]["verdict"] == "ok"
    assert "cost" not in out


def test_critique_sql_unparsable_llm_reply_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feature_flags, "CRITIC_ENABLED", True)

    class _StubMsg:
        content = "not json at all"

    class _Stub:
        def invoke(self, _messages: Any) -> Any:
            return _StubMsg()

    monkeypatch.setattr(critic_mod, "get_llm", lambda *_a, **_k: _Stub())

    out = critique_sql_node(
        {
            "question": "x",
            "sql": "SELECT 1",
            "sql_result": [{"?": 1}],
            "row_count": 1,
            "relevant_schema": "",
        }
    )
    assert out["critic"]["verdict"] == "ok"
    # Cost IS recorded — the LLM call happened, only parsing failed.
    assert "cost" in out


# ---------------------------------------------------------------------------
# critique_sql_node — real verdicts propagate
# ---------------------------------------------------------------------------


def _install_stub_llm(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    class _StubMsg:
        content = json.dumps(payload)

    class _Stub:
        def invoke(self, _messages: Any) -> Any:
            return _StubMsg()

    monkeypatch.setattr(critic_mod, "get_llm", lambda *_a, **_k: _Stub())


def test_critique_sql_ok_verdict_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feature_flags, "CRITIC_ENABLED", True)
    _install_stub_llm(
        monkeypatch,
        {"verdict": "ok", "reason": "matches question", "concerns": []},
    )

    out = critique_sql_node(
        {
            "question": "How many customers?",
            "sql": "SELECT count(*) FROM customers",
            "sql_result": [{"count": 91}],
            "row_count": 1,
            "relevant_schema": "table customers",
        }
    )
    assert out["critic"]["verdict"] == "ok"
    assert out["critic"]["reason"] == "matches question"


def test_critique_sql_suspicious_verdict_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feature_flags, "CRITIC_ENABLED", True)
    _install_stub_llm(
        monkeypatch,
        {
            "verdict": "suspicious",
            "reason": "JOIN may fan out duplicates",
            "concerns": ["consider DISTINCT"],
        },
    )

    out = critique_sql_node(
        {
            "question": "List customers with orders",
            "sql": "SELECT c.* FROM customers c JOIN orders o ON o.customer_id = c.id",
            "sql_result": [{"id": 1}],
            "row_count": 100,
            "relevant_schema": "tables ...",
        }
    )
    assert out["critic"]["verdict"] == "suspicious"
    assert "DISTINCT" in out["critic"]["concerns"][0]


def test_critique_sql_wrong_verdict_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feature_flags, "CRITIC_ENABLED", True)
    _install_stub_llm(
        monkeypatch,
        {
            "verdict": "wrong",
            "reason": "filters 1998 not 1997",
            "concerns": ["WHERE year = 1998 should be 1997"],
        },
    )

    out = critique_sql_node(
        {
            "question": "How many orders in 1997?",
            "sql": "SELECT count(*) FROM orders WHERE extract(year from order_date) = 1998",
            "sql_result": [{"count": 408}],
            "row_count": 1,
            "relevant_schema": "table orders",
        }
    )
    assert out["critic"]["verdict"] == "wrong"
    assert "1997" in out["critic"]["reason"]


# ---------------------------------------------------------------------------
# route_after_critic
# ---------------------------------------------------------------------------


def test_route_after_critic_ok_to_summarize() -> None:
    assert route_after_critic({"critic": {"verdict": "ok"}}) == "summarize_result"


def test_route_after_critic_suspicious_to_summarize() -> None:
    # Suspicious passes through; the FE renders a badge but we don't
    # block the user. Same as the "show the answer" path.
    assert (
        route_after_critic({"critic": {"verdict": "suspicious"}}) == "summarize_result"
    )


def test_route_after_critic_wrong_first_time_to_record_retry() -> None:
    state = {
        "critic": {"verdict": "wrong", "reason": "x"},
        "turn_index": 1,
        "attempts": [],
    }
    assert route_after_critic(state) == "record_critic_rejection"


def test_route_after_critic_wrong_after_retry_falls_through_to_summarize() -> None:
    """Critic-retry budget is 1. After the first critic retry, even
    a second "wrong" verdict should pass through to summarize with
    the badge instead of looping forever."""
    state = {
        "critic": {"verdict": "wrong", "reason": "still wrong"},
        "turn_index": 1,
        "attempts": [
            Attempt(
                sql="SELECT 1",
                error="x",
                error_class="critic_rejected",
                turn_idx=1,
            )
        ],
    }
    assert route_after_critic(state) == "summarize_result"


def test_route_after_critic_missing_state_defaults_to_summarize() -> None:
    # Defensive: a brand-new state with no critic field must not crash.
    assert route_after_critic({}) == "summarize_result"


# ---------------------------------------------------------------------------
# record_critic_rejection_node
# ---------------------------------------------------------------------------


def test_record_critic_rejection_writes_attempt_and_error() -> None:
    state = {
        "sql": "SELECT bad sql",
        "critic": {
            "verdict": "wrong",
            "reason": "filter on wrong year",
            "concerns": ["WHERE year=1998 should be 1997"],
        },
        "turn_index": 3,
    }
    out = record_critic_rejection_node(state)
    assert out["error"].startswith("critic_rejected:")
    assert len(out["attempts"]) == 1
    a = out["attempts"][0]
    assert a["sql"] == "SELECT bad sql"
    assert a["error_class"] == "critic_rejected"
    assert a["turn_idx"] == 3
    # Concerns folded into the human-readable detail string.
    assert "WHERE year=1998" in a["error"]


def test_record_critic_rejection_handles_missing_concerns() -> None:
    state = {
        "sql": "SELECT 1",
        "critic": {"verdict": "wrong", "reason": "vague", "concerns": []},
        "turn_index": 1,
    }
    out = record_critic_rejection_node(state)
    assert out["attempts"][0]["error_class"] == "critic_rejected"
    assert "concerns" not in out["attempts"][0]["error"]


# ---------------------------------------------------------------------------
# CriticVerdict pydantic model — bounds
# ---------------------------------------------------------------------------


def test_critic_verdict_truncates_long_concerns() -> None:
    v = CriticVerdict(
        verdict="suspicious",
        reason="x",
        concerns=["a" * 300, "ok"],
    )
    assert len(v.concerns[0]) == 200
    assert v.concerns[0].endswith("...")
    assert v.concerns[1] == "ok"
