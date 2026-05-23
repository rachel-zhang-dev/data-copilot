"""Agent state — the single source of truth that flows through the graph.

LangGraph executes a *graph of nodes*. Each node is a Python function
that receives the current state, optionally mutates it, and returns the
fields it changed. LangGraph then merges those changes back into the
state and routes execution to the next node.

Because state is a TypedDict, IDEs and mypy can verify that every field
read or written is spelled correctly.

Reducer summary
---------------
* ``messages``  — ``add_messages`` (LangChain): smart append + dedupe.
* ``attempts``  — ``operator.add``: plain append. ``turn_idx`` on each
                  Attempt lets ``can_retry`` ignore failures from
                  earlier turns (week 5).
* ``dialogue``  — ``replace_or_append``: appends by default, but
                  ``compact_history_node`` returns a sentinel dict
                  ``{"replace": [...]}`` to overwrite the whole list
                  with the post-compaction view.
* anything else — default replace semantics.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langgraph.graph.message import add_messages

Intent = Literal["data", "chitchat"]
"""Top-level intent. Decided by ``classify_intent_node`` and used by
``route_after_classify`` to branch the graph."""


ErrorClass = Literal["unsafe_sql", "execution_failed", "fatal"]
"""Categorisation of a node failure. Used by ``can_retry`` to decide
whether the agent loops back to ``generate_sql`` or terminates with a
user-facing error."""


class Attempt(TypedDict):
    """One pass through ``generate_sql -> validate_sql -> execute_sql``.

    Each failed attempt appends a record to ``state.attempts`` so the
    next ``generate_sql`` call can see what was tried and why it
    failed, and so routers can count failures by class.

    ``turn_idx`` (week 5) tags each attempt with the conversation turn
    it belongs to. Without it, retry budgets would leak across turns
    and a follow-up question could find itself "already at limit"
    because of failures recorded in the previous turn.
    """

    sql: str
    error: str
    error_class: ErrorClass
    turn_idx: int


class Turn(TypedDict):
    """One user-facing message pair in the conversation.

    The list of ``Turn`` objects is the canonical "what the user has
    been talking about" view, distinct from ``messages`` which holds
    every internal LLM exchange (intent classification, retry
    rewrites, etc.).
    """

    role: Literal["user", "assistant"]
    content: str
    # Optional analytics on assistant turns; omitted on user turns.
    sql: NotRequired[str]
    row_count: NotRequired[int]


def replace_or_append(left: list[Turn], right: list[Turn] | dict[str, list[Turn]]) -> list[Turn]:
    """Custom reducer for ``dialogue``.

    Default behaviour is append (so normal nodes can push a single new
    turn). When ``compact_history_node`` needs to *replace* the entire
    list (because old turns have been summarised), it returns a dict
    of the form ``{"replace": new_list}`` and this reducer honours it.

    The unusual return-type protocol is the cheapest workaround for
    LangGraph's lack of a native "replace this field" semantics on
    fields that otherwise want appending behaviour.
    """
    if isinstance(right, dict):
        if "replace" in right:
            return list(right["replace"])
        # Defensive: any other dict shape we ignore — should not happen
        return list(left)
    return left + list(right)


class AgentState(TypedDict, total=False):
    """State carried through the agent graph.

    ``total=False`` means every key is optional; nodes populate fields
    incrementally as the agent makes progress.
    """

    # Conversation history. The ``add_messages`` reducer concatenates
    # any messages a node returns onto whatever was there before.
    messages: Annotated[list[Any], add_messages]

    # ---------- Inputs (set by the caller) ----------
    question: str
    """The original natural-language question from the user."""

    # ---------- Conversation-level fields (persist across turns) ----------
    dialogue: Annotated[list[Turn], replace_or_append]
    """User-facing conversation history. Appended one Turn at a time
    by ``append_to_dialogue_node`` after each successful or failed
    turn; replaced wholesale by ``compact_history_node`` when the
    cumulative size crosses the threshold."""

    turn_index: int
    """1-based index of the current turn within the conversation.
    Set by ``reset_per_turn_node`` based on ``len(dialogue) // 2 + 1``.
    Tagged onto each ``Attempt`` so retry counting is turn-local."""

    # ---------- Routing (turn-local) ----------
    intent: Intent
    """Whether the question needs SQL (``data``) or just a friendly
    reply (``chitchat``). Set by ``classify_intent_node``. Reset at
    the start of each turn."""

    # ---------- Intermediate (turn-local; reset per turn) ----------
    relevant_schema: str
    """Pruned database schema fed to the LLM (set by the schema retriever)."""

    sql: str
    """SQL query generated by the LLM."""

    sql_result: list[dict[str, Any]]
    """Rows returned by executing the SQL."""

    row_count: int
    """Number of rows returned by ``execute_sql_node``."""

    error: str
    """Error message of the LATEST failure. ``generate_sql`` clears it
    on retry, so routers can rely on a non-empty value to mean
    'something went wrong in the run that just finished'."""

    attempts: Annotated[list[Attempt], operator.add]
    """Append-only history of failed attempts. Each Attempt is tagged
    with ``turn_idx`` so ``can_retry`` only counts failures from the
    current turn."""

    retry_count: int
    """Legacy field from week 2; superseded by ``attempts``. Kept for
    backwards compatibility with any external observer that read it."""

    # ---------- Outputs (read by the caller) ----------
    answer: str
    """Final natural-language answer presented to the user."""
