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
    RETRY_SQL_SYSTEM,
    RETRY_SQL_USER_TEMPLATE,
    SMALL_TALK_SYSTEM,
    SUMMARIZE_SYSTEM,
    SUMMARIZE_USER_TEMPLATE,
)
from copilot.agent.sql_safety import SqlSafetyError, strip_fence, validate_and_rewrite
from copilot.agent.state import AgentState, Attempt, ErrorClass, Intent
from copilot.config import get_settings
from copilot.db import get_schema_ddl, run_select
from copilot.llm import get_llm

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Self-healing policy
# ---------------------------------------------------------------------------


RETRY_BUDGET: dict[ErrorClass, int] = {
    "execution_failed": 2,
    "unsafe_sql": 1,
    "fatal": 0,
}
"""How many retries each error class is allowed.

Budget is the *number of retries* on top of the initial attempt:
``execution_failed=2`` means up to 3 total LLM calls. See
``docs/decisions/0004-self-healing-policy.md`` for the rationale.
"""


# Hard ceiling on the total number of LLM calls per request, regardless
# of what someone might set RETRY_BUDGET to via configuration. Defends
# the agent against accidental loop blowups.
HARD_RETRY_CEILING = 5


def classify_error(error: str) -> ErrorClass:
    """Map an ``error`` string to its retry class.

    The prefixes match what ``validate_sql_node`` and
    ``execute_sql_node`` produce, so the mapping is purely string-based
    and does not need to introspect exceptions.
    """
    if error.startswith("unsafe_sql:"):
        return "unsafe_sql"
    if error.startswith("execution_failed:"):
        return "execution_failed"
    return "fatal"


