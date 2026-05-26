"""Dialogue lifecycle nodes (week 5).

Three small bookkeeping nodes that maintain the user-facing
conversation view across turns:

* ``reset_per_turn_node``     — runs at the start of every turn.
                                Clears turn-local fields so a
                                follow-up question does not inherit
                                the previous turn's SQL / attempts /
                                error / etc.
* ``append_to_dialogue_node`` — runs at the end of every turn.
                                Appends one ``user`` ``Turn`` (the
                                question) and one ``assistant``
                                ``Turn`` (the answer + SQL) to the
                                ``dialogue`` field.

The actual compaction logic lives in ``compaction.py``; keeping it
separate stops this module from depending on the LLM client.
"""

from __future__ import annotations

import logging
from typing import Any

from copilot.agent.state import AgentState, Turn

log = logging.getLogger(__name__)


def current_turn_index(state: AgentState) -> int:
    """Compute the 1-based turn index from the current ``dialogue``.

    Each completed turn writes exactly two entries (user + assistant).
    The next turn is therefore ``len(dialogue) // 2 + 1``.
    """
    dialogue = state.get("dialogue") or []
    return len(dialogue) // 2 + 1


def reset_per_turn_node(state: AgentState) -> dict[str, Any]:
    """Wipe turn-local fields before classify_intent runs.

    The fields that **persist** across turns are excluded:
      * ``messages``  — driven by LangGraph's add_messages reducer
      * ``dialogue``  — the user-facing transcript
      * ``attempts``  — kept for telemetry; ``can_retry`` filters by
                         ``turn_idx`` so old attempts are inert
      * ``question``  — set anew on each ``ainvoke``

    Everything else gets reset so the new turn starts on a clean slate.
    Note: returning ``None`` for a key sets the field to ``None``,
    which our routers and helpers treat as "absent" via ``state.get``.
    """
    return {
        "intent": None,
        "relevant_schema": None,
        "relevant_tables": None,
        "sql": None,
        "sql_result": None,
        "row_count": None,
        "error": None,
        "answer": None,
        # Week 7: clear the HITL pause + decision so a follow-up turn
        # never inherits "yes I already approved" from a previous turn.
        "pending_risk": None,
        "risk_decision": None,
        # Phase 1.1: clear the coverage gate verdict so a refused turn
        # doesn't bleed into the next turn's UI.
        "coverage": None,
        # Week 8: clear the structured insight + chart so a follow-up
        # turn never echoes the previous answer's visualisation.
        "insight": None,
        "chart_kind": None,
        "chart_spec": None,
        # Phase 1.2: clear pattern findings so a refused / no-data turn
        # doesn't inherit outliers from the previous query.
        "patterns": None,
        "turn_index": current_turn_index(state),
    }


def _user_turn(question: str) -> Turn:
    return {"role": "user", "content": question}


def _assistant_turn(state: AgentState) -> Turn:
    """Build the assistant turn from whatever the agent ended up with.

    Always populates ``content`` (the user-facing answer); attaches
    ``sql`` and ``row_count`` when the SQL pipeline ran (i.e. it was
    a data question, not chitchat).
    """
    turn: Turn = {
        "role": "assistant",
        "content": state.get("answer") or "",
    }
    sql = state.get("sql")
    if sql:
        turn["sql"] = sql
    row_count = state.get("row_count")
    if row_count is not None:
        turn["row_count"] = row_count
    return turn


def append_to_dialogue_node(state: AgentState) -> dict[str, Any]:
    """Append the (user_question, assistant_answer) pair to ``dialogue``.

    Runs after every terminal node (small_talk, summarize_result,
    finalize_error). The reducer is ``replace_or_append`` so a plain
    list return value is treated as append.
    """
    user = _user_turn(state["question"])
    assistant = _assistant_turn(state)
    return {"dialogue": [user, assistant]}


def format_dialogue_for_prompt(dialogue: list[Turn], *, max_turns: int = 6) -> str:
    """Return a compact textual rendering of the most recent turns.

    Used by ``generate_sql_node`` to give the LLM enough context to
    resolve references like "those" or "and how about France?".
    Limiting to ``max_turns`` keeps prompts small even on long
    conversations (compact_history_node enforces a hard ceiling
    too, so this is defensive belt-and-suspenders).
    """
    if not dialogue:
        return ""
    recent = dialogue[-max_turns:]
    lines = []
    for turn in recent:
        prefix = "User:" if turn["role"] == "user" else "Assistant:"
        content = turn["content"].strip().replace("\n", " ")
        if turn["role"] == "assistant" and "sql" in turn:
            content = f"{content}  [ran: {turn['sql']}]"
        lines.append(f"{prefix} {content}")
    return "\n".join(lines)
