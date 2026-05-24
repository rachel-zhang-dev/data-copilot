"""Unit + flow tests for the week-7 risk + HITL pair.

``check_risk_node`` and ``await_confirmation_node`` together implement
the only LangGraph ``interrupt()`` site in the agent. The tests in this
file cover three layers:

1. ``check_risk_node`` in isolation — branches on cost vs threshold,
   threshold=0 disable, EXPLAIN failure tolerated.
2. ``await_confirmation_node`` decision coercion — what counts as
   "approved" and what falls through to "rejected".
3. End-to-end through a compiled graph with the in-memory checkpointer:
   verify the graph actually pauses, surfaces the payload, and resumes
   into either ``execute_sql`` or ``finalize_error``.

The compiled-graph tests use the same LLM-and-DB stubbing pattern as
``test_eval_runner.py`` so they run in milliseconds with no external
services.
"""

from __future__ import annotations

from typing import Any

import pytest
from copilot.agent import build_graph
from copilot.agent import risk as risk_mod
from copilot.agent.risk import _coerce_decision, check_risk_node
from copilot.config import get_settings
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

# ---------------------------------------------------------------------------
# check_risk_node — pure node logic
# ---------------------------------------------------------------------------


def _state(sql: str | None) -> dict[str, Any]:
    return {"question": "q", "sql": sql} if sql is not None else {"question": "q"}


def test_check_risk_returns_empty_when_no_sql() -> None:
    """Chitchat / pre-SQL paths must not be blocked by this node."""
    assert check_risk_node(_state(None)) == {}


def test_check_risk_skipped_when_threshold_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """``risk_explain_cost_threshold=0`` is the documented disable knob."""
    monkeypatch.setattr(get_settings(), "risk_explain_cost_threshold", 0.0)
    # explain_cost should never be called when the gate is disabled.
    monkeypatch.setattr(
        risk_mod, "explain_cost", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError())
    )
    assert check_risk_node(_state("SELECT 1")) == {}


def test_check_risk_low_cost_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Low-cost SQL falls through to ``execute_sql`` (no ``pending_risk``)
    but still publishes a ``db_explain_calls`` cost increment (week 9)."""
    monkeypatch.setattr(get_settings(), "risk_explain_cost_threshold", 1000.0)
    monkeypatch.setattr(risk_mod, "explain_cost", lambda *_a, **_k: 12.3)
    out = check_risk_node(_state("SELECT 1"))
    assert "pending_risk" not in out
    assert out.get("cost") == {"db_explain_calls": 1}


def test_check_risk_high_cost_pends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "risk_explain_cost_threshold", 100.0)
    monkeypatch.setattr(risk_mod, "explain_cost", lambda *_a, **_k: 9999.0)
    out = check_risk_node(_state("SELECT * FROM huge_join"))
    assert "pending_risk" in out
    pending = out["pending_risk"]
    assert pending["sql"] == "SELECT * FROM huge_join"
    assert pending["total_cost"] == 9999.0
    assert pending["threshold"] == 100.0
    assert "9999" in pending["reason"] or "9999.0" in pending["reason"]


def test_check_risk_explain_failure_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing EXPLAIN must never block the user — the worst case is
    that an expensive query runs without a confirm prompt, which is
    identical to pre-week-7 behaviour. Cost still ticks (week 9) so
    observability sees the attempted call."""
    monkeypatch.setattr(get_settings(), "risk_explain_cost_threshold", 100.0)

    def _boom(*_a: Any, **_k: Any) -> float:
        raise RuntimeError("planner timed out")

    monkeypatch.setattr(risk_mod, "explain_cost", _boom)
    out = check_risk_node(_state("SELECT * FROM ok"))
    assert "pending_risk" not in out
    assert out.get("cost") == {"db_explain_calls": 1}


# ---------------------------------------------------------------------------
# decision coercion — what counts as "approved"?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, "approve", "approved", "yes", "Y", " APPROVE "])
def test_coerce_decision_approved(value: Any) -> None:
    assert _coerce_decision(value) == "approved"


@pytest.mark.parametrize(
    "value", [False, "reject", "rejected", "no", "n", "maybe", None, 0, "", "👍"]
)
def test_coerce_decision_rejected(value: Any) -> None:
    assert _coerce_decision(value) == "rejected"


# ---------------------------------------------------------------------------
# Compiled-graph flow — interrupt + resume
# ---------------------------------------------------------------------------


@pytest.fixture()
def stubbed_world(monkeypatch: pytest.MonkeyPatch):
    """Stand the whole agent up with everything I/O-bound replaced."""

    class _StubLLM:
        def __init__(self, *responses: str) -> None:
            self._responses = list(responses) or [""]
            self.calls: list[Any] = []

        def invoke(self, msgs: list[Any]) -> AIMessage:
            idx = min(len(self.calls), len(self._responses) - 1)
            self.calls.append(msgs)
            return AIMessage(content=self._responses[idx])

    llm = _StubLLM(
        "data",  # classify_intent
        "SELECT * FROM big_table",  # generate_sql
        "Big table has many rows.",  # summarize_result (only used on approve)
    )

    def _llm(*_a: Any, **_k: Any) -> _StubLLM:
        return llm

    monkeypatch.setattr("copilot.llm.get_llm", _llm)
    monkeypatch.setattr("copilot.agent.nodes.get_llm", _llm)
    monkeypatch.setattr("copilot.agent.compaction.get_llm", _llm)
    monkeypatch.setattr(
        "copilot.agent.nodes.run_select", lambda *_a, **_k: [{"id": 1}, {"id": 2}]
    )
    monkeypatch.setattr("copilot.agent.retriever.list_tables", lambda: ["big_table"])
    monkeypatch.setattr("copilot.agent.retriever.get_foreign_keys", lambda: {})
    monkeypatch.setattr(
        "copilot.agent.retriever.vector_search_tables", lambda _q, _k: ["big_table"]
    )
    monkeypatch.setattr(
        "copilot.agent.retriever.get_table_ddl", lambda _t: "Table: big_table"
    )
    monkeypatch.setattr(
        "copilot.agent.retriever.get_schema_ddl", lambda: "Table: big_table"
    )
    return llm


