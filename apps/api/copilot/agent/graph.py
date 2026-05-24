"""LangGraph wiring — the week-7 HITL-capable text-to-SQL agent.

Layered on top of week 5's multi-turn graph, week 7 adds the
human-in-the-loop confirmation pair:

* ``check_risk``           — runs after ``validate_sql``. Calls Postgres
                              ``EXPLAIN`` on the validated SQL and writes
                              a ``pending_risk`` payload to state when
                              the planner cost exceeds the configured
                              threshold.
* ``await_confirmation``   — pauses the graph via LangGraph's
                              ``interrupt()`` primitive. Resumes on the
                              caller's next ``Command(resume=...)`` and
                              writes ``risk_decision`` (approved /
                              rejected) to state.

Persistence is wired through ``compile(checkpointer=...)``: each
``ainvoke`` is keyed by ``thread_id == conversation_id`` so the
agent automatically loads the prior state and saves the diff after
every node, including across an interrupt-resume gap.

Graph (week 7)::

         reset_per_turn
                |
         classify_intent
              /        \\
        chitchat        data
            |             |
       small_talk    retrieve_schema
            |             |
            |        generate_sql <----+
            |             |            |retry
            |        validate_sql -----+
            |             |            |
            |        check_risk        |
            |          /     \\        |
            |   await_confirm  \\      |
            |       |  \\       \\     |
            |   approved rejected      |
            |       |       \\         |
            |        \\       \\        |
            |        execute_sql ------+
            |             |
            |       summarize_result
            |             |
            |        finalize_error
            |             |
            +------+------+
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
from copilot.agent.dialogue import append_to_dialogue_node, reset_per_turn_node
from copilot.agent.retriever import retrieve_schema_node
from copilot.agent.risk import (
    await_confirmation_node,
    check_risk_node,
    route_after_confirmation,
    route_after_risk,
)
from copilot.agent.state import AgentState


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
    workflow.add_node("retrieve_schema", retrieve_schema_node)
    workflow.add_node("generate_sql", nodes.generate_sql_node)
    workflow.add_node("validate_sql", nodes.validate_sql_node)
    workflow.add_node("check_risk", check_risk_node)
    workflow.add_node("await_confirmation", await_confirmation_node)
    workflow.add_node("execute_sql", nodes.execute_sql_node)
    workflow.add_node("summarize_result", nodes.summarize_result_node)
    workflow.add_node("finalize_error", nodes.finalize_error_node)
    workflow.add_node("append_to_dialogue", append_to_dialogue_node)
    workflow.add_node("compact_history", compact_history_node)

    # ---- edges ----
    workflow.add_edge(START, "reset_per_turn")
    workflow.add_edge("reset_per_turn", "classify_intent")

    # Intent fan-out: chitchat short-circuits past SQL generation.
    workflow.add_conditional_edges(
        "classify_intent",
        nodes.route_after_classify,
        {
            "small_talk": "small_talk",
            "generate_sql": "retrieve_schema",
        },
    )

    workflow.add_edge("retrieve_schema", "generate_sql")
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

    workflow.add_conditional_edges(
        "execute_sql",
        nodes.route_after_execute,
        {
            "summarize_result": "summarize_result",
            "generate_sql": "generate_sql",
            "finalize_error": "finalize_error",
        },
    )

    # All three terminal "answer-ready" nodes funnel into the
    # bookkeeping pair (append_to_dialogue, compact_history) before END,
    # so dialogue is updated regardless of which branch produced the
    # answer.
    workflow.add_edge("small_talk", "append_to_dialogue")
    workflow.add_edge("summarize_result", "append_to_dialogue")
    workflow.add_edge("finalize_error", "append_to_dialogue")
    workflow.add_edge("append_to_dialogue", "compact_history")
    workflow.add_edge("compact_history", END)

    if checkpointer is not None:
        return workflow.compile(checkpointer=checkpointer)
    return workflow.compile()
