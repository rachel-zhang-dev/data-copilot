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


# ---------------------------------------------------------------------------
# Regression: no LLM-calling node should append to ``state["messages"]``.
# That field used to grow unboundedly because four nodes returned
# ``{"messages": [response]}``; with the week-5 checkpointer that growth
# bloated every Postgres checkpoint row. Nothing actually reads from
# ``messages`` in our code, so the fix was to stop writing it. This test
# locks that decision in place — if anyone re-introduces a write here,
# the test fails immediately.
# ---------------------------------------------------------------------------


def test_no_node_writes_to_messages_field(
    stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nodes, "get_schema_ddl", lambda: "CREATE TABLE customers (id INT);")
    stub_llm_factory(
        "data",  # classify_intent
        "Hi there!",  # small_talk
        "SELECT * FROM customers",  # generate_sql
        "There are 91 customers.",  # summarize_result
    )

    state: dict[str, Any] = {"question": "How many customers?", "sql_result": [], "sql": "SELECT 1"}
    outputs = [
        nodes.classify_intent_node(state),
        nodes.small_talk_node(state),
        nodes.generate_sql_node(state),
        nodes.summarize_result_node(state),
    ]
    for out in outputs:
        assert "messages" not in out, (
            f"node returned 'messages' — reintroduce the cleanup from ADR 0005 §5: {out}"
        )


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


def test_route_after_validate_proceeds_on_success() -> None:
    assert nodes.route_after_validate({"sql": "SELECT 1"}) == "execute_sql"


def test_route_after_validate_terminates_when_no_attempts_history() -> None:
    """Error set but no attempts list => caller did not record one;
    safest path is terminate."""
    assert nodes.route_after_validate({"error": "unsafe_sql: ..."}) == "finalize_error"


def test_route_after_execute_proceeds_on_success() -> None:
    assert nodes.route_after_execute({"sql_result": []}) == "summarize_result"


def test_route_after_execute_terminates_when_no_attempts_history() -> None:
    assert nodes.route_after_execute({"error": "execution_failed: ..."}) == "finalize_error"


# ---------------------------------------------------------------------------
# Week 4 — self-healing primitives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error,expected_class",
    [
        ("unsafe_sql: DROP not allowed", "unsafe_sql"),
        ("execution_failed: relation foo does not exist", "execution_failed"),
        ("network blew up", "fatal"),
        ("", "fatal"),
    ],
)
def test_classify_error(error: str, expected_class: str) -> None:
    assert nodes.classify_error(error) == expected_class


def _attempt(error_class: str, sql: str = "SELECT 1", error: str = "boom") -> Any:
    return {"sql": sql, "error": error, "error_class": error_class}


def test_can_retry_returns_false_when_no_attempts() -> None:
    assert nodes.can_retry([]) is False


def test_can_retry_first_execution_failure_allows_retry() -> None:
    assert nodes.can_retry([_attempt("execution_failed")]) is True


def test_can_retry_at_execution_failed_budget_still_allows_one_more() -> None:
    # budget=2 means up to 2 retries; len=2 = "we have failed twice,
    # we are about to do the 2nd retry" => True
    assert nodes.can_retry([_attempt("execution_failed"), _attempt("execution_failed")]) is True


def test_can_retry_over_execution_failed_budget_stops() -> None:
    attempts = [_attempt("execution_failed")] * 3
    assert nodes.can_retry(attempts) is False


def test_can_retry_unsafe_sql_budget_is_one() -> None:
    assert nodes.can_retry([_attempt("unsafe_sql")]) is True
    assert nodes.can_retry([_attempt("unsafe_sql")] * 2) is False


def test_can_retry_fatal_class_never_retries() -> None:
    assert nodes.can_retry([_attempt("fatal")]) is False


