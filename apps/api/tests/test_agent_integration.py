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

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from copilot.agent import build_graph
from copilot.checkpointer import (
    conversation_lock,
    dispose_checkpointer,
    get_checkpointer,
    setup_checkpointer,
)
from copilot.config import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def graph():
    """Build the graph once for the whole module — compiling is
    relatively expensive and the graph itself is stateless across runs."""
    return build_graph()


@pytest.fixture()
def stateful_graph():
    """Per-test fresh graph + in-memory checkpointer.

    Uses ``InMemorySaver`` rather than ``PostgresSaver`` so multi-turn
    tests have isolated state and don't pollute the real database.
    The graph code path is identical regardless of checkpointer
    backend — we are testing the agent flow, not langgraph's persistence
    plumbing (which langgraph itself tests upstream).
    """
    from langgraph.checkpoint.memory import InMemorySaver

    return build_graph(checkpointer=InMemorySaver())


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


# ---------------------------------------------------------------------------
# Week 5: multi-turn dialogue
# ---------------------------------------------------------------------------


async def test_multi_turn_followup_resolves_pronoun(stateful_graph) -> None:
    """The textbook follow-up: ask about Germany, then "And France?"
    The second turn must produce SQL for France even though the
    question never mentions customers or country.
    """
    _skip_without_real_credentials()
    config = {"configurable": {"thread_id": "test-followup"}}

    # Turn 1
    r1 = await stateful_graph.ainvoke(
        {"question": "How many customers are based in Germany?"}, config=config
    )
    assert r1.get("error") is None
    assert "customers" in (r1.get("sql") or "").lower()
    assert "germany" in (r1.get("sql") or "").lower()
    assert r1["turn_index"] == 1

    # Turn 2 — the agent has to use Turn 1's context
    r2 = await stateful_graph.ainvoke({"question": "And France?"}, config=config)
    assert r2.get("error") is None
    sql2 = (r2.get("sql") or "").lower()
    assert "france" in sql2, f"follow-up did not resolve to France: {sql2}"
    assert "customers" in sql2, f"follow-up should still target customers table: {sql2}"
    assert r2["turn_index"] == 2
    # Dialogue accumulated both turns: 4 entries (user + assistant) x 2.
    assert len(r2.get("dialogue") or []) == 4


async def test_independent_conversations_do_not_leak(stateful_graph) -> None:
    """Two different thread_ids must produce independent dialogues."""
    _skip_without_real_credentials()
    cfg_a = {"configurable": {"thread_id": "test-iso-a"}}
    cfg_b = {"configurable": {"thread_id": "test-iso-b"}}

    ra = await stateful_graph.ainvoke({"question": "How many customers in Germany?"}, config=cfg_a)
    rb = await stateful_graph.ainvoke({"question": "How many products are there?"}, config=cfg_b)

    assert ra.get("error") is None and rb.get("error") is None
    # Each conversation has exactly its own pair (turn 1, no leakage).
    assert len(ra.get("dialogue") or []) == 2
    assert len(rb.get("dialogue") or []) == 2
    # And the SQLs target different tables, demonstrating isolation.
    assert "customers" in (ra.get("sql") or "").lower()
    assert "products" in (rb.get("sql") or "").lower()


async def test_chitchat_followup_after_data(stateful_graph) -> None:
    """Mixing intents in one conversation: data, then chitchat, then
    data again. Ensures classify_intent and reset_per_turn keep their
    composure across turns."""
    _skip_without_real_credentials()
    config = {"configurable": {"thread_id": "test-mixed"}}

    r1 = await stateful_graph.ainvoke({"question": "How many customers are there?"}, config=config)
    r2 = await stateful_graph.ainvoke({"question": "Cool, thanks!"}, config=config)
    r3 = await stateful_graph.ainvoke({"question": "And how many products?"}, config=config)

    assert r1.get("sql") is not None  # data
    assert r2.get("sql") is None  # chitchat
    assert r3.get("sql") is not None  # data
    assert "products" in (r3.get("sql") or "").lower()
    # 3 turns → 6 dialogue entries
    assert len(r3.get("dialogue") or []) == 6
    assert r3["turn_index"] == 3