def can_retry(attempts: list[Attempt]) -> bool:
    """Return True if we should loop back to ``generate_sql``.

    Decision rule:
      * No prior attempts => no retry decision to make (caller is past
        the first failure).
      * Look up the budget for the LATEST failure's class.
      * Allow retry while ``len(attempts) <= budget``. Equality is
        included because budget is the *count of retries on top of
        the initial attempt*, and the next loop will increase the
        attempts list to ``budget + 1`` in worst case.
      * Also enforce ``HARD_RETRY_CEILING`` so a misconfigured budget
        cannot cause runaway loops.
    """
    if not attempts:
        return False
    if len(attempts) >= HARD_RETRY_CEILING:
        return False
    last_class = attempts[-1]["error_class"]
    return len(attempts) <= RETRY_BUDGET.get(last_class, 0)


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

    On the first call this looks like the week-2 behaviour. On a retry
    (i.e. ``state["attempts"]`` is non-empty), we switch to the
    self-healing prompt that includes the previously-failed SQL plus
    the error message, so the LLM can produce a *correction*.

    We pull the schema lazily so unit tests can patch ``get_schema_ddl``
    without standing up Postgres.
    """
    schema = state.get("relevant_schema") or get_schema_ddl()
    attempts = state.get("attempts", [])

    if attempts:
        last = attempts[-1]
        sys_msg = RETRY_SQL_SYSTEM
        user_msg = RETRY_SQL_USER_TEMPLATE.format(
            schema=schema,
            question=state["question"],
            last_sql=last["sql"],
            last_error=last["error"],
            attempt_no_prev=len(attempts),
            attempt_no=len(attempts) + 1,
        )
        log.info(
            "generate_sql RETRY #%d (last_class=%s)",
            len(attempts) + 1,
            last["error_class"],
        )
    else:
        sys_msg = GENERATE_SQL_SYSTEM
        user_msg = GENERATE_SQL_USER_TEMPLATE.format(schema=schema, question=state["question"])

    llm = get_llm(temperature=0.0)
    response = llm.invoke(
        [
            SystemMessage(content=sys_msg),
            HumanMessage(content=user_msg),
        ]
    )
    sql = strip_fence(_message_text(response).strip())
    log.info("generate_sql -> %s", sql.replace("\n", " ")[:200])

    # CRITICAL: clear ``error`` so the routers downstream see a fresh
    # attempt, not the stale failure that triggered this retry.
    return {
        "sql": sql,
        "relevant_schema": schema,
        "error": None,
        "messages": [response],
    }


def validate_sql_node(state: AgentState) -> dict[str, Any]:
    """Run the safety policy. On failure, record ``error`` and append
    a record to ``attempts`` so the router can decide whether to retry
    and so the next ``generate_sql`` call can see what went wrong."""
    settings = get_settings()
    try:
        rewritten = validate_and_rewrite(state["sql"], max_rows=settings.sql_max_rows)
    except SqlSafetyError as exc:
        log.warning("validate_sql rejected: %s", exc)
        return {
            "error": f"unsafe_sql: {exc}",
            "attempts": [
                Attempt(
                    sql=state["sql"],
                    error=str(exc),
                    error_class="unsafe_sql",
                )
            ],
        }
    return {"sql": rewritten}


def execute_sql_node(state: AgentState) -> dict[str, Any]:
    """Execute the (already-validated) SQL and stash rows + count.

    On a database error, append to ``attempts`` exactly as
    ``validate_sql_node`` does for safety failures, so the retry loop
    has the same shape regardless of which step blew up.
    """
    try:
        rows = run_select(state["sql"])
    except Exception as exc:
        log.exception("execute_sql failed")
        return {
            "error": f"execution_failed: {exc}",
            "attempts": [
                Attempt(
                    sql=state["sql"],
                    error=str(exc),
                    error_class="execution_failed",
                )
            ],
        }
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
    """Produce a user-facing message when retry budget is exhausted.

    No LLM call — the failure modes here are predictable enough that a
    deterministic template avoids both cost and the risk of the LLM
    hallucinating an answer in a failure case.

    The user-facing message includes the attempt count when there was
    more than one, so it is honest about what happened ("I tried 3
    times and still got it wrong") rather than implying a single
    point-of-failure.
    """
    err = state.get("error") or "unknown_error"
    n = len(state.get("attempts", []))
    suffix = f" (after {n} attempts)" if n > 1 else ""

    if err.startswith("unsafe_sql:"):
        reason = err.removeprefix("unsafe_sql:").strip()
        msg = (
            "I cannot run that query because it does not look like a safe "
            f"read-only SELECT{suffix}. ({reason}) Try rephrasing it as a "
            "question about the data, for example: 'How many orders shipped "
            "last month?'"
        )
    elif err.startswith("execution_failed:"):
        reason = err.removeprefix("execution_failed:").strip()
        msg = (
            f"I generated SQL but the database returned an error{suffix} "
            f"({reason}). The schema may have changed, or the question may "
            "need to be more specific."
        )
    else:
        msg = f"Sorry, I could not answer that{suffix}. Reason: {err}"
    return {"answer": msg}


# ---------------------------------------------------------------------------
# Routing functions
# ---------------------------------------------------------------------------


def route_after_classify(state: AgentState) -> str:
    """Pick the next node based on the classifier's verdict."""
    return "small_talk" if state.get("intent") == "chitchat" else "generate_sql"


def route_after_validate(state: AgentState) -> str:
    """Pick the next node after the safety check.

    Three outcomes:
      * No error          -> proceed to ``execute_sql``.
      * Error + can retry -> loop back to ``generate_sql``.
      * Error + budget exhausted -> ``finalize_error``.
    """
    if not state.get("error"):
        return "execute_sql"
    if can_retry(state.get("attempts", [])):
        return "generate_sql"
    return "finalize_error"


def route_after_execute(state: AgentState) -> str:
    """Same shape as ``route_after_validate`` but after DB execution."""
    if not state.get("error"):
        return "summarize_result"
    if can_retry(state.get("attempts", [])):
        return "generate_sql"
    return "finalize_error"