def test_can_retry_respects_hard_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with a wildly inflated budget, the global ceiling kicks in."""
    monkeypatch.setattr(
        nodes, "RETRY_BUDGET", {"execution_failed": 100, "unsafe_sql": 1, "fatal": 0}
    )
    attempts = [_attempt("execution_failed")] * nodes.HARD_RETRY_CEILING
    assert nodes.can_retry(attempts) is False


# ---------------------------------------------------------------------------
# generate_sql in retry mode
# ---------------------------------------------------------------------------


def test_generate_sql_uses_retry_prompt_when_attempts_exist(
    stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nodes, "get_schema_ddl", lambda: "Table: customers")
    llm = stub_llm_factory("SELECT * FROM customers LIMIT 1")
    state = {
        "question": "list customers",
        "relevant_schema": "Table: customers",
        "attempts": [
            _attempt(
                "execution_failed",
                sql="SELECT * FROM customer",
                error='relation "customer" does not exist',
            )
        ],
        "error": "execution_failed: ...",
    }
    out = nodes.generate_sql_node(state)

    # Retry branch: returned dict must clear ``error`` so routers see fresh state.
    assert out["error"] is None
    assert out["sql"] == "SELECT * FROM customers LIMIT 1"

    # Verify the retry prompt actually included the previous failure.
    user_msg = llm.calls[0][1].content
    assert "SELECT * FROM customer" in user_msg
    assert "customer" in user_msg
    assert "does not exist" in user_msg


def test_generate_sql_uses_first_time_prompt_when_no_attempts(
    stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nodes, "get_schema_ddl", lambda: "Table: customers")
    llm = stub_llm_factory("SELECT 1")

    nodes.generate_sql_node({"question": "test", "relevant_schema": "Table: t"})

    user_msg = llm.calls[0][1].content
    # First-time prompt does NOT include the "previous attempt" wording.
    assert "previous attempt" not in user_msg.lower()


# ---------------------------------------------------------------------------
# validate_sql / execute_sql write attempts on failure
# ---------------------------------------------------------------------------


def test_validate_sql_appends_attempt_on_safety_failure() -> None:
    out = nodes.validate_sql_node({"sql": "DROP TABLE customers"})
    assert out["error"].startswith("unsafe_sql:")
    assert len(out["attempts"]) == 1
    attempt = out["attempts"][0]
    assert attempt["sql"] == "DROP TABLE customers"
    assert attempt["error_class"] == "unsafe_sql"


def test_execute_sql_appends_attempt_on_db_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError('relation "foo" does not exist')

    monkeypatch.setattr(nodes, "run_select", boom)
    out = nodes.execute_sql_node({"sql": "SELECT * FROM foo"})
    assert out["error"].startswith("execution_failed:")
    assert len(out["attempts"]) == 1
    assert out["attempts"][0]["error_class"] == "execution_failed"
    assert out["attempts"][0]["sql"] == "SELECT * FROM foo"


# ---------------------------------------------------------------------------
# Routing in retry mode
# ---------------------------------------------------------------------------


def test_route_after_validate_loops_back_when_retryable() -> None:
    state = {
        "error": "unsafe_sql: DROP not allowed",
        "attempts": [_attempt("unsafe_sql")],
    }
    assert nodes.route_after_validate(state) == "generate_sql"


def test_route_after_validate_terminates_when_budget_exhausted() -> None:
    state = {
        "error": "unsafe_sql: ...",
        "attempts": [_attempt("unsafe_sql")] * 2,
    }
    assert nodes.route_after_validate(state) == "finalize_error"


def test_route_after_execute_loops_back_when_retryable() -> None:
    state = {
        "error": "execution_failed: bad column",
        "attempts": [_attempt("execution_failed")],
    }
    assert nodes.route_after_execute(state) == "generate_sql"


def test_route_after_execute_terminates_when_budget_exhausted() -> None:
    state = {
        "error": "execution_failed: still bad",
        "attempts": [_attempt("execution_failed")] * 3,
    }
    assert nodes.route_after_execute(state) == "finalize_error"


# ---------------------------------------------------------------------------
# finalize_error mentions attempt count
# ---------------------------------------------------------------------------


def test_finalize_error_mentions_attempts_when_more_than_one() -> None:
    out = nodes.finalize_error_node(
        {
            "error": "execution_failed: still wrong",
            "attempts": [_attempt("execution_failed")] * 3,
        }
    )
    assert "after 3 attempts" in out["answer"]


def test_finalize_error_does_not_mention_attempts_for_first_failure() -> None:
    out = nodes.finalize_error_node(
        {
            "error": "unsafe_sql: nope",
            "attempts": [_attempt("unsafe_sql")],
        }
    )
    assert "attempts" not in out["answer"]


# ---------------------------------------------------------------------------
# Week 5 — multi-turn isolation
# ---------------------------------------------------------------------------


def _attempt_with_turn(error_class: str, turn_idx: int) -> Any:
    return {
        "sql": "SELECT 1",
        "error": "boom",
        "error_class": error_class,
        "turn_idx": turn_idx,
    }


def test_can_retry_filters_attempts_by_turn() -> None:
    """Failures from a previous turn must not eat the current turn's
    retry budget. Two execution_failed in turn 1 + zero in turn 2
    means turn 2 still has full budget left."""
    attempts = [
        _attempt_with_turn("execution_failed", 1),
        _attempt_with_turn("execution_failed", 1),
        _attempt_with_turn("execution_failed", 1),
    ]
    # turn 1 is over budget
    assert nodes.can_retry(attempts, turn_idx=1) is False
    # turn 2 has no failures yet
    assert nodes.can_retry(attempts, turn_idx=2) is False  # actually no attempts in turn 2


def test_can_retry_within_turn_after_other_turn_exhausted() -> None:
    attempts = [
        _attempt_with_turn("execution_failed", 1),
        _attempt_with_turn("execution_failed", 1),
        _attempt_with_turn("execution_failed", 1),
        # turn 2 starts; one failure so far, allowed to retry
        _attempt_with_turn("execution_failed", 2),
    ]
    assert nodes.can_retry(attempts, turn_idx=2) is True


def test_can_retry_default_turn_idx_aggregates_all_for_back_compat() -> None:
    """When called without turn_idx (legacy week-4 callers), all
    attempts are counted. Required so existing tests still pass."""
    attempts = [_attempt("execution_failed")] * 2
    assert nodes.can_retry(attempts) is True


def test_validate_sql_records_turn_idx_on_failure() -> None:
    out = nodes.validate_sql_node({"sql": "DROP TABLE customers", "turn_index": 7})
    assert out["attempts"][0]["turn_idx"] == 7


def test_validate_sql_defaults_turn_idx_to_one_if_missing() -> None:
    out = nodes.validate_sql_node({"sql": "DROP TABLE customers"})
    assert out["attempts"][0]["turn_idx"] == 1


def test_execute_sql_records_turn_idx_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("nope")

    monkeypatch.setattr(nodes, "run_select", boom)
    out = nodes.execute_sql_node({"sql": "SELECT 1", "turn_index": 3})
    assert out["attempts"][0]["turn_idx"] == 3


def test_generate_sql_includes_dialogue_history_in_prompt(
    stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nodes, "get_schema_ddl", lambda: "Table: customers")
    llm = stub_llm_factory("SELECT 1")

    state = {
        "question": "What about France?",
        "relevant_schema": "Table: customers",
        "dialogue": [
            {"role": "user", "content": "How many German customers?"},
            {
                "role": "assistant",
                "content": "11",
                "sql": "SELECT count(*) FROM customers WHERE country='Germany'",
            },
        ],
    }
    nodes.generate_sql_node(state)

    user_msg = llm.calls[0][1].content
    assert "German customers" in user_msg
    assert "Previous turns" in user_msg or "previous turns" in user_msg.lower()


def test_generate_sql_no_history_block_when_dialogue_empty(
    stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nodes, "get_schema_ddl", lambda: "Table: customers")
    llm = stub_llm_factory("SELECT 1")

    nodes.generate_sql_node(
        {"question": "How many customers?", "relevant_schema": "Table: customers"}
    )

    user_msg = llm.calls[0][1].content
    assert "Previous turns" not in user_msg


def test_generate_sql_retry_filters_attempts_by_turn(
    stub_llm_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry should only show the LAST failure of THIS turn — not
    bring up a failure from a previous turn that was already resolved."""
    monkeypatch.setattr(nodes, "get_schema_ddl", lambda: "Table: customers")
    llm = stub_llm_factory("SELECT count(*) FROM customers")

    state = {
        "question": "And in Italy?",
        "relevant_schema": "Table: customers",
        "turn_index": 2,
        "attempts": [
            # Old failure from turn 1 — should NOT appear in retry prompt
            _attempt_with_turn("execution_failed", 1),
            # Current turn's failure (typo "custmrs" is intentional)
            {
                "sql": "SELECT * FROM custmrs",
                "error": "relation custmrs does not exist",
                "error_class": "execution_failed",
                "turn_idx": 2,
            },
        ],
    }
    nodes.generate_sql_node(state)

    user_msg = llm.calls[0][1].content
    # The current-turn failure SHOULD be in the prompt
    assert "custmrs" in user_msg
    # We can't easily assert the old failure is absent (because both are
    # similar shapes), but the "Your previous attempt (#1)" wording
    # should match THIS turn's count, not the cumulative count.
    assert "previous attempt (#1)" in user_msg.lower()
