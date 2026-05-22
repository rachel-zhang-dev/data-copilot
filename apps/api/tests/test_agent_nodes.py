"""Unit tests for individual LangGraph nodes.

We mock the LLM (via ``stub_llm_factory``) and the database (via
``monkeypatch`` on ``copilot.agent.nodes.run_select`` /
``copilot.agent.nodes.get_schema_ddl``). The goal is to exercise the
node logic itself — prompt assembly, state diff, error handling — in
isolation, with no network calls.

Integration tests with the real LLM and real DB live in
``test_agent_integration.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from copilot.agent import nodes

# ---------------------------------------------------------------------------
# classify_intent_node
# ---------------------------------------------------------------------------


def test_classify_intent_returns_data_for_data_question(stub_llm_factory) -> None:
    stub_llm_factory("data")
    out = nodes.classify_intent_node({"question": "How many customers are there?"})
    assert out["intent"] == "data"


def test_classify_intent_returns_chitchat_for_greeting(stub_llm_factory) -> None:
    stub_llm_factory("chitchat")
    out = nodes.classify_intent_node({"question": "Hello!"})
    assert out["intent"] == "chitchat"


def test_classify_intent_falls_back_to_data_on_unrecognised_reply(
    stub_llm_factory,
) -> None:
    stub_llm_factory("¯\\_(ツ)_/¯")
    out = nodes.classify_intent_node({"question": "what?"})
    assert out["intent"] == "data"


def test_classify_intent_strips_whitespace_and_case(stub_llm_factory) -> None:
    stub_llm_factory("  CHITCHAT\n")
    out = nodes.classify_intent_node({"question": "hi"})
    assert out["intent"] == "chitchat"


# ---------------------------------------------------------------------------
# small_talk_node
# ---------------------------------------------------------------------------


def test_small_talk_produces_an_answer(stub_llm_factory) -> None:
    stub_llm_factory("Hi! I help answer questions about your data.")
    out = nodes.small_talk_node({"question": "Hello"})
    assert "data" in out["answer"].lower()


# ---------------------------------------------------------------------------
# generate_sql_node
# ---------------------------------------------------------------------------


def test_generate_sql_uses_provided_schema_and_returns_sql(
    stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The schema in state takes precedence over the introspected one.
    monkeypatch.setattr(nodes, "get_schema_ddl", lambda: "ALWAYS_WRONG")
    llm = stub_llm_factory("SELECT COUNT(*) FROM customers")
    out = nodes.generate_sql_node(
        {
            "question": "How many customers?",
            "relevant_schema": "Table: customers\n  - customer_id (varchar)",
        }
    )
    assert out["sql"] == "SELECT COUNT(*) FROM customers"
    # Verify the schema we pass through actually reached the LLM.
    user_msg_content = llm.calls[0][1].content
    assert "customer_id" in user_msg_content


def test_generate_sql_falls_back_to_introspection_when_no_schema(
    stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nodes, "get_schema_ddl", lambda: "Table: orders\n")
    stub_llm_factory("SELECT * FROM orders")
    out = nodes.generate_sql_node({"question": "show orders"})
    assert "orders" in out["sql"].lower()
    assert "orders" in out["relevant_schema"].lower()


# ---------------------------------------------------------------------------
# validate_sql_node
# ---------------------------------------------------------------------------


def test_validate_sql_passes_and_injects_limit() -> None:
    out = nodes.validate_sql_node({"sql": "SELECT * FROM customers"})
    assert "LIMIT" in out["sql"].upper()
    assert "error" not in out


def test_validate_sql_rejects_dangerous_sql() -> None:
    out = nodes.validate_sql_node({"sql": "DROP TABLE customers"})
    assert out["error"].startswith("unsafe_sql:")


def test_validate_sql_strips_fences_then_validates() -> None:
    out = nodes.validate_sql_node({"sql": "```sql\nSELECT 1\n```"})
    assert "error" not in out
    assert "SELECT" in out["sql"].upper()


# ---------------------------------------------------------------------------
# execute_sql_node
# ---------------------------------------------------------------------------


def test_execute_sql_returns_rows_and_count(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"count": 91}]
    monkeypatch.setattr(nodes, "run_select", lambda *_a, **_k: rows)
    out = nodes.execute_sql_node({"sql": "SELECT COUNT(*) FROM customers"})
    assert out["sql_result"] == rows
    assert out["row_count"] == 1


def test_execute_sql_captures_db_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError('relation "foo" does not exist')

    monkeypatch.setattr(nodes, "run_select", boom)
    out = nodes.execute_sql_node({"sql": "SELECT * FROM foo"})
    assert out["error"].startswith("execution_failed:")
    assert "foo" in out["error"]


# ---------------------------------------------------------------------------
# summarize_result_node
# ---------------------------------------------------------------------------


def test_summarize_result_uses_llm_to_phrase_answer(stub_llm_factory) -> None:
    stub_llm_factory("There are 91 customers in total.")
    out = nodes.summarize_result_node(
        {
            "question": "How many customers?",
            "sql": "SELECT COUNT(*) FROM customers LIMIT 100",
            "sql_result": [{"count": 91}],
            "row_count": 1,
        }
    )
    assert "91" in out["answer"]


# ---------------------------------------------------------------------------
# finalize_error_node
# ---------------------------------------------------------------------------


def test_finalize_error_explains_safety_violation() -> None:
    out = nodes.finalize_error_node({"error": "unsafe_sql: DROP not allowed"})
    assert "read-only" in out["answer"].lower()


def test_finalize_error_explains_execution_failure() -> None:
    out = nodes.finalize_error_node({"error": "execution_failed: relation x does not exist"})
    assert "database returned an error" in out["answer"].lower()


def test_finalize_error_handles_unknown_error() -> None:
    out = nodes.finalize_error_node({})
    assert "unknown_error" in out["answer"].lower()


# ---------------------------------------------------------------------------
# Routing functions
# ---------------------------------------------------------------------------


def test_route_after_classify_dispatches_on_intent() -> None:
    assert nodes.route_after_classify({"intent": "chitchat"}) == "small_talk"
    assert nodes.route_after_classify({"intent": "data"}) == "generate_sql"
    # Default branch when intent is missing should not crash; falls back to data.
    assert nodes.route_after_classify({}) == "generate_sql"


def test_route_after_validate_uses_error_field() -> None:
    assert nodes.route_after_validate({"error": "unsafe_sql: ..."}) == "finalize_error"
    assert nodes.route_after_validate({"sql": "SELECT 1"}) == "execute_sql"


def test_route_after_execute_uses_error_field() -> None:
    assert nodes.route_after_execute({"error": "execution_failed: ..."}) == "finalize_error"
    assert nodes.route_after_execute({"sql_result": []}) == "summarize_result"
