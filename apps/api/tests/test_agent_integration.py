"""End-to-end integration tests.

These tests hit the **real** DeepSeek API and the **real** Postgres
database, so they are slow, cost a tiny bit of money, and require a
working ``.env`` plus ``./scripts/dev.sh up``. They are excluded from
the default ``pytest`` run via the ``integration`` marker.

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
    """Hard-skip when DeepSeek credentials are still the placeholder
    value. The integration suite is opt-in and should never silently
    pass on a misconfigured machine."""
    settings = get_settings()
    if settings.deepseek_api_key.startswith("test-") or settings.deepseek_api_key.startswith(
        "your_"
    ):
        pytest.skip("Real DEEPSEEK_API_KEY required for integration tests")


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
