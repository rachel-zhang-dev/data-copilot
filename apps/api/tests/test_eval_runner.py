"""Smoke-level tests for the eval runner.

We mock LLM + DB at the module-import boundary so the runner can
drive the full graph without external services. The goal is to
verify plumbing — feature_flags get applied, cases are graded, and
aggregation works — not to test the agent itself.
"""

from __future__ import annotations

from typing import Any

import pytest
from copilot.agent import build_graph
from copilot.eval.cases import CaseSpec, Expect, HistoryTurn
from copilot.eval.config import (
    BASELINE_FULL,
    WITHOUT_DIALOGUE_CONTEXT,
    WITHOUT_SCHEMA_RAG,
    ExperimentConfig,
)
from copilot.eval.experiments._common import run_ab
from copilot.eval.runner import run_eval
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver


class StubLLM:
    """Returns scripted responses, one per call. Used for both the
    chat LLM and (when patched) the compaction LLM."""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses) or [""]
        self.calls: list[str] = []

    def invoke(self, msgs: list[Any]) -> AIMessage:
        prompt = msgs[1].content if len(msgs) > 1 else ""
        self.calls.append(prompt)
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return AIMessage(content=self._responses[idx])


@pytest.fixture()
def patched_world(monkeypatch: pytest.MonkeyPatch):
    """Stand the agent up with everything I/O-bound replaced."""
    llm = StubLLM(
        "data",  # classify
        "SELECT count(*) FROM customers",  # generate_sql
        "There are 91 customers.",  # summarize
        # cycle continues if more cases
    )

    def _llm(*_a: Any, **_k: Any) -> StubLLM:
        return llm

    monkeypatch.setattr("copilot.llm.get_llm", _llm)
    monkeypatch.setattr("copilot.agent.nodes.get_llm", _llm)
    monkeypatch.setattr("copilot.agent.compaction.get_llm", _llm)
    monkeypatch.setattr("copilot.agent.nodes.run_select", lambda *_a, **_k: [{"count": 91}])
    monkeypatch.setattr("copilot.agent.retriever.list_tables", lambda: ["customers"])
    monkeypatch.setattr("copilot.agent.retriever.get_foreign_keys", lambda: {})
    monkeypatch.setattr(
        "copilot.agent.retriever.vector_search_tables", lambda _q, _k: ["customers"]
    )
    monkeypatch.setattr("copilot.agent.retriever.get_table_ddl", lambda _t: "Table: customers")
    monkeypatch.setattr("copilot.agent.retriever.get_schema_ddl", lambda: "Table: customers")
    return llm


def _passing_case() -> CaseSpec:
    return CaseSpec(
        id="t",
        question="How many customers?",
        category="count",
        expects=Expect(sql_must_contain=("customers", "count")),
    )


def _failing_case() -> CaseSpec:
    return CaseSpec(
        id="t",
        question="How many products?",
        category="count",
        # Insists on `products` table; our stub LLM returns customers SQL.
        expects=Expect(sql_must_contain=("products",)),
    )


# ---------------------------------------------------------------------------
# run_eval basic flow
# ---------------------------------------------------------------------------


async def test_run_eval_aggregates_correctly_on_passing(patched_world) -> None:
    cases = [_passing_case()]
    result = await run_eval(cases, BASELINE_FULL, graph=build_graph(checkpointer=InMemorySaver()))
    assert result.total == 1
    assert result.passed == 1
    assert result.success_rate == 1.0
    assert result.outcomes[0].run.attempts >= 1


async def test_run_eval_marks_failures(patched_world) -> None:
    cases = [_failing_case()]
    result = await run_eval(cases, BASELINE_FULL, graph=build_graph(checkpointer=InMemorySaver()))
    assert result.total == 1
    assert result.passed == 0
    assert len(result.failures()) == 1


