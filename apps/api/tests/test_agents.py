"""Tests for the week-12.5 multi-agent layer.

Three groups:

1. **Analyst parser** — pure, no I/O. Mirrors ``test_insight.py`` for
   the SQL Specialist's structured output.
2. **Supervisor routing** — pure functions ``route_after_sql`` and
   ``route_after_analyst``; every branch of the decision tables.
3. **Compiled-graph flow** — end-to-end via the supervisor graph
   with stubbed Specialist + LLM, covering the happy path,
   short-circuit branches, drill-down loop, hop-count enforcement,
   feature-flag override, and cost accumulation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from copilot.agent import feature_flags
from copilot.agents import build_supervisor_graph
from copilot.agents.analyst import parse_response
from copilot.agents.messages import AnalystAnomaly, AnalystResponse, DrillDownRequest
from copilot.agents.supervisor import (
    MAX_HOP_COUNT,
    route_after_analyst,
    route_after_sql,
)
from langchain_core.messages import AIMessage
from langgraph.graph import END

# ---------------------------------------------------------------------------
# Analyst parser
# ---------------------------------------------------------------------------


def test_analyst_parses_empty_envelope() -> None:
    raw = '{"anomalies": [], "followups": [], "drill_down": null}'
    out = parse_response(raw)
    assert isinstance(out, AnalystResponse)
    assert out.anomalies == []
    assert out.followups == []
    assert out.drill_down is None


def test_analyst_parses_full_envelope() -> None:
    raw = (
        '{"anomalies": [{"label":"Skew","detail":"Top 1 holds 80%","severity":"warn"}],'
        '"followups": [{"question":"Show by quarter","rationale":"trend",'
        '"expected_chart_kind":"line"}],'
        '"drill_down": {"question":"Top 3 only","why":"focus the chart"}}'
    )
    out = parse_response(raw)
    assert out is not None
    assert out.anomalies[0].severity == "warn"
    assert out.followups[0].expected_chart_kind == "line"
    assert out.drill_down is not None
    assert out.drill_down.question == "Top 3 only"


def test_analyst_strips_fences() -> None:
    raw = '```json\n{"anomalies":[],"followups":[],"drill_down":null}\n```'
    out = parse_response(raw)
    assert out is not None


def test_analyst_returns_none_on_garbage() -> None:
    assert parse_response("definitely not json") is None
    # Non-object root → schema mismatch
    assert parse_response("[1,2,3]") is None
    assert parse_response("") is None
    assert parse_response("   ") is None


def test_analyst_empty_envelope_is_valid_silent_output() -> None:
    """``{}`` is a valid Analyst response — every field has a default
    and "silence" is the correct behaviour on uninteresting data."""
    out = parse_response("{}")
    assert isinstance(out, AnalystResponse)
    assert out.anomalies == []
    assert out.followups == []
    assert out.drill_down is None


# ---------------------------------------------------------------------------
# Supervisor routing
# ---------------------------------------------------------------------------


def _sql_state(**kwargs: Any) -> dict[str, Any]:
    """Build a Specialist-state dict with sensible defaults."""
    return {
        "sql_result": [{"a": 1}],
        "row_count": 1,
        "chart_kind": "table",
        "intent": "data",
        "answer": "x",
        "sql": "SELECT 1",
        **kwargs,
    }


def test_route_after_sql_chitchat_short_circuits() -> None:
    out = route_after_sql({"sql_result": _sql_state(intent="chitchat")})
    assert out == END


def test_route_after_sql_error_short_circuits() -> None:
    out = route_after_sql({"sql_result": _sql_state(error="execution_failed: ...")})
    assert out == END


def test_route_after_sql_empty_rows_short_circuits() -> None:
    out = route_after_sql({"sql_result": {**_sql_state(), "sql_result": []}})
    assert out == END


def test_route_after_sql_kpi_single_row_short_circuits() -> None:
    out = route_after_sql({"sql_result": _sql_state(chart_kind="kpi")})
    assert out == END


def test_route_after_sql_paused_short_circuits() -> None:
    """HITL pause defers Analyst until after the user approves."""
    out = route_after_sql({"sql_result": {**_sql_state(), "__interrupt__": ("anything",)}})
    assert out == END


def test_route_after_sql_disabled_flag_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feature_flags, "ANALYST_ENABLED", False)
    out = route_after_sql({"sql_result": _sql_state(chart_kind="bar", row_count=10)})
    assert out == END


def test_route_after_sql_real_data_invokes_analyst() -> None:
    out = route_after_sql(
        {
            "sql_result": _sql_state(
                sql_result=[{"x": 1}, {"x": 2}, {"x": 3}],
                row_count=3,
                chart_kind="bar",
            )
        }
    )
    assert out == "analyst"


def test_route_after_analyst_no_drill_down_ends() -> None:
    state = {
        "hop_count": 1,
        "analyst": AnalystResponse(),  # no drill_down
    }
    assert route_after_analyst(state) == END


def test_route_after_analyst_with_drill_down_loops_back() -> None:
    state = {
        "hop_count": 1,
        "analyst": AnalystResponse(
            drill_down=DrillDownRequest(question="sharper", why="dig in")
        ),
    }
    assert route_after_analyst(state) == "sql_specialist"


def test_route_after_analyst_hop_budget_terminates() -> None:
    """Even with a drill-down request, exceeding the hop budget ends."""
    state = {
        "hop_count": MAX_HOP_COUNT,
        "analyst": AnalystResponse(
            drill_down=DrillDownRequest(question="more", why="...")
        ),
    }
    assert route_after_analyst(state) == END


# ---------------------------------------------------------------------------
# Compiled-graph flow (end-to-end via the supervisor)
# ---------------------------------------------------------------------------


def _fake_sql_graph(returns: list[dict[str, Any]]):
    """Build a MagicMock SQL graph whose ``ainvoke`` returns each
    entry of ``returns`` in turn (so the drill-down loop sees a
    different result for the second invocation)."""
    graph = MagicMock()
    graph.ainvoke = AsyncMock(side_effect=list(returns))
    graph.aget_state = AsyncMock()
    return graph


def _analyst_response(*, drill: bool = False) -> AnalystResponse:
    return AnalystResponse(
        anomalies=[AnalystAnomaly(label="L", detail="D", severity="info")],
        followups=[],
        drill_down=DrillDownRequest(question="dig", why="why") if drill else None,
    )


@pytest.fixture()
def patch_analyst_llm(monkeypatch: pytest.MonkeyPatch):
    """Stub the analyst's LLM so it returns a controllable AnalystResponse
    on each call. Tests pass the response sequence in via the fixture
    factory."""

    def _install(*responses: AnalystResponse | None) -> list[Any]:
        calls: list[Any] = []

        class _Stub:
            def __init__(self) -> None:
                self._i = 0

            def invoke(self, msgs: list[Any]) -> AIMessage:
                calls.append(msgs)
                resp = responses[min(self._i, len(responses) - 1)]
                self._i += 1
                if resp is None:
                    return AIMessage(content="not json at all")
                return AIMessage(content=resp.model_dump_json())

        stub = _Stub()
        monkeypatch.setattr("copilot.agents.analyst.nodes.get_llm", lambda **_k: stub)
        return calls

    return _install


async def test_supervisor_data_path_invokes_analyst(patch_analyst_llm) -> None:
    """A normal multi-row answer routes through the Analyst and the
    final SupervisorState carries both ``sql_result`` and ``analyst``."""
    sql_graph = _fake_sql_graph([
        {
            "sql_result": [{"x": 1}, {"x": 2}],
            "row_count": 2,
            "chart_kind": "bar",
            "intent": "data",
            "answer": "two rows",
            "sql": "SELECT x",
            "turn_index": 1,
            "cost": {"llm_calls": 1, "est_usd": 0.001},
        },
    ])
    patch_analyst_llm(_analyst_response())

    supervisor = build_supervisor_graph(sql_graph)
    out = await supervisor.ainvoke(
        {"question": "q", "conversation_id": "t1", "hop_count": 0, "drill_downs": []},
    )

    assert out["sql_result"]["answer"] == "two rows"
    assert out["analyst"] is not None
    assert out["analyst"].anomalies[0].label == "L"
    assert out["hop_count"] == 1
    assert sql_graph.ainvoke.call_count == 1
    # Analyst cost folded into sql_result.cost
    assert out["sql_result"]["cost"]["llm_calls"] >= 2


async def test_supervisor_chitchat_skips_analyst(patch_analyst_llm) -> None:
    sql_graph = _fake_sql_graph([
        {
            "intent": "chitchat",
            "answer": "hi",
            "sql_result": None,
            "turn_index": 1,
        },
    ])
    patch_analyst_llm(_analyst_response())  # never called

    supervisor = build_supervisor_graph(sql_graph)
    out = await supervisor.ainvoke(
        {"question": "hi", "conversation_id": "t1", "hop_count": 0, "drill_downs": []},
    )
    # Analyst left untouched
    assert out.get("analyst") is None


async def test_supervisor_drill_down_recurses_once(patch_analyst_llm) -> None:
    """Analyst asks for a drill-down → Specialist runs again →
    Analyst on the deeper turn returns no drill_down → END."""
    sql_graph = _fake_sql_graph([
        # First invocation: top-level answer
        {
            "sql_result": [{"country": "DE", "n": 11}, {"country": "FR", "n": 7}],
            "row_count": 2,
            "chart_kind": "bar",
            "intent": "data",
            "answer": "Germany leads",
            "sql": "SELECT country, count(*) FROM customers GROUP BY country",
            "turn_index": 1,
            "cost": {"llm_calls": 1},
        },
        # Second invocation: the drill-down result
        {
            "sql_result": [{"name": "Alice"}, {"name": "Bob"}],
            "row_count": 2,
            "chart_kind": "table",
            "intent": "data",
            "answer": "two German customers",
            "sql": "SELECT name FROM customers WHERE country='Germany'",
            "turn_index": 1,
            "cost": {"llm_calls": 1},
        },
    ])
    patch_analyst_llm(
        _analyst_response(drill=True),    # first analyst run requests drill-down
        _analyst_response(drill=False),   # second run says we're done
    )

    supervisor = build_supervisor_graph(sql_graph)
    out = await supervisor.ainvoke(
        {"question": "who has the most?", "conversation_id": "t1",
         "hop_count": 0, "drill_downs": []},
    )

    assert sql_graph.ainvoke.call_count == 2
    assert out["hop_count"] == 2
    # The first (parent) SQL state landed in drill_downs
    assert len(out["drill_downs"]) == 1
    assert out["drill_downs"][0]["answer"] == "Germany leads"
    # The final visible answer is the drill-down's
    assert out["sql_result"]["answer"] == "two German customers"


async def test_supervisor_drill_down_capped_at_max_hops(patch_analyst_llm) -> None:
    """Even if the Analyst keeps requesting drill-downs, the supervisor
    refuses past ``MAX_HOP_COUNT``."""
    sql_graph = _fake_sql_graph([
        {
            "sql_result": [{"x": 1}, {"x": 2}],
            "row_count": 2, "chart_kind": "bar", "intent": "data",
            "answer": "a", "sql": "s", "turn_index": 1,
        },
        {
            "sql_result": [{"x": 3}, {"x": 4}],
            "row_count": 2, "chart_kind": "bar", "intent": "data",
            "answer": "b", "sql": "s", "turn_index": 1,
        },
    ])
    # Analyst asks for drill-down on every call (greedy)
    patch_analyst_llm(_analyst_response(drill=True), _analyst_response(drill=True))

    supervisor = build_supervisor_graph(sql_graph)
    out = await supervisor.ainvoke(
        {"question": "q", "conversation_id": "t1", "hop_count": 0, "drill_downs": []},
    )

    # Max 2 Specialist invocations regardless of how many drill-downs
    # the Analyst asks for.
    assert sql_graph.ainvoke.call_count == MAX_HOP_COUNT
    assert out["hop_count"] == MAX_HOP_COUNT


async def test_supervisor_paused_at_hitl_does_not_invoke_analyst(patch_analyst_llm) -> None:
    """HITL pause should propagate cleanly without triggering Analyst."""
    from langgraph.types import Interrupt

    paused = {
        "sql": "SELECT * FROM big LIMIT 100",
        "turn_index": 1,
        "__interrupt__": (
            Interrupt(
                value={
                    "sql": "SELECT * FROM big LIMIT 100",
                    "total_cost": 9999.0,
                    "threshold": 1000.0,
                    "reason": "too costly",
                },
                id="i1",
            ),
        ),
    }
    sql_graph = _fake_sql_graph([paused])
    patch_analyst_llm(_analyst_response())  # never called

    supervisor = build_supervisor_graph(sql_graph)
    out = await supervisor.ainvoke(
        {"question": "show all", "conversation_id": "t1",
         "hop_count": 0, "drill_downs": []},
    )

    assert out.get("analyst") is None
    assert out["sql_result"].get("__interrupt__")


async def test_supervisor_disabled_flag_skips_analyst(
    monkeypatch: pytest.MonkeyPatch, patch_analyst_llm
) -> None:
    monkeypatch.setattr(feature_flags, "ANALYST_ENABLED", False)
    sql_graph = _fake_sql_graph([
        {
            "sql_result": [{"x": 1}, {"x": 2}],
            "row_count": 2, "chart_kind": "bar", "intent": "data",
            "answer": "a", "sql": "s", "turn_index": 1,
        },
    ])
    calls = patch_analyst_llm(_analyst_response())

    supervisor = build_supervisor_graph(sql_graph)
    out = await supervisor.ainvoke(
        {"question": "q", "conversation_id": "t1", "hop_count": 0, "drill_downs": []},
    )

    assert out.get("analyst") is None
    assert len(calls) == 0  # analyst LLM never invoked


async def test_supervisor_analyst_llm_failure_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LLM exception inside the Analyst node degrades to
    ``analyst=None``; the SQL answer still ships."""
    sql_graph = _fake_sql_graph([
        {
            "sql_result": [{"x": 1}, {"x": 2}],
            "row_count": 2, "chart_kind": "bar", "intent": "data",
            "answer": "a", "sql": "s", "turn_index": 1,
        },
    ])

    class _BrokenLLM:
        def invoke(self, _msgs: Any) -> Any:
            raise RuntimeError("provider blew up")

    monkeypatch.setattr("copilot.agents.analyst.nodes.get_llm", lambda **_k: _BrokenLLM())

    supervisor = build_supervisor_graph(sql_graph)
    out = await supervisor.ainvoke(
        {"question": "q", "conversation_id": "t1", "hop_count": 0, "drill_downs": []},
    )

    assert out.get("analyst") is None
    assert out["sql_result"]["answer"] == "a"
