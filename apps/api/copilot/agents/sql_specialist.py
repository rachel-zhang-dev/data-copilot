"""SQL Specialist wrapper.

Adapts the existing 12-node LangGraph (``copilot.agent.build_graph``)
to look like a single node from the Supervisor's perspective. Three
responsibilities:

1. Translate the supervisor's input fields (``question`` / ``resume``)
   into the shape the Specialist expects (``{"question": ...}`` or
   ``Command(resume=...)``).
2. Invoke the Specialist's compiled graph **with the same
   conversation thread_id** the supervisor was called with, so
   multi-turn dialogue and the PostgresSaver checkpoint behave
   identically to pre-12.5.
3. Hand the Specialist's final ``AgentState`` dict back to the
   supervisor as ``sql_result`` and bump ``hop_count``.

The Specialist's own state shape stays unchanged — this wrapper is
the *only* code that knows both shapes.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from copilot.agents.state import SupervisorState

log = logging.getLogger(__name__)


def make_sql_specialist_node(
    sql_graph: CompiledStateGraph[Any, Any, Any, Any],
) -> Any:
    """Return an async LangGraph node bound to ``sql_graph``.

    Closing over the compiled Specialist (rather than building it on
    every call) keeps the per-turn overhead at one ``ainvoke`` plus
    one ``aget_state``. The supervisor graph is built once at startup
    and reused for the lifetime of the process.
    """

    async def sql_specialist_node(state: SupervisorState) -> dict[str, Any]:
        question = state.get("question")
        resume = state.get("resume")
        conversation_id = state.get("conversation_id") or ""

        # Same thread_id as the supervisor's own invocation so the
        # PostgresSaver checkpoint history is shared with the
        # Specialist's existing dialogue / attempts persistence.
        config: RunnableConfig = {"configurable": {"thread_id": conversation_id}}

        # The supervisor builds a fresh ``question`` on a recursive
        # drill-down — the Specialist gets the sharper sub-question
        # but the same thread_id, so dialogue context carries through.
        # ``payload`` is intentionally ``object`` so the union of the
        # Command + dict branches doesn't confuse LangGraph's overloads.
        payload: object
        if resume is not None:
            payload = Command(resume=resume)
        else:
            payload = {"question": question}

        log.info(
            "sql_specialist: invoking (hop=%d, resume=%s, q=%s)",
            state.get("hop_count", 0) + 1,
            resume,
            (question or "")[:60],
        )
        # ``ainvoke`` returns the merged state including the
        # ``__interrupt__`` marker on a paused turn — the same shape
        # ``main._build_ask_response`` already consumes.
        result = await sql_graph.ainvoke(payload, config=config)

        return {
            "sql_result": dict(result),
            # We always bump hop_count by exactly one per Specialist
            # invocation. The supervisor's routing decisions read this
            # to enforce the drill-down budget.
            "hop_count": state.get("hop_count", 0) + 1,
            # Subsequent loops are NEVER resume-flavoured (we resume
            # once on user input; a drill-down is a fresh question).
            "resume": None,
        }

    return cast(Any, sql_specialist_node)