async def test_by_category_groups_outcomes(patched_world) -> None:
    cases = [_passing_case(), _failing_case()]
    result = await run_eval(cases, BASELINE_FULL, graph=build_graph(checkpointer=InMemorySaver()))
    by_cat = result.by_category()
    assert "count" in by_cat
    assert by_cat["count"]["n"] == 2
    # 1 of 2 passed
    assert abs(by_cat["count"]["success_rate"] - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# Feature flag plumbing — observe that the override actually flips behaviour
# ---------------------------------------------------------------------------


async def test_schema_rag_flag_takes_effect(patched_world, monkeypatch) -> None:
    """When schema_rag_enabled=False, retrieve_schema_node should
    short-circuit to the full DDL fallback. We verify by checking the
    relevant_schema seen by generate_sql (the StubLLM records the
    prompt, including the schema)."""
    monkeypatch.setattr("copilot.agent.retriever.get_table_ddl", lambda _t: "FOCUSED_SCHEMA")
    monkeypatch.setattr("copilot.agent.retriever.get_schema_ddl", lambda: "FULL_SCHEMA_FALLBACK")

    cases = [_passing_case()]
    g = build_graph(checkpointer=InMemorySaver())

    # With RAG on (baseline)
    await run_eval(cases, BASELINE_FULL, graph=g)
    # With RAG off (treatment)
    await run_eval(cases, WITHOUT_SCHEMA_RAG, graph=g)

    on_prompt = patched_world.calls[1]  # the SQL-gen prompt of the first run
    off_prompt = patched_world.calls[4]  # second run, classify(3) + sql_gen(4)
    # Treatment with RAG on uses focused; baseline (off) uses full fallback.
    assert "FOCUSED_SCHEMA" in on_prompt
    assert "FULL_SCHEMA_FALLBACK" in off_prompt


async def test_dialogue_context_flag_off_strips_history(patched_world) -> None:
    """With the dialogue flag off, the gen prompt should NOT include
    a 'Previous turns' block, even if dialogue was seeded."""
    # Seed a prior turn through the case's setup_history.
    case = CaseSpec(
        id="fu",
        question="And France?",
        category="follow_up",
        expects=Expect(sql_must_contain=("customers",)),
        setup_history=(
            HistoryTurn(role="user", content="How many German customers?"),
            HistoryTurn(
                role="assistant",
                content="11",
                sql="SELECT count(*) FROM customers WHERE country='Germany'",
            ),
        ),
    )

    g = build_graph(checkpointer=InMemorySaver())
    await run_eval([case], WITHOUT_DIALOGUE_CONTEXT, graph=g)
    # 1st call is classify; 2nd is generate_sql
    sql_prompt = patched_world.calls[1]
    assert "Previous turns" not in sql_prompt


# ---------------------------------------------------------------------------
# Comparison wrapper
# ---------------------------------------------------------------------------


async def test_run_ab_returns_named_comparison(patched_world) -> None:
    cases = [_passing_case()]
    cmp = await run_ab(
        "smoke",
        cases,
        baseline=ExperimentConfig(label="b"),
        treatment=ExperimentConfig(label="t"),
    )
    assert cmp.name == "smoke"
    assert cmp.baseline.config.label == "b"
    assert cmp.treatment.config.label == "t"
    assert cmp.baseline.total == 1
    assert cmp.treatment.total == 1


async def test_case_timeout_recorded_as_failure() -> None:
    """A hung graph invocation must surface as a ``runner_timeout``
    failure rather than blocking the whole eval."""
    import asyncio

    class HungGraph:
        async def ainvoke(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            await asyncio.sleep(10)
            return {}

    case = _passing_case()
    result = await run_eval(
        [case], BASELINE_FULL, graph=HungGraph(), case_timeout_s=0.05
    )
    assert result.total == 1
    assert result.passed == 0
    outcome = result.outcomes[0]
    assert outcome.run.error is not None
    assert outcome.run.error.startswith("runner_timeout:")
    assert outcome.run.attempts == 0


async def test_no_timeout_keeps_legacy_behaviour(patched_world) -> None:
    """Default (timeout=None) must not wrap ``ainvoke`` in wait_for so
    existing test paths keep working unchanged."""
    cases = [_passing_case()]
    result = await run_eval(
        cases,
        BASELINE_FULL,
        graph=build_graph(checkpointer=InMemorySaver()),
        case_timeout_s=None,
    )
    assert result.passed == 1


async def test_run_ab_filters_by_category(patched_world) -> None:
    cases = [
        _passing_case(),  # category=count
        CaseSpec(
            id="fu",
            question="And France?",
            category="follow_up",
            expects=Expect(sql_must_contain=("customers",)),
            setup_history=(
                HistoryTurn(role="user", content="Germany?"),
                HistoryTurn(role="assistant", content="11"),
            ),
        ),
    ]
    cmp = await run_ab(
        "fu_only",
        cases,
        baseline=ExperimentConfig(label="b"),
        treatment=ExperimentConfig(label="t"),
        cases_filter="follow_up",
    )
    # Only the follow-up case ran on each side.
    assert cmp.baseline.total == 1
    assert cmp.treatment.total == 1
    assert cmp.baseline.outcomes[0].case.id == "fu"
