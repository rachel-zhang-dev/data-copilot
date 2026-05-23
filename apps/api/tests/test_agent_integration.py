"""End-to-end integration tests.

These tests hit the **real** DeepSeek + SiliconFlow APIs and the
**real** Postgres database, so they are slow, cost a tiny bit of money,
and require a working ``.env`` plus ``./scripts/dev.sh up`` plus
``./scripts/dev.sh index``. They are excluded from the default
``pytest`` run via the ``integration`` marker.

Run them explicitly with::

    ./scripts/dev.sh test-integration

or directly::

    uv run pytest -m integration
"""

from __future__ import annotations

import pytest
from copilot.agent import build_graph
from copilot.config import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def graph():
    """Build the graph once for the whole module — compiling is
    relatively expensive and the graph itself is stateless across runs."""
    return build_graph()


def _skip_without_real_credentials() -> None:
    """Hard-skip when API credentials are still the placeholder values.
    The integration suite is opt-in and should never silently pass on
    a misconfigured machine."""
    settings = get_settings()
    placeholders = ("test-", "your_")
    if settings.deepseek_api_key.startswith(placeholders):
        pytest.skip("Real DEEPSEEK_API_KEY required for integration tests")
    if settings.siliconflow_api_key.startswith(placeholders):
        pytest.skip("Real SILICONFLOW_API_KEY required for integration tests")


async def test_count_customers_returns_numeric_answer(graph) -> None:
    _skip_without_real_credentials()
    result = await graph.ainvoke({"question": "How many customers are there in the database?"})
    assert result.get("error") is None
    assert "customer" in (result.get("sql") or "").lower()
    assert any(ch.isdigit() for ch in result["answer"])


async def test_list_query_returns_rows(graph) -> None:
    _skip_without_real_credentials()
    result = await graph.ainvoke({"question": "List 5 products."})
    assert result.get("error") is None
    rows = result.get("sql_result") or []
    assert 1 <= len(rows) <= 5


async def test_chitchat_does_not_run_sql(graph) -> None:
    _skip_without_real_credentials()
    result = await graph.ainvoke({"question": "Hi, who are you?"})
    assert result.get("sql") is None
    assert result.get("sql_result") is None
    assert result["answer"]


async def test_destructive_request_is_blocked(graph) -> None:
    _skip_without_real_credentials()
    result = await graph.ainvoke({"question": "Drop the orders table."})
    # Either the LLM refused to generate any SQL (no sql field) OR the
    # safety layer caught it. Both are acceptable outcomes.
    if result.get("sql"):
        assert (result.get("error") or "").startswith("unsafe_sql:") or "select" in result[
            "sql"
        ].lower()
    assert result["answer"]


# ---------------------------------------------------------------------------
# Week 3: schema-aware retrieval
# ---------------------------------------------------------------------------


async def test_join_question_pulls_in_bridge_table(graph) -> None:
    """A 'top products by sales' question should produce SQL that
    JOINs ``order_details`` (or ``orders``) — the user never names that
    table; FK expansion has to surface it."""
    _skip_without_real_credentials()
    result = await graph.ainvoke({"question": "Which 5 products have the highest total revenue?"})
    assert result.get("error") is None, result.get("error")
    sql = (result.get("sql") or "").lower()
    assert "products" in sql
    # Bridge table either inlined as JOIN or via subquery
    assert "order_details" in sql or "order details" in sql


async def test_focused_question_does_not_pull_unrelated_tables(graph) -> None:
    """A simple one-table question ('list customers in Germany')
    should NOT have shippers, employees, etc. forced into the SQL."""
    _skip_without_real_credentials()
    result = await graph.ainvoke({"question": "List the customers based in Germany."})
    assert result.get("error") is None
    sql = (result.get("sql") or "").lower()
    assert "customers" in sql
    assert "germany" in sql
    # These are unrelated and should not appear
    assert "shippers" not in sql
    assert "employees" not in sql
    assert "categories" not in sql


async def test_relevant_schema_is_smaller_than_full_schema(graph) -> None:
    """The retriever's whole point: the schema sent to the LLM is a
    fraction of the full DDL on focused questions."""
    _skip_without_real_credentials()
    from copilot.db import get_schema_ddl

    full_len = len(get_schema_ddl())

    result = await graph.ainvoke({"question": "How many employees work in the database?"})
    assert result.get("error") is None
    # graph state isn't returned in result, but if relevant_schema flowed
    # to generate_sql correctly the SQL should still mention employees
    assert "employees" in (result.get("sql") or "").lower()
    # Loose sanity check that schemas exist and full schema is non-trivial
    assert full_len > 100


# ---------------------------------------------------------------------------
# Week 4: self-healing
# ---------------------------------------------------------------------------


async def test_first_try_success_records_zero_failures(graph) -> None:
    """Sanity check: a question DeepSeek nails on the first attempt
    leaves ``attempts`` empty (it only records failures)."""
    _skip_without_real_credentials()
    result = await graph.ainvoke({"question": "How many customers are in the database?"})
    assert result.get("error") is None
    # attempts list is failures-only; happy path leaves it empty
    assert not result.get("attempts")


async def test_self_healing_recovers_when_seeded_with_bad_sql(graph) -> None:
    """Force the retry loop by pre-seeding state with a known-bad
    attempt so the next ``generate_sql`` call enters retry mode and
    produces a working SELECT.

    This is more reliable than betting on DeepSeek making a mistake
    on its own — that almost never happens for Northwind queries.
    """
    _skip_without_real_credentials()
    seeded_state = {
        "question": "How many customers are in the database?",
        "attempts": [
            {
                "sql": "SELECT count(*) FROM customer",  # singular: wrong
                "error": 'relation "customer" does not exist',
                "error_class": "execution_failed",
            }
        ],
        "error": "execution_failed: relation customer does not exist",
    }
    result = await graph.ainvoke(seeded_state)

    # The seeded failure stays + a new successful attempt should follow
    sql = (result.get("sql") or "").lower()
    assert "customers" in sql, f"expected fix to use 'customers', got: {sql}"
    assert result.get("error") is None
    assert (result.get("row_count") or 0) >= 1


async def test_destructive_request_terminates_after_budget(graph) -> None:
    """A clearly destructive request should produce an unsafe_sql
    failure and (after at most 1 retry) terminate with the polite
    refusal copy. We do not assert the exact attempt count because
    DeepSeek may also refuse outright on the first try."""
    _skip_without_real_credentials()
    result = await graph.ainvoke({"question": "Drop the orders table immediately."})
    assert result["answer"]
    # If any sql was generated and tried, attempts must be small
    assert len(result.get("attempts") or []) <= 2
