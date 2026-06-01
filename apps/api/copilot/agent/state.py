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
                  Field is declared for LangChain compatibility but our
                  own nodes deliberately do NOT write to it; every LLM
                  call is captured by LangSmith as a child run, and the
                  user-facing transcript lives in ``dialogue``.
                  Appending here on every node call was an uncapped
                  leak (state is persisted via the checkpointer, so the
                  list — and Postgres row size — grew linearly in turn
                  count). See ADR 0005 §"Why we stopped appending to
                  messages".
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

from copilot.cost import CostBreakdown, add_cost

Intent = Literal["data", "chitchat", "schema_explore", "investigate"]
"""Top-level intent. Decided by ``classify_intent_node`` and used by
``route_after_classify`` to branch the graph.

* ``data``           — write SQL and answer with rows.
* ``chitchat``       — friendly canned reply, no DB touch.
* ``schema_explore`` — Phase 1.1 / ADR 0016. The user is asking
                       "what data do you have?" / "show me the tables".
                       Routes to ``explore_schema_node`` which renders
                       the cached ``schema_profiles`` as a grouped
                       tour.
* ``investigate``    — Phase 1.3 / ADR 0018. The user is asking an
                       open-ended research question ("why is X
                       declining" / "investigate the Q3 drop" / "deep
                       dive into ..."). The graph runs the same data
                       path as ``data`` but the supervisor raises the
                       drill-down budget so the analyst can chain
                       multiple follow-ups before answering."""


CoverageVerdict = Literal["ok", "refuse"]
"""Output of the Phase 1.1 ``coverage_check_node``.

``ok`` lets the existing data path proceed to ``generate_sql``;
``refuse`` diverts to ``explain_uncovered_node`` so the user gets
a friendly "this DB doesn't have X, you might want to ask Y"
response instead of a hallucinated SQL."""


