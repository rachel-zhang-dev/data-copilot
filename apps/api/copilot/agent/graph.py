"""LangGraph wiring — the week-5 multi-turn text-to-SQL agent.

Layered on top of week 4's self-healing graph, week 5 adds:

* ``reset_per_turn``       — first node every invocation, clears
                              turn-local fields so a follow-up is not
                              poisoned by the previous turn's state.
* ``append_to_dialogue``   — last node before END for every terminal
                              path (small_talk / summarize_result /
                              finalize_error). Records the turn into
                              the user-facing dialogue list.
* ``compact_history``      — runs after append_to_dialogue. When the
                              dialogue exceeds ``compaction_threshold_tokens``
                              it summarises older turns into one synthetic
                              entry; otherwise it is a no-op.

Persistence is wired through ``compile(checkpointer=...)``: each
``ainvoke`` is keyed by ``thread_id == conversation_id`` so the
agent automatically loads the prior state and saves the diff after
every node.

Graph (week 5)::

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

    # After validation: success -> execute, retryable error -> loop,
    # terminal error -> finalize.
    workflow.add_conditional_edges(
        "validate_sql",
        nodes.route_after_validate,
        {
            "execute_sql": "execute_sql",
            "generate_sql": "generate_sql",
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
