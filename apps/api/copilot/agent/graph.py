"""Minimal LangGraph agent — Hello World.

This is intentionally tiny. Week 2 will replace it with the real
text-to-SQL pipeline:

    [understand_question]
            │
            ▼
    [retrieve_schema]      (RAG over table/column descriptions)
            │
            ▼
    [generate_sql]
            │
            ▼
    [validate_sql]   ─── error ──► [rewrite_sql] ──┐
            │                                       │
         valid                                      │
            │   ◄───────────────────────────────────┘
            ▼
    [execute_sql]
            │
            ▼
    [summarize_result]
            │
            ▼
          DONE
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from copilot.agent.state import AgentState
from copilot.llm import get_llm

SYSTEM_PROMPT = """You are Data Copilot, an enterprise data assistant.
For now you only echo the user's question with a friendly note. Real
SQL generation will be wired up in week 2."""


def echo_node(state: AgentState) -> dict:
    """Temporary single node — proves the LLM + LangGraph pipeline works."""
    llm = get_llm(temperature=0.3)
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=state["question"]),
    ]
    response = llm.invoke(messages)
    return {
        "messages": [response],
        "answer": response.content,
    }


def build_graph():
    """Compile the agent graph."""
    workflow = StateGraph(AgentState)
    workflow.add_node("echo", echo_node)
    workflow.add_edge(START, "echo")
    workflow.add_edge("echo", END)
    return workflow.compile()