async def test_low_cost_skips_confirmation(
    monkeypatch: pytest.MonkeyPatch, stubbed_world
) -> None:
    """A cheap query routes straight through to execute_sql with no
    interrupt — verifies week-7's insertion didn't break the happy path."""
    monkeypatch.setattr(get_settings(), "risk_explain_cost_threshold", 1000.0)
    monkeypatch.setattr(risk_mod, "explain_cost", lambda *_a, **_k: 5.0)

    graph = build_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t-low"}}

    out = await graph.ainvoke({"question": "How many rows?"}, config=cfg)
    assert out.get("__interrupt__") is None
    assert out.get("answer")
    assert out.get("row_count") == 2
    assert out.get("error") is None


async def test_high_cost_pauses_for_confirmation(
    monkeypatch: pytest.MonkeyPatch, stubbed_world
) -> None:
    """An expensive query pauses with a populated payload and the
    graph's ``next`` pointing at ``await_confirmation``."""
    monkeypatch.setattr(get_settings(), "risk_explain_cost_threshold", 100.0)
    monkeypatch.setattr(risk_mod, "explain_cost", lambda *_a, **_k: 12345.0)

    graph = build_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t-pause"}}

    out = await graph.ainvoke({"question": "Show everything."}, config=cfg)
    interrupts = out.get("__interrupt__")
    assert interrupts, "graph should be paused"
    payload = interrupts[0].value
    # The safety layer rewrote the LLM SQL with an injected LIMIT before
    # check_risk ran; that is the SQL the user actually has to approve,
    # so it is what the payload should carry.
    assert payload["sql"] == "SELECT * FROM big_table LIMIT 100"
    assert payload["total_cost"] == 12345.0
    assert payload["threshold"] == 100.0

    snapshot = await graph.aget_state(cfg)
    assert snapshot.next == ("await_confirmation",)
    # No answer / row_count yet — the user has not approved.
    assert not out.get("answer")
    assert out.get("row_count") is None


async def test_resume_approve_executes_sql(
    monkeypatch: pytest.MonkeyPatch, stubbed_world
) -> None:
    """Approve → execute_sql runs and the turn finishes normally."""
    monkeypatch.setattr(get_settings(), "risk_explain_cost_threshold", 100.0)
    monkeypatch.setattr(risk_mod, "explain_cost", lambda *_a, **_k: 12345.0)

    graph = build_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t-approve"}}

    await graph.ainvoke({"question": "Show everything."}, config=cfg)
    out = await graph.ainvoke(Command(resume="approve"), config=cfg)

    assert out.get("__interrupt__") is None
    assert out.get("row_count") == 2
    assert out.get("error") is None
    assert out.get("risk_decision") == "approved"
    assert out["answer"]


async def test_resume_reject_terminates_with_user_rejected(
    monkeypatch: pytest.MonkeyPatch, stubbed_world
) -> None:
    """Reject → execute_sql NEVER runs, finalize_error produces a polite
    refusal, ``attempts`` carries the user_rejected record."""
    monkeypatch.setattr(get_settings(), "risk_explain_cost_threshold", 100.0)
    monkeypatch.setattr(risk_mod, "explain_cost", lambda *_a, **_k: 12345.0)

    ran_select: list[str] = []

    def _capture_select(sql: str) -> list[dict[str, Any]]:
        ran_select.append(sql)
        return []

    monkeypatch.setattr("copilot.agent.nodes.run_select", _capture_select)

    graph = build_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t-reject"}}

    await graph.ainvoke({"question": "Show everything."}, config=cfg)
    out = await graph.ainvoke(Command(resume="reject"), config=cfg)

    assert ran_select == [], "execute_sql must not run after a rejection"
    assert out.get("__interrupt__") is None
    assert out.get("risk_decision") == "rejected"
    assert (out.get("error") or "").startswith("user_rejected:")
    assert "did not run that query" in (out.get("answer") or "").lower()

    attempts = [a for a in (out.get("attempts") or []) if a.get("error_class") == "user_rejected"]
    assert len(attempts) == 1
    assert attempts[0]["sql"] == "SELECT * FROM big_table LIMIT 100"


async def test_pending_risk_cleared_on_next_turn(
    monkeypatch: pytest.MonkeyPatch, stubbed_world
) -> None:
    """``reset_per_turn_node`` must wipe ``pending_risk`` so a fresh
    turn never inherits a stale 'pending' marker from a previous one."""
    monkeypatch.setattr(get_settings(), "risk_explain_cost_threshold", 100.0)
    monkeypatch.setattr(risk_mod, "explain_cost", lambda *_a, **_k: 12345.0)

    graph = build_graph(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t-clear"}}

    # Turn 1: pause then reject.
    await graph.ainvoke({"question": "Show everything."}, config=cfg)
    await graph.ainvoke(Command(resume="reject"), config=cfg)

    # Turn 2: lower the cost so the second turn skips HITL entirely.
    monkeypatch.setattr(risk_mod, "explain_cost", lambda *_a, **_k: 5.0)
    out2 = await graph.ainvoke({"question": "How many rows now?"}, config=cfg)

    assert out2.get("pending_risk") is None
    assert out2.get("risk_decision") is None
    assert out2.get("__interrupt__") is None
