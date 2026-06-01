"""LangGraph wiring — the week-8 text-to-SQL agent.

Layered on top of week 7's HITL-capable graph, week 8 adds a
``visualize`` node that runs after ``summarize_result`` on every
successful data turn. It classifies the result shape into one of
five buckets (kpi / bar / line / grouped_bar / table) and emits a
Vega-Lite v5 spec for the three "real chart" kinds. See ADR 0009 for
the heuristic + fail-soft design.

Phase 1.1 (ADR 0016) added a three-way intent split and an inline
coverage gate:

* New ``schema_explore`` intent → ``explore_schema`` node, which
  renders the cached ``schema_profiles`` as a topic-grouped tour.
* The data branch now passes through ``coverage_check`` between
  ``retrieve_schema`` and ``generate_sql``. On ``refuse`` it diverts
  to ``explain_uncovered`` — the agent never writes SQL it can't
  justify against the schema.

Persistence is wired through ``compile(checkpointer=...)``: each
``ainvoke`` is keyed by ``thread_id == conversation_id`` so the
agent automatically loads the prior state and saves the diff after
every node, including across an interrupt-resume gap.

Graph (Phase 1.1)::

                 reset_per_turn
                       |
                 classify_intent
              /        |        \\
       chitchat   schema_explore   data
          |            |            |
    small_talk   explore_schema  retrieve_schema
          |            |            |
          |            |       coverage_check
          |            |        /         \\
          |            |   refuse           ok
          |            |     |              |
          |            |  explain_uncovered  generate_sql <----+
          |            |     |              |                 |retry
          |            |     |        validate_sql -----------+
          |            |     |              |                 |
          |            |     |          check_risk            |
          |            |     |           /      \\            |
          |            |     |  await_confirm    \\           |
          |            |     |     |   \\           \\        |
          |            |     | approved rejected               |
          |            |     |     |       \\                  |
          |            |     |     |        \\                  |
          |            |     |  execute_sql ------------------+
          |            |     |     |
          |            |     |  summarize_result
          |            |     |     |
          |            |     |  detect_patterns  (Phase 1.2)
          |            |     |     |
          |            |     |  visualize
          |            |     |     |
          |            |     |  finalize_error
          |            |     |     |
          +----+-------+-----+-----+
                       |
                append_to_dialogue
                       |
                compact_history
                       |
                      END
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from copilot.agent import nodes
from copilot.agent.compaction import compact_history_node
from copilot.agent.coverage import (
    coverage_check_node,
    explain_uncovered_node,
    route_after_coverage,
)
from copilot.agent.critic import (
    critique_sql_node,
    record_critic_rejection_node,
    route_after_critic,
)
from copilot.agent.dialogue import append_to_dialogue_node, reset_per_turn_node
from copilot.agent.explore import explore_schema_node
from copilot.agent.patterns.node import detect_patterns_node
from copilot.agent.retriever import retrieve_schema_node
from copilot.agent.risk import (
    await_confirmation_node,
    check_risk_node,
    route_after_confirmation,
    route_after_risk,
)
from copilot.agent.state import AgentState
from copilot.agent.visualize import visualize_node


def build_graph(
    *, checkpointer: Any | None = None
) -> CompiledStateGraph[AgentState, Any, AgentState, AgentState]:
    """Compile the agent graph.

    Args:
        checkpointer: optional LangGraph checkpoint saver. When
            provided, the compiled graph supports ``thread_id``-keyed
            multi-turn conversations. When ``None`` the graph is
            stateless across invocations (handy in unit tests).
    """
    workflow: StateGraph[AgentState, Any, AgentState, AgentState] = StateGraph(AgentState)

    # ---- nodes ----
    workflow.add_node("reset_per_turn", reset_per_turn_node)
    workflow.add_node("classify_intent", nodes.classify_intent_node)
    workflow.add_node("small_talk", nodes.small_talk_node)
    workflow.add_node("explore_schema", explore_schema_node)
    workflow.add_node("retrieve_schema", retrieve_schema_node)
    workflow.add_node("coverage_check", coverage_check_node)
    workflow.add_node("explain_uncovered", explain_uncovered_node)
    workflow.add_node("generate_sql", nodes.generate_sql_node)
    workflow.add_node("validate_sql", nodes.validate_sql_node)
    workflow.add_node("check_risk", check_risk_node)
    workflow.add_node("await_confirmation", await_confirmation_node)
    workflow.add_node("execute_sql", nodes.execute_sql_node)
    # Phase 2.3 / ADR 0021 — critic + the tiny bookkeeping node that
    # converts a "wrong" verdict into a proper Attempt before looping
    # back to generate_sql. Order matters at the wiring step below.
    workflow.add_node("critique_sql", critique_sql_node)
    workflow.add_node("record_critic_rejection", record_critic_rejection_node)
    workflow.add_node("summarize_result", nodes.summarize_result_node)
    workflow.add_node("detect_patterns", detect_patterns_node)
    workflow.add_node("visualize", visualize_node)
    workflow.add_node("finalize_error", nodes.finalize_error_node)
    workflow.add_node("append_to_dialogue", append_to_dialogue_node)
    workflow.add_node("compact_history", compact_history_node)

    # ---- edges ----
    workflow.add_edge(START, "reset_per_turn")
    workflow.add_edge("reset_per_turn", "classify_intent")

    # Intent fan-out (Phase 1.1): three branches. chitchat short-
    # circuits past SQL generation; schema_explore goes to a tour
    # node; data fires the retrieve → coverage_check pipeline.
    workflow.add_conditional_edges(
        "classify_intent",
        nodes.route_after_classify,
        {
            "small_talk": "small_talk",
            "explore_schema": "explore_schema",
            "generate_sql": "retrieve_schema",
        },
    )

    # Data branch: schema retrieval → coverage gate → SQL writing
    # (Phase 1.1 inserts coverage_check between the two; on refuse,
    # the graph diverts to explain_uncovered and skips SQL entirely).
    workflow.add_edge("retrieve_schema", "coverage_check")
    workflow.add_conditional_edges(
        "coverage_check",
        route_after_coverage,
        {
            "generate_sql": "generate_sql",
            "explain_uncovered": "explain_uncovered",
        },
    )
    workflow.add_edge("generate_sql", "validate_sql")

    # After validation: success -> check_risk (week 7 inserts a risk
    # gate between validate and execute), retryable error -> loop,
    # terminal error -> finalize.
    workflow.add_conditional_edges(
        "validate_sql",
        nodes.route_after_validate,
        {
            # NOTE: ``nodes.route_after_validate`` returns the string
            # ``"execute_sql"`` to mean "the SQL is valid; go run it".
            # Week 7 swaps the destination to ``check_risk`` so the
            # cost gate runs first; the router contract is unchanged.
            "execute_sql": "check_risk",
            "generate_sql": "generate_sql",
            "finalize_error": "finalize_error",
        },
    )

    # Week 7 — the human-in-the-loop pair. ``check_risk`` is cheap
    # (one EXPLAIN call) and routes around itself when the SQL is
    # below threshold; only the expensive branch reaches
    # ``await_confirmation`` and pauses the graph.
    workflow.add_conditional_edges(
        "check_risk",
        route_after_risk,
        {
            "execute_sql": "execute_sql",
            "await_confirmation": "await_confirmation",
        },
    )
    workflow.add_conditional_edges(
        "await_confirmation",
        route_after_confirmation,
        {
            "execute_sql": "execute_sql",
            "finalize_error": "finalize_error",
        },
    )

    # Phase 2.3 — on execute success, divert to the critic node before
    # summarizing. The router's contract is unchanged (it still returns
    # the string "summarize_result" to mean "the SQL is good"); we
    # remap that destination to "critique_sql" so the critic runs
    # first. This keeps ``nodes.route_after_execute`` decoupled from
    # the critic — turning the critic off via the feature flag is then
    # a no-op on the routing side; the critic node itself returns
    # verdict=ok and ``route_after_critic`` falls through to summarize.
    workflow.add_conditional_edges(
        "execute_sql",
        nodes.route_after_execute,
        {
            "summarize_result": "critique_sql",
            "generate_sql": "generate_sql",
            "finalize_error": "finalize_error",
        },
    )

    # Phase 2.3 — critic fan-out. Two destinations:
    #   * ``summarize_result``         — verdict ok / suspicious, OR
    #                                    wrong but no retry budget left.
    #   * ``record_critic_rejection`` — verdict wrong with retry budget;
    #                                    this tiny node converts the
    #                                    verdict into an Attempt record
    #                                    + error string and the edge
    #                                    below loops to generate_sql.
    workflow.add_conditional_edges(
        "critique_sql",
        route_after_critic,
        {
            "summarize_result": "summarize_result",
            "record_critic_rejection": "record_critic_rejection",
        },
    )
    workflow.add_edge("record_critic_rejection", "generate_sql")

    # Week 8 inserts ``visualize`` only on the data-success path —
    # chitchat / terminal-error / refused / explored branches don't
    # have rows to chart. ALL terminal branches funnel into the
    # bookkeeping pair (append_to_dialogue, compact_history) before
    # END, so dialogue is updated regardless of which branch produced
    # the answer (Phase 1.1 adds explore_schema + explain_uncovered).
    workflow.add_edge("small_talk", "append_to_dialogue")
    workflow.add_edge("explore_schema", "append_to_dialogue")
    workflow.add_edge("explain_uncovered", "append_to_dialogue")
    # Phase 1.2 (ADR 0017) — pattern detector runs between
    # ``summarize_result`` (which produces the legacy NL insight) and
    # ``visualize`` (which decides the chart kind). detector findings
    # are prepended to ``insight.bullets`` so the existing
    # InsightPanel surfaces them with no FE change; ``patterns``
    # carries the structured form for future chart annotations.
    workflow.add_edge("summarize_result", "detect_patterns")
    workflow.add_edge("detect_patterns", "visualize")
    workflow.add_edge("visualize", "append_to_dialogue")
    workflow.add_edge("finalize_error", "append_to_dialogue")
    workflow.add_edge("append_to_dialogue", "compact_history")
    workflow.add_edge("compact_history", END)

    if checkpointer is not None:
        return workflow.compile(checkpointer=checkpointer)
    return workflow.compile()
