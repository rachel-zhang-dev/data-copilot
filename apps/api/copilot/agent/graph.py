"""LangGraph wiring — the week-3 multi-node text-to-SQL agent with
schema-aware retrieval.

Read this file alongside ``nodes.py`` and ``retriever.py`` (what each
node does) and ``state.py`` (what flows between them). This file is
purely about the *shape* of the graph; all real work is inside node
functions.

Graph::

                  +----------------+
                  | classify_intent|
                  +-------+--------+
                          |
              chitchat ---+--- data
              |                 |
              v                 v
        +-----------+    +-------------------+
        | small_talk|    | retrieve_schema   |  <-- new in week 3
        +-----+-----+    +-------+-----------+
              |                  |
              |                  v
              |          +---------------+
              |          |  generate_sql |
              |          +-------+-------+
              |                  |
              |                  v
              |          +---------------+
              |          |  validate_sql |
              |          +-------+-------+
              |        invalid|     |valid
              |               v     v
              |     +-----------+ +----------+
              |     | finalize_ | |execute_  |
              |     | error     | |  sql     |
              |     +-----+-----+ +----+-----+
              |           ^           |
              |           | db error  |
              |           +-----------+
              |           |  ok
              |           v
              |     +---------------+
              |     | summarize_    |
              |     |   result      |
              |     +-------+-------+
              |             |
              +------+------+
                     v
                    END

Week 4 will close the loop by sending ``finalize_error`` back to
``generate_sql`` for self-healing rather than terminating.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from copilot.agent import nodes
from copilot.agent.retriever import retrieve_schema_node
from copilot.agent.state import AgentState


def build_graph() -> CompiledStateGraph[AgentState, Any, AgentState, AgentState]:
    """Compile the agent graph.

    Pattern:
      1. ``StateGraph(<schema>)``
      2. ``add_node`` for each step
      3. ``add_edge`` / ``add_conditional_edges`` to wire them
      4. ``compile()`` returns the runnable, immutable graph

    Building once at startup is materially cheaper than rebuilding per
    request, which is why ``main.lifespan`` stashes the result on
    ``app.state``.
    """
    workflow: StateGraph[AgentState, Any, AgentState, AgentState] = StateGraph(AgentState)

    workflow.add_node("classify_intent", nodes.classify_intent_node)
    workflow.add_node("small_talk", nodes.small_talk_node)
    workflow.add_node("retrieve_schema", retrieve_schema_node)
    workflow.add_node("generate_sql", nodes.generate_sql_node)
    workflow.add_node("validate_sql", nodes.validate_sql_node)
    workflow.add_node("execute_sql", nodes.execute_sql_node)
    workflow.add_node("summarize_result", nodes.summarize_result_node)
    workflow.add_node("finalize_error", nodes.finalize_error_node)

    workflow.add_edge(START, "classify_intent")

    # Intent fan-out: chitchat short-circuits to a friendly reply; data
    # questions first go through the schema retriever, then SQL gen.
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

    # After validation: either jump straight to the error sink or proceed
    # to execution.
    workflow.add_conditional_edges(
        "validate_sql",
        nodes.route_after_validate,
        {
            "finalize_error": "finalize_error",
            "execute_sql": "execute_sql",
        },
    )

    # Same pattern after execution — DB errors get rerouted.
    workflow.add_conditional_edges(
        "execute_sql",
        nodes.route_after_execute,
        {
            "finalize_error": "finalize_error",
            "summarize_result": "summarize_result",
        },
    )

    workflow.add_edge("small_talk", END)
    workflow.add_edge("summarize_result", END)
    workflow.add_edge("finalize_error", END)

    return workflow.compile()
