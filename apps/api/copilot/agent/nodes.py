"""LangGraph node implementations.

Each node is a small, pure function that:

* takes the current ``AgentState``,
* does one well-defined job (LLM call, SQL execution, etc.),
* returns a ``dict`` of state fields to merge.

Nodes never reach into each other; they communicate exclusively via the
state object. This is what lets us unit-test them in isolation, and
what lets LangGraph reorder / retry / parallelise them later.

The graph wiring lives in ``graph.py``. This file is "what each step
does"; that file is "how the steps connect".
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from copilot.agent.prompts import (
    CLASSIFY_INTENT_SYSTEM,
    GENERATE_SQL_SYSTEM,
    GENERATE_SQL_USER_TEMPLATE,
    SMALL_TALK_SYSTEM,
    SUMMARIZE_SYSTEM,
    SUMMARIZE_USER_TEMPLATE,
)
from copilot.agent.sql_safety import SqlSafetyError, validate_and_rewrite
from copilot.agent.state import AgentState, Intent
from copilot.config import get_settings
from copilot.db import get_schema_ddl, run_select
from copilot.llm import get_llm

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _message_text(msg: AIMessage) -> str:
    """LangChain's ``AIMessage.content`` can be a string OR a list of
    content blocks (multi-modal). For our text-only flow the str branch
    is the common case; we coerce the list case defensively."""
    content = msg.content
    if isinstance(content, str):
        return content
    return "".join(
        part.get("text", "") if isinstance(part, dict) else str(part) for part in content
    )


def _preview_rows(rows: list[dict[str, Any]], limit: int = 20) -> str:
    """Compact JSON preview of rows for the summarizer prompt.

    We cap at ``limit`` rows so a 100-row result does not blow up the
    summarizer's token budget. The summarizer is told the *real* row
    count separately so it does not undercount.
    """
    return json.dumps(rows[:limit], default=str, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Nodes — chitchat branch
# ---------------------------------------------------------------------------


def classify_intent_node(state: AgentState) -> dict[str, Any]:
    """Decide whether the user is asking a data question or just chatting.

    We use a tiny prompt with ``temperature=0`` and parse the first
    word of the reply. Anything we do not recognise falls back to
    ``data`` — better to attempt SQL than to refuse politely.
    """
    llm = get_llm(temperature=0.0, max_tokens=4)
    response = llm.invoke(
        [
            SystemMessage(content=CLASSIFY_INTENT_SYSTEM),
            HumanMessage(content=state["question"]),
        ]
    )
    raw = _message_text(response).strip().lower().split()
    intent: Intent = "data"
    if raw and raw[0] in {"data", "chitchat"}:
        intent = raw[0]  # type: ignore[assignment]
    log.info("classify_intent -> %s", intent)
    return {"intent": intent, "messages": [response]}


def small_talk_node(state: AgentState) -> dict[str, Any]:
    """Reply to greetings / 'what can you do' style questions.

    Uses a slightly higher temperature so the response does not feel
    canned across repeat invocations.
    """
    llm = get_llm(temperature=0.4)
    response = llm.invoke(
        [
            SystemMessage(content=SMALL_TALK_SYSTEM),
            HumanMessage(content=state["question"]),
        ]
    )
    answer = _message_text(response).strip()
    return {"answer": answer, "messages": [response]}


# ---------------------------------------------------------------------------
# Nodes — data branch
# ---------------------------------------------------------------------------


def generate_sql_node(state: AgentState) -> dict[str, Any]:
    """Ask the LLM to translate the question into a SELECT statement.

    We pull the schema lazily so unit tests can patch ``get_schema_ddl``
    without standing up Postgres.
    """
    schema = state.get("relevant_schema") or get_schema_ddl()
    user_msg = GENERATE_SQL_USER_TEMPLATE.format(schema=schema, question=state["question"])
    llm = get_llm(temperature=0.0)
    response = llm.invoke(
        [
            SystemMessage(content=GENERATE_SQL_SYSTEM),
            HumanMessage(content=user_msg),
        ]
    )
    sql = _message_text(response).strip()
    log.info("generate_sql -> %s", sql.replace("\n", " ")[:200])
    return {"sql": sql, "relevant_schema": schema, "messages": [response]}


def validate_sql_node(state: AgentState) -> dict[str, Any]:
    """Run the safety policy. On failure, record ``error`` for routing."""
    settings = get_settings()
    try:
        rewritten = validate_and_rewrite(state["sql"], max_rows=settings.sql_max_rows)
    except SqlSafetyError as exc:
        log.warning("validate_sql rejected: %s", exc)
        return {"error": f"unsafe_sql: {exc}"}
    return {"sql": rewritten}


def execute_sql_node(state: AgentState) -> dict[str, Any]:
    """Execute the (already-validated) SQL and stash rows + count."""
    try:
        rows = run_select(state["sql"])
    except Exception as exc:
        log.exception("execute_sql failed")
        return {"error": f"execution_failed: {exc}"}
    return {"sql_result": rows, "row_count": len(rows)}


def summarize_result_node(state: AgentState) -> dict[str, Any]:
    """Turn raw rows into a natural-language answer."""
    rows = state.get("sql_result", [])
    user_msg = SUMMARIZE_USER_TEMPLATE.format(
        question=state["question"],
        sql=state.get("sql", ""),
        row_count=state.get("row_count", len(rows)),
        rows_preview=_preview_rows(rows),
    )
    llm = get_llm(temperature=0.2)
    response = llm.invoke(
        [
            SystemMessage(content=SUMMARIZE_SYSTEM),
            HumanMessage(content=user_msg),
        ]
    )
    answer = _message_text(response).strip()
    return {"answer": answer, "messages": [response]}


def finalize_error_node(state: AgentState) -> dict[str, Any]:
    """Produce a user-facing message when an earlier node set ``error``.

    No LLM call — the failure modes here are predictable enough that a
    deterministic template avoids both cost and the risk of the LLM
    hallucinating an answer in a failure case.
    """
    err = state.get("error") or "unknown_error"
    if err.startswith("unsafe_sql:"):
        reason = err.removeprefix("unsafe_sql:").strip()
        msg = (
            "I cannot run that query because it does not look like a safe "
            f"read-only SELECT. ({reason}) Try rephrasing it as a question "
            "about the data, for example: 'How many orders shipped last month?'"
        )
    elif err.startswith("execution_failed:"):
        reason = err.removeprefix("execution_failed:").strip()
        msg = (
            "I generated SQL but the database returned an error while "
            f"executing it ({reason}). The schema may have changed, or the "
            "question may need to be more specific."
        )
    else:
        msg = f"Sorry, I could not answer that. Reason: {err}"
    return {"answer": msg}


# ---------------------------------------------------------------------------
# Routing functions
# ---------------------------------------------------------------------------


def route_after_classify(state: AgentState) -> str:
    """Pick the next node based on the classifier's verdict."""
    return "small_talk" if state.get("intent") == "chitchat" else "generate_sql"


def route_after_validate(state: AgentState) -> str:
    """If ``validate_sql_node`` set an error, jump to the error sink."""
    return "finalize_error" if state.get("error") else "execute_sql"


def route_after_execute(state: AgentState) -> str:
    """Same idea for execution failures."""
    return "finalize_error" if state.get("error") else "summarize_result"
