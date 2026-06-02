"""SQL verification loop / critic (Phase 2.3 / ADR 0021).

The agent's first six layers of defence (coverage gate, schema-aware
retrieval, static safety, risk gate, self-healing, Postgres) all
catch *syntactic* failures — SQL that fails to parse, fails to
execute, or runs an expensive plan. None of them catch *semantic*
failures: SQL that runs fine and returns plausible numbers, but
answers a different question than the user asked (wrong JOIN
direction, missing filter, wrong aggregation grain, …).

This module adds the seventh layer: a critic node that runs AFTER
``execute_sql_node`` succeeds. The critic is a separate LLM call
that sees question + schema + SQL + first N rows and produces a
structured verdict:

* ``ok``         — silently pass through to ``summarize_result``.
* ``suspicious`` — pass through but the verdict envelope lands on
                   ``state.critic`` so the FE can render a
                   "⚠ low confidence" badge.
* ``wrong``      — treat as a "semantic" failure: append an Attempt
                   with ``error_class="critic_rejected"`` and route
                   back to ``generate_sql``. The retry prompt is a
                   variant tuned for "fix what the reviewer said",
                   not "fix what the database said".

Fail-soft policy
----------------
Like the coverage gate, the critic fails OPEN. If the LLM call
errors, the JSON is unparsable, or the feature flag is off, the
node returns ``{"critic": {"verdict": "ok", ...}}`` and the graph
proceeds. A broken critic must never block a legitimate answer.

Budget
------
``RETRY_BUDGET["critic_rejected"] = 1`` — one critic-driven retry
per turn. Combined with the ``HARD_RETRY_CEILING`` of 5 in
``nodes.py``, the worst-case path is initial + 2 exec-retries +
1 critic-retry = 4 SQL-generation calls per turn. Bounded.

Why a separate node rather than folding it into summarize_result
-----------------------------------------------------------------
Two reasons:

1. The retry edge needs a distinct graph node so LangGraph can
   re-route back to ``generate_sql``. Summarize-and-then-retry is
   not expressible as a single node.
2. The eval harness flips an A/B on the critic via
   ``feature_flags.CRITIC_ENABLED``; keeping the node distinct
   lets the harness skip it cleanly without touching summarize.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError, field_validator

from copilot.agent.prompts import CRITIC_SYSTEM, CRITIC_USER_TEMPLATE
from copilot.agent.state import AgentState, Attempt
from copilot.cost import CostBreakdown
from copilot.llm import get_llm

log = logging.getLogger(__name__)


CriticVerdictLabel = Literal["ok", "suspicious", "wrong"]


# Cap the number of rows the critic ever sees to keep the prompt cheap
# and to avoid leaking PII into the LLM context. 5 rows is plenty for a
# human reviewer to sanity-check shape; the critic gets the TOTAL row
# count separately so it doesn't undercount.
_PREVIEW_ROW_COUNT = 5

# Per-field caps mirror the insight envelope (insight.py) so a runaway
# LLM cannot bloat ``critic`` past sensible limits in the response.
_MAX_REASON_CHARS = 240
_MAX_CONCERN_CHARS = 200
_MAX_CONCERNS = 4


class CriticVerdict(BaseModel):
    """Structured verdict produced by the critic LLM.

    The shape mirrors the simpler half of the coverage envelope: a
    single verdict label, a one-sentence reason, and a small list of
    specific concerns. ``ok`` verdicts get an empty concerns list by
    convention but we don't enforce it (the LLM occasionally returns
    "ok with notes" which we accept and ignore on render)."""

    verdict: CriticVerdictLabel
    reason: str = Field("", max_length=_MAX_REASON_CHARS)
    concerns: list[str] = Field(default_factory=list, max_length=_MAX_CONCERNS)

    @field_validator("concerns")
    @classmethod
    def _cap_concern_length(cls, v: list[str]) -> list[str]:
        return [
            c if len(c) <= _MAX_CONCERN_CHARS else c[: _MAX_CONCERN_CHARS - 3] + "..."
            for c in v
        ]


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)


def _strip_fence(text_: str) -> str:
    return _FENCE_RE.sub("", text_).strip()


def parse_critic_verdict(raw: str) -> CriticVerdict | None:
    """Best-effort parse of an LLM reply into a ``CriticVerdict``.

    Returns ``None`` on any parse or schema error; callers treat that
    as fail-open (verdict ok)."""
    if not raw or not raw.strip():
        return None
    cleaned = _strip_fence(raw)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning("critic JSON parse failed: %s", exc)
        return None
    try:
        return CriticVerdict.model_validate(payload)
    except ValidationError as exc:
        log.warning("critic schema validation failed: %s", exc)
        return None


def _preview_rows(rows: list[dict[str, Any]] | None, limit: int) -> str:
    """Compact JSON preview, identical formatting to the summarizer's
    ``_preview_rows`` so the LLM sees one consistent style across
    prompts in the same pipeline."""
    return json.dumps(
        (rows or [])[:limit], default=str, ensure_ascii=False, indent=2
    )


def _ok_verdict(reason: str) -> dict[str, Any]:
    """Construct the fail-open envelope used when the critic can't
    actually produce a verdict (flag off, LLM down, parse failed)."""
    return {
        "verdict": "ok",
        "reason": reason,
        "concerns": [],
    }


def _llm_cost(response: Any, prompt_text: str) -> CostBreakdown:
    """Local copy of ``nodes._llm_cost`` to keep the critic module
    free of a back-import from nodes (which would create a cycle once
    nodes imports critic for the retry branch)."""
    # Lazy import so the module-load order stays:
    #   prompts → critic → nodes → graph
    from copilot.agent.nodes import _llm_cost as _nodes_llm_cost

    return _nodes_llm_cost(response, prompt_text)


def critique_sql_node(state: AgentState) -> dict[str, Any]:
    """Decide whether the SQL we just executed actually answers the
    user's question.

    Always returns at least ``{"critic": ...}``. On any failure the
    critic record carries ``verdict="ok"`` so the downstream router
    behaves as if the gate hadn't run.

    The node does NOT write to ``error`` or ``attempts`` directly —
    that's the router's job (``route_after_critic`` below). Keeping
    side effects out of the node body makes the unit test trivial
    (assert verdict comes through) and lets the routing decision
    live next to the other routers."""
    # Lazy flag import: production never reads ``feature_flags`` outside
    # an active eval ``override`` block, and pulling at module top would
    # couple the agent runtime to the eval-only module on every import.
    from copilot.agent import feature_flags

    if not feature_flags.CRITIC_ENABLED:
        log.info("critique_sql: disabled (feature flag) → ok")
        return {"critic": _ok_verdict("critic disabled")}

    sql = state.get("sql")
    if not sql:
        log.info("critique_sql: no SQL on state → ok (nothing to critique)")
        return {"critic": _ok_verdict("no SQL produced")}

    schema = state.get("relevant_schema") or ""
    question = state.get("question") or ""
    rows = state.get("sql_result") or []
    row_count = state.get("row_count")
    if row_count is None:
        row_count = len(rows)

    user_msg = CRITIC_USER_TEMPLATE.format(
        schema=schema,
        question=question,
        sql=sql,
        preview_count=min(_PREVIEW_ROW_COUNT, row_count),
        row_count=row_count,
        rows_preview=_preview_rows(rows, _PREVIEW_ROW_COUNT),
    )

    try:
        llm = get_llm(
            temperature=0.0,
            model_kwargs={"response_format": {"type": "json_object"}},
        )
        response = llm.invoke(
            [
                SystemMessage(content=CRITIC_SYSTEM),
                HumanMessage(content=user_msg),
            ]
        )
    except Exception as exc:
        log.warning("critique_sql: LLM call failed (%s); fail-open", exc)
        return {"critic": _ok_verdict("critic LLM unavailable")}

    # Pull the text + cost up-front so even an unparsable verdict still
    # logs the spend. Same posture as ``coverage_check_node``.
    raw = (response.content if isinstance(response.content, str)
           else "".join(str(p) for p in response.content)).strip()
    cost = _llm_cost(response, user_msg)
    parsed = parse_critic_verdict(raw)

    if parsed is None:
        log.warning("critique_sql: unparsable reply → fail-open. raw=%r", raw[:300])
        return {"critic": _ok_verdict("critic verdict unparsable"), "cost": cost}

    log.info(
        "critique_sql: verdict=%s reason=%r concerns=%s",
        parsed.verdict,
        parsed.reason[:80],
        parsed.concerns,
    )
    return {"critic": parsed.model_dump(), "cost": cost}


def route_after_critic(state: AgentState) -> str:
    """Pick the next node after the critic ran.

    Routing rules:
      * verdict ``ok`` / ``suspicious``  → ``summarize_result``
        (the FE renders the suspicious badge from ``state.critic``)
      * verdict ``wrong`` + retry budget → append a synthetic Attempt
        (so ``can_retry`` filters and prompts can see it) and loop
        back to ``generate_sql``.
      * verdict ``wrong`` + no budget    → ``summarize_result``
        (don't block the user; the suspicious badge stays attached).

    Side-effect (Attempt insertion) lives here on purpose:
    ``critique_sql_node`` returns one merge-dict cleanly without ever
    touching the error / attempts surface; the router is where the
    "the critic rejected → become a failed attempt" transformation
    happens. Mirrors how ``route_after_validate`` and
    ``route_after_execute`` handle the same conversion."""
    # Lazy import to avoid the prompts → critic → nodes → critic
    # cycle that would arise if we hoisted these to the top.

    critic = state.get("critic") or {}
    verdict = critic.get("verdict", "ok")

    if verdict != "wrong":
        return "summarize_result"

    turn_idx = state.get("turn_index", 1)
    # Note: we can't append to state.attempts from a router (LangGraph
    # routers are pure functions); the Attempt is recorded by the
    # critic node ITSELF when it sees verdict=wrong on the next pass
    # is not feasible either. Instead, we handle this via a small
    # helper: the *next* call into route_after_critic-then-generate
    # path needs the Attempt on state. We synthesise it here by
    # piggy-backing on the standard self-healing convention: the
    # critic node already wrote ``critic`` to state; the
    # ``generate_sql_node`` retry branch consults BOTH ``attempts``
    # and ``critic`` and uses the critic path when the latest
    # attempt is critic-flavoured.
    #
    # For routing purposes, we just decide whether to loop based on
    # whether the LAST attempt this turn (if any) was already a
    # critic_rejected one — if so, we've used our critic retry
    # budget and must move on.
    attempts = state.get("attempts") or []
    this_turn = [a for a in attempts if a.get("turn_idx", 0) == turn_idx]
    already_critic_retried = any(
        a.get("error_class") == "critic_rejected" for a in this_turn
    )
    if already_critic_retried:
        log.info(
            "route_after_critic: verdict=wrong but critic retry already used → summarize",
        )
        return "summarize_result"

    # Has retry budget — loop back. The Attempt is appended below by
    # the dedicated ``record_critic_rejection_node`` that sits between
    # critique_sql and generate_sql on the retry edge.
    log.info("route_after_critic: verdict=wrong → record + retry")
    return "record_critic_rejection"


def record_critic_rejection_node(state: AgentState) -> dict[str, Any]:
    """Bookkeeping node that converts a ``wrong`` critic verdict into a
    proper ``Attempt`` record + sets ``error`` so the standard retry
    machinery in ``generate_sql_node`` picks it up.

    Sits on the retry edge ``critique_sql → record_critic_rejection
    → generate_sql``. Kept tiny on purpose — pure state-shaping, no
    LLM call. We could have folded this into ``critique_sql_node`` but
    keeping it on the retry edge means the node's only job stays
    "produce a verdict"; the conversion is opt-in via the router.
    """
    critic = state.get("critic") or {}
    sql = state.get("sql") or ""
    reason = critic.get("reason") or "critic rejected the SQL as semantically wrong"
    concerns = critic.get("concerns") or []
    detail = reason
    if concerns:
        detail = f"{reason} | concerns: {'; '.join(concerns)}"

    return {
        "error": f"critic_rejected: {detail}",
        "attempts": [
            Attempt(
                sql=sql,
                error=detail,
                error_class="critic_rejected",
                turn_idx=state.get("turn_index", 1),
            )
        ],
    }