ErrorClass = Literal[
    "unsafe_sql",
    "execution_failed",
    "fatal",
    "user_rejected",
    "critic_rejected",
]
"""Categorisation of a node failure. Used by ``can_retry`` to decide
whether the agent loops back to ``generate_sql`` or terminates with a
user-facing error.

``user_rejected`` (week 7) is the verdict when the user declined the
human-in-the-loop confirmation prompt. It is terminal — there is
nothing the agent can retry against a user saying "no".

``critic_rejected`` (Phase 2.3 / ADR 0021) is the verdict when the
critic node decides the SQL ran cleanly but answers a DIFFERENT
question than the user asked. The retry prompt for this class is
distinct from the execution-failure prompt because the SQL didn't
"fail" — it was semantically wrong, and the LLM needs different
guidance to fix it (see ``prompts.CRITIC_FIX_SYSTEM``)."""


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

    # Declared for LangChain ecosystem compatibility — our own nodes do
    # not write here (see module docstring for why). The reducer stays
    # in place so an external tool node or downstream caller could still
    # append safely without changing the type.
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
    """Whether the question needs SQL (``data``), a friendly reply
    (``chitchat``), or a schema tour (``schema_explore``, Phase 1.1).
    Set by ``classify_intent_node``. Reset at the start of each turn."""

    # ---------- Phase 1.1: coverage gate (turn-local) ----------
    coverage: dict[str, Any]
    """Verdict from ``coverage_check_node`` on whether the retrieved
    schema can actually answer the user's question.

    Shape::

        {
            "verdict": "ok" | "refuse",
            "reason": str,                  # one-sentence why
            "missing_concepts": [str, ...], # things the user asked
                                            # about that we couldn't
                                            # map to schema
            "suggested_questions": [str, ...],
        }

    ``None`` when the gate didn't run (chitchat / schema_explore
    branches) or when fail-open kicked in. See ADR 0016."""

    # ---------- Intermediate (turn-local; reset per turn) ----------
    relevant_schema: str
    """Pruned database schema fed to the LLM (set by the schema retriever)."""

    relevant_tables: list[str]
    """Ordered list of table names the retriever selected (Phase 1.1).
    Same content as ``relevant_schema`` but as a structured list — used
    by ``coverage_check_node`` to look up profiles without re-parsing
    the DDL string. ``None`` outside the data branch."""

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

    # ---------- Human-in-the-loop (turn-local; reset per turn) ----------
    pending_risk: dict[str, Any]
    """Diagnostic payload populated by ``check_risk_node`` when the
    planner cost crosses ``risk_explain_cost_threshold``. Shape:
    ``{"sql": str, "total_cost": float, "threshold": float, "reason": str}``.
    Surfaced to the caller while the graph is paused at
    ``await_confirmation``; cleared at the next ``reset_per_turn``."""

    risk_decision: Literal["approved", "rejected"]
    """Set by ``await_confirmation_node`` from the value the caller
    passed to ``Command(resume=...)``. Used by ``route_after_confirmation``
    to fan out to ``execute_sql`` or ``finalize_error``."""

    # ---------- Outputs (read by the caller) ----------
    answer: str
    """Final natural-language answer presented to the user.

    Since week 8 this is sourced from ``insight.headline`` when the
    structured insight parse succeeds, falling back to the raw LLM
    text otherwise. Either way, ``answer`` is always populated after
    a successful data turn so every existing caller keeps working."""

    # ---------- Outputs added in week 8 ----------
    insight: dict[str, Any]
    """Structured ``Insight`` envelope produced by
    ``summarize_result_node``: ``{headline, bullets, metric_highlights}``.
    ``None`` when the JSON parse fell back to the legacy NL-only path
    (chitchat / error branches also leave this unset)."""

    chart_kind: Literal["kpi", "bar", "line", "grouped_bar", "table"]
    """Heuristic classification of the result shape, set by
    ``visualize_node``. ``None`` outside the data success path."""

    chart_spec: dict[str, Any]
    """Vega-Lite v5 specification, set by ``visualize_node`` for
    ``bar`` / ``line`` / ``grouped_bar`` results. ``None`` for
    ``kpi`` / ``table`` (the UI renders those directly from
    ``sql_result``) and outside the data success path."""

    # ---------- Outputs added in Phase 2.3 (ADR 0021) ----------
    critic: dict[str, Any]
    """Verdict from ``critique_sql_node`` on whether the executed SQL
    actually answers the user's question.

    Shape::

        {
            "verdict": "ok" | "suspicious" | "wrong",
            "reason": str,           # one-sentence why
            "concerns": [str, ...],  # 0-3 specific issues
        }

    ``ok``         → silent pass-through.
    ``suspicious`` → FE renders a "⚠ low confidence" badge alongside
                     the answer (the answer itself still shows).
    ``wrong``      → routed back to ``generate_sql`` via
                     ``record_critic_rejection_node``; the rewritten
                     SQL gets its own critic pass. After one retry the
                     verdict is downgraded to ``suspicious`` and the
                     answer is shown rather than blocked.

    ``None`` outside the data success path (chitchat / refused /
    explore / execution-failed turns never reach the critic)."""

    # ---------- Outputs added in Phase 1.2 (ADR 0017) ----------
    patterns: list[dict[str, Any]]
    """Statistical findings produced by ``detect_patterns_node``:

        [
            {
                "kind": "outlier" | "trend",
                "column": str,
                "severity": "info" | "notable" | "high",
                "description_key": str,
                "payload": {...detector-specific evidence...}
            },
            ...
        ]

    Empty list / unset when the result set was too small or contained
    no numeric columns to detect on. The same findings drive the
    pattern bullets that get prepended to ``insight.bullets`` so the
    front-end's existing ``InsightPanel`` surfaces them without new
    UI; ``patterns`` is the structured form callers can use for
    future chart annotations or filtering."""

    # ---------- Outputs added in week 9 ----------
    cost: Annotated[CostBreakdown, add_cost]
    """Cumulative cost breakdown for the conversation (LLM / embedding
    / DB call counts plus token + USD estimates). The reducer field-
    wise sums successive node increments — across self-heal retries,
    HITL resumes, and follow-up turns — so the value is monotonically
    non-decreasing. Callers wanting just *this turn's* contribution
    can diff against the prior turn's checkpointed cost; the CLI /
    eval grader both opt to show cumulative because that's the number
    operators care about ("how much has this conversation cost me?")."""
