"""Minimal LangGraph agent — Hello World.

Right now the graph has a single node ("echo") that simply asks the LLM
to answer the user's question. Week 2 onwards we will replace this with
a real text-to-SQL pipeline:

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

Why LangGraph and not a plain Python function?
----------------------------------------------
A real agent needs to *branch*, *loop*, *retry*, and *pause for human
input*. Modelling that as nested ``if`` / ``while`` quickly becomes
unreadable. LangGraph encodes the flow as an explicit directed graph,
which is easier to reason about, easier to visualise, and supports
useful runtime features like checkpointing and time-travel debugging.
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
    """The single node in our hello-world graph.

    A LangGraph node is just a function that:
        * receives the current ``state`` (a TypedDict),
        * does some work (here: a single LLM call),
        * returns a dict of fields to merge back into the state.

    The keys we return MUST match field names declared in ``AgentState``.
    """
    # Build a fresh chat model. We keep this inside the node so each
    # invocation is independent — easier to test, easier to swap models
    # per-node later.
    llm = get_llm(temperature=0.3)

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=state["question"]),
    ]

    # ⭐ This call hits the DeepSeek API over the network. Everything
    # else in this file is just plumbing around this one line.
    response = llm.invoke(messages)

    return {
        # ``messages`` uses the ``add_messages`` reducer, so this list
        # gets *appended* to existing history rather than replacing it.
        "messages": [response],
        # ``answer`` does not have a reducer, so it is overwritten.
        "answer": response.content,
    }


def build_graph():
    """Compile the agent graph.

    Three steps for any LangGraph:
        1. Create a StateGraph parameterised by your state schema.
        2. ``add_node`` for each step in the workflow.
        3. ``add_edge`` to connect them, using the special ``START`` and
           ``END`` sentinels for entry / exit.

    ``compile()`` produces an immutable, runnable graph. Building it once
    at startup (see ``main.lifespan``) and reusing it across requests is
    much cheaper than rebuilding per request.
    """
    workflow = StateGraph(AgentState)

    # Register the node. The first argument is the *node name* used by
    # edges; the second is the function executed when control reaches it.
    workflow.add_node("echo", echo_node)

    # Wire up the flow:  START → echo → END
    workflow.add_edge(START, "echo")
    workflow.add_edge("echo", END)

    return workflow.compile()