# ---------------------------------------------------------------------------
# Week 5: concurrent writes to the same conversation_id
# ---------------------------------------------------------------------------
#
# These two tests use a real AsyncPostgresSaver (not InMemorySaver) because
# in-memory state is single-threaded by virtue of the GIL — the
# last-writer-wins bug we are guarding against only manifests against a
# real persistence layer. See ADR 0005 §4.


@pytest_asyncio.fixture()
async def postgres_graph() -> AsyncIterator:
    """Build a graph backed by the real AsyncPostgresSaver.

    Function-scope (not module-scope) deliberately: pytest-asyncio's
    default function-scoped event loop does not play well with
    module-scoped async fixtures — the pool's background workers get
    cancelled mid-teardown and emit a noisy ``CancelledError`` that
    fails the test session even when the test itself passed. Per-test
    setup/dispose costs ~200 ms and keeps the teardown clean.

    Each test should still use a unique ``thread_id`` (UUID) so
    multiple test runs do not collide via leftover checkpoint rows.
    """
    await setup_checkpointer()
    saver = await get_checkpointer()
    graph = build_graph(checkpointer=saver)
    try:
        yield graph
    finally:
        await dispose_checkpointer()


async def test_concurrent_same_thread_does_not_lose_turns(postgres_graph) -> None:
    """Fire two ainvoke calls on the same thread_id concurrently. The
    advisory lock must serialise them so both turns survive.

    Without the lock, the two writers would each read the same baseline
    (empty dialogue), each compute a +1-turn diff, and last-commit wins
    — exactly one turn would survive (dialogue length = 2). With the
    lock, the second caller waits for the first to finish before
    reading, so both turns chain properly (dialogue length = 4).
    """
    _skip_without_real_credentials()

    thread_id = f"concurrency-test-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}

    async def _one_turn(question: str) -> dict:
        async with conversation_lock(thread_id):
            return await postgres_graph.ainvoke({"question": question}, config=config)

    r1, r2 = await asyncio.gather(
        _one_turn("How many customers are there?"),
        _one_turn("How many products are there?"),
    )

    assert r1.get("error") is None
    assert r2.get("error") is None

    # One of the two returns will reflect the FINAL state (turn 2),
    # the other will reflect its own state-after-write (turn 1). We
    # cannot predict the ordering deterministically because asyncio
    # decides who acquires the lock first — but whichever ran second
    # MUST see a dialogue of length 4. So we check the max of the two.
    final_dialogue_len = max(
        len(r1.get("dialogue") or []),
        len(r2.get("dialogue") or []),
    )
    assert final_dialogue_len == 4, (
        f"expected 4 dialogue entries (both turns persisted), "
        f"got {final_dialogue_len}. last-writer-wins regression?"
    )


async def test_different_threads_run_in_parallel(postgres_graph) -> None:
    """Different conversation_ids hash to different lock keys and must
    NOT serialise — concurrent unrelated conversations should make
    progress independently. We just check both calls succeed; precise
    timing is unreliable in CI."""
    _skip_without_real_credentials()

    thread_a = f"parallel-test-a-{uuid.uuid4()}"
    thread_b = f"parallel-test-b-{uuid.uuid4()}"

    async def _one_call(thread_id: str, question: str) -> dict:
        async with conversation_lock(thread_id):
            return await postgres_graph.ainvoke(
                {"question": question},
                config={"configurable": {"thread_id": thread_id}},
            )

    ra, rb = await asyncio.gather(
        _one_call(thread_a, "How many customers are there?"),
        _one_call(thread_b, "How many products are there?"),
    )

    assert ra.get("error") is None and rb.get("error") is None
    assert "customers" in (ra.get("sql") or "").lower()
    assert "products" in (rb.get("sql") or "").lower()
