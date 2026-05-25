"""Supervisor graph (week 12.5).

A thin LangGraph state machine that wires the SQL Specialist (the
existing week-12 graph) and the Analyst together with deterministic
(rule-based) routing.

Topology::

      START
        │
        ▼
   sql_specialist  ←──────────────┐
        │                          │ drill-down loop
        ▼                          │ (hop_count ≤ 2)
   route_after_sql                 │
   ├── END (chitchat / pause /     │
   │       error / row_count <= 1) │
   └── analyst                     │
        │                          │
        ▼                          │
   route_after_analyst             │
   ├── END                         │
   └── sql_specialist  ────────────┘

The supervisor compiles WITHOUT a checkpointer — its own state is
ephemeral per request. Multi-turn dialogue still lives in the SQL
Specialist's PostgresSaver (week 5) which the wrapper passes through
unchanged.

See ADR 0014 for the full rationale (why rule-based, why hop_count<=2,
why supervisor compiles standalone).
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from copilot.agents.analyst import analyst_node
from copilot.agents.sql_specialist import make_sql_specialist_node
from copilot.agents.state import SupervisorState

log = logging.getLogger(__name__)


# Max number of SQL Specialist invocations per user turn — i.e. the
# initial answer + one optional drill-down. Hard-coded rather than a
# setting because it is the topology, not a tuning knob.
MAX_HOP_COUNT = 2


def route_after_sql(state: SupervisorState) -> str:
    """Decide whether the Analyst should weigh in on this SQL answer.

    Skip the Analyst when:

    * The user is in chitchat — there are no rows to analyse.
    * The Specialist paused for HITL confirmation — the user has not
      yet approved the query so analysing it would be premature.
    * The Specialist errored out — analysing a failure adds no value
      and risks confusing the user.
    * The result was a one-row KPI with no chart — usually the answer
      is the headline number and follow-ups would be noise.
    * ``ANALYST_ENABLED`` is off (eval baseline / operator disable).

    All other cases forward to the Analyst.
    """
    # Lazy import so the production import graph does not pick up
    # ``feature_flags`` outside an eval override.
    from copilot.agent import feature_flags

    if not (state.get("analyst_enabled", feature_flags.ANALYST_ENABLED)):
        log.info("supervisor.route_after_sql: analyst disabled → END")
        return END

    sql_state = state.get("sql_result") or {}

    if "__interrupt__" in sql_state:
        # Paused at the HITL gate; defer Analyst until resume.
        log.info("supervisor.route_after_sql: paused at HITL → END")
        return END

    if sql_state.get("intent") == "chitchat":
        log.info("supervisor.route_after_sql: chitchat → END")
        return END

    if sql_state.get("error"):
        log.info("supervisor.route_after_sql: error path → END")
        return END

    rows = sql_state.get("sql_result")
    if not isinstance(rows, list) or len(rows) == 0:
        log.info("supervisor.route_after_sql: empty result → END")
        return END

    if sql_state.get("chart_kind") == "kpi" and len(rows) <= 1:
        log.info("supervisor.route_after_sql: single-row KPI → END")
        return END

    return "analyst"


def route_after_analyst(state: SupervisorState) -> str:
    """Decide whether to recursively invoke the Specialist.

    The Analyst MAY have produced a ``drill_down`` request — we
    honour it iff ``hop_count`` is still under the budget. The check
    is belt-and-suspenders with the Analyst's own ``hop_count >= 1``
    self-restraint; either layer can refuse alone.
    """
    if state.get("hop_count", 0) >= MAX_HOP_COUNT:
        log.info("supervisor.route_after_analyst: hop budget exhausted → END")
        return END

    analyst = state.get("analyst")
    drill = getattr(analyst, "drill_down", None) if analyst is not None else None
    if drill is None:
        return END

    log.info("supervisor.route_after_analyst: drill-down requested → sql_specialist")
    return "sql_specialist"


def _carry_drill_down(state: SupervisorState) -> dict[str, Any]:
    """Bridge node: take ``analyst.drill_down.question`` and stage it
    as the next Specialist invocation's input.

    Also records the parent's ``sql_result`` into ``drill_downs`` so
    the API response can group parent + child for the UI.
    """
    analyst = state.get("analyst")
    drill = getattr(analyst, "drill_down", None) if analyst is not None else None
    if drill is None:
        return {}

    # The PARENT's sql_result becomes a historical drill-down entry;
    # the NEW Specialist invocation will write a fresh sql_result.
    parent = state.get("sql_result") or {}

    return {
        "question": drill.question,
        "resume": None,
        "drill_downs": [parent],
    }


def build_supervisor_graph(
    sql_graph: CompiledStateGraph[Any, Any, Any, Any],
) -> CompiledStateGraph[SupervisorState, Any, SupervisorState, SupervisorState]:
    """Compile the multi-agent graph on top of ``sql_graph``.

    The supervisor itself is stateless per request — it compiles
    without a checkpointer. The Specialist's checkpointer (passed in
    via ``sql_graph``) handles all dialogue persistence.
    """
    workflow: StateGraph[SupervisorState, Any, SupervisorState, SupervisorState] = StateGraph(
        SupervisorState
    )

    workflow.add_node("sql_specialist", make_sql_specialist_node(sql_graph))
    workflow.add_node("analyst", analyst_node)
    workflow.add_node("prepare_drill_down", _carry_drill_down)

    workflow.add_edge(START, "sql_specialist")

    workflow.add_conditional_edges(
        "sql_specialist",
        route_after_sql,
        {
            "analyst": "analyst",
            END: END,
        },
    )

    workflow.add_conditional_edges(
        "analyst",
        route_after_analyst,
        {
            "sql_specialist": "prepare_drill_down",
            END: END,
        },
    )

    # After staging the drill-down, loop back to the Specialist.
    workflow.add_edge("prepare_drill_down", "sql_specialist")

    return workflow.compile()
