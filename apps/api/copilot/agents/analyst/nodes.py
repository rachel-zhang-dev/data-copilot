"""Analyst node — the second worker in the supervisor + worker pattern.

One LangGraph node (``analyst_node``) plus a private JSON parser.
Inputs come from the supervisor as a typed ``AnalystRequest``;
outputs go back into ``SupervisorState.analyst`` as an
``AnalystResponse`` (or ``None`` on graceful failure).

Failure handling is fail-soft on every axis: a missing API key,
malformed JSON, schema-violating fields, or an over-long response
all degrade to ``analyst=None`` so the parent SQL answer still
ships to the user.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from copilot.agents.analyst.prompts import ANALYST_SYSTEM, ANALYST_USER_TEMPLATE
from copilot.agents.messages import AnalystRequest, AnalystResponse
from copilot.agents.state import SupervisorState
from copilot.config import get_settings
from copilot.cost import (
    CostBreakdown,
    estimate_tokens_from_chars,
    llm_call_cost,
    usage_from_response,
)
from copilot.llm import get_llm

log = logging.getLogger(__name__)

# Hard cap on how many rows we hand to the Analyst's prompt. Large
# result sets get truncated; the Analyst sees ``row_count`` separately
# so it doesn't undercount.
_MAX_PROMPT_ROWS = 20

# How many recent dialogue turns the Analyst sees. Enough to spot
# "we just looked at that two turns ago" without bloating the prompt.
_MAX_DIALOGUE_CONTEXT = 6

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _strip_fence(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def parse_response(raw: str) -> AnalystResponse | None:
    """Best-effort parse of one LLM reply into an ``AnalystResponse``.

    Returns ``None`` on any parse / validation failure. Exported for
    tests so the parser can be exercised without standing up a real
    LLM call.
    """
    if not raw or not raw.strip():
        return None
    cleaned = _strip_fence(raw)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning("analyst: JSON parse failed (%s)", exc)
        return None
    if not isinstance(payload, dict):
        log.warning("analyst: top-level JSON is not an object")
        return None
    try:
        return AnalystResponse.model_validate(payload)
    except ValidationError as exc:
        log.warning("analyst: schema validation failed (%s)", exc)
        return None


# ---------------------------------------------------------------------------
# Prompt-building helpers
# ---------------------------------------------------------------------------


def _rows_preview(rows: list[dict[str, Any]]) -> str:
    """Compact JSON preview, capped at ``_MAX_PROMPT_ROWS``."""
    return json.dumps(rows[:_MAX_PROMPT_ROWS], default=str, ensure_ascii=False, indent=2)


def _dialogue_context(dialogue: list[dict[str, Any]]) -> str:
    """Render the most recent dialogue turns as plain text.

    Mirrors the Specialist's ``format_dialogue_for_prompt`` shape so
    the model sees the same conventions across both agents.
    """
    recent = dialogue[-_MAX_DIALOGUE_CONTEXT:]
    if not recent:
        return "(none)"
    lines: list[str] = []
    for turn in recent:
        prefix = "User:" if turn.get("role") == "user" else "Assistant:"
        content = str(turn.get("content", "")).strip()
        lines.append(f"{prefix} {content}")
    return "\n".join(lines)


def _drill_eligibility(hop_count: int, hop_budget: int, intent: str | None) -> str:
    """One-sentence prompt cue about whether drill-down is allowed.

    ``hop_count`` is the number of Specialist invocations that already
    completed before the Analyst's prompt was built; ``hop_budget``
    is the maximum allowed (Phase 1.3 lets it be > 2 in
    ``investigate`` mode).

    * hop_count < hop_budget → drill_down allowed.
    * hop_count >= hop_budget → must refuse further.

    Phase 1.3.1 — investigate-mode wording moves from soft "SHOULD"
    to imperative "MUST emit … unless stopping criteria are met"
    because the soft form let DeepSeek-class analysts terminate after
    a single hop on multi-part research questions (observed in eval
    + manual smoke). The matching stopping criteria live in the
    SYSTEM prompt under "INVESTIGATE-MODE STOPPING CRITERION".
    """
    if hop_count >= hop_budget:
        return (
            "You MUST set drill_down to null on this turn "
            f"(hop budget of {hop_budget} has been exhausted)."
        )
    remaining = hop_budget - hop_count
    if intent == "investigate":
        return (
            f"You MUST emit a drill_down (or stop ONLY if the system "
            f"prompt's stopping criteria are met). {remaining} hop(s) "
            "remain — make each one count."
        )
    return (
        "You MAY emit a single drill_down request if the rows hint at "
        f"a sharper question. {remaining} hop(s) remain."
    )


def _llm_cost(response: AIMessage, prompt_text: str) -> CostBreakdown:
    """Pull token usage from ``response_metadata`` when present;
    otherwise fall back to the same ``chars/4`` heuristic the SQL
    Specialist uses for its own nodes."""
    model = get_settings().deepseek_model
    usage = usage_from_response(response)
    if usage is not None:
        tokens_in, tokens_out = usage
    else:
        from copilot.agent.nodes import _message_text

        tokens_in = estimate_tokens_from_chars(prompt_text)
        tokens_out = estimate_tokens_from_chars(_message_text(response))
    return llm_call_cost(model, tokens_in=tokens_in, tokens_out=tokens_out)


# ---------------------------------------------------------------------------
# The node itself
# ---------------------------------------------------------------------------


def _build_request(state: SupervisorState) -> AnalystRequest | None:
    """Adapter: project ``SupervisorState`` to an ``AnalystRequest``.

    Returns ``None`` when there's nothing for the Analyst to look at
    (no SQL was run, no rows, chitchat / error path).
    """
    # Lazy import to avoid module-import cycle (supervisor → analyst).
    from copilot.agents.supervisor import _hop_budget_for

    sql_state = state.get("sql_result") or {}
    rows = sql_state.get("sql_result")
    if not isinstance(rows, list) or not rows:
        return None
    if sql_state.get("intent") == "chitchat":
        return None
    if sql_state.get("error"):
        return None

    # Phase 1.3 — assemble the drill-down history so the Analyst can
    # see what's already been asked. The list starts with the user's
    # original question (the FIRST entry in ``drill_downs`` if any,
    # otherwise the current sql_state's question) and chains every
    # past Specialist invocation's question in invocation order.
    past_specialist_states: list[dict[str, Any]] = list(
        state.get("drill_downs") or []
    )
    drill_history: list[str] = [
        str(s.get("question"))
        for s in past_specialist_states
        if s.get("question")
    ]

    return AnalystRequest(
        question=sql_state.get("question", state.get("question") or ""),
        sql=sql_state.get("sql"),
        answer=sql_state.get("answer", ""),
        rows=list(rows),
        row_count=sql_state.get("row_count"),
        chart_kind=sql_state.get("chart_kind"),
        chart_spec=sql_state.get("chart_spec"),
        dialogue_recent=list(sql_state.get("dialogue") or []),
        hop_count=state.get("hop_count", 1),
        intent=sql_state.get("intent"),
        hop_budget=_hop_budget_for(state),
        drill_history=drill_history,
    )


def _bump_cost(sql_state: dict[str, Any], increment: CostBreakdown) -> dict[str, Any]:
    """Return ``sql_state`` with ``cost`` field-wise summed.

    The Specialist has already populated ``cost`` with the cumulative
    figure for the SQL path. After the Specialist's graph finishes
    its reducer is no longer active — the Analyst manually adds its
    own LLM-call cost so ``AskResponse.cost`` reflects every spent
    cent of this turn.
    """
    from copilot.cost import add_cost

    return {**sql_state, "cost": add_cost(sql_state.get("cost"), increment)}


def analyst_node(state: SupervisorState) -> dict[str, Any]:
    """LangGraph node: produce an ``AnalystResponse`` for this turn.

    Fail-soft contract: every failure path returns
    ``{"analyst": None}`` so the parent SQL answer still ships.
    """
    from copilot.agent.nodes import _message_text

    request = _build_request(state)
    if request is None:
        log.info("analyst: nothing to analyse (chitchat / no rows / error)")
        return {"analyst": None}

    drill_history_text = (
        "\n".join(f"  {i+1}. {q}" for i, q in enumerate(request.drill_history))
        if request.drill_history
        else "  (none — this is the first hop)"
    )
    user_msg = ANALYST_USER_TEMPLATE.format(
        question=request.question,
        sql=request.sql or "(none)",
        row_count=request.row_count if request.row_count is not None else len(request.rows),
        rows_preview=_rows_preview(request.rows),
        answer=request.answer,
        dialogue_context=_dialogue_context(request.dialogue_recent),
        hop_count=request.hop_count,
        hop_budget=request.hop_budget,
        intent=request.intent or "data",
        drill_history=drill_history_text,
        drill_down_eligibility=_drill_eligibility(
            request.hop_count, request.hop_budget, request.intent
        ),
    )

    # JSON mode keeps the model honest; the ``parse_response`` fallback
    # is defence in depth for providers that don't honour it.
    llm = get_llm(
        temperature=0.3,
        model_kwargs={"response_format": {"type": "json_object"}},
    )
    try:
        response = llm.invoke(
            [SystemMessage(content=ANALYST_SYSTEM), HumanMessage(content=user_msg)]
        )
    except Exception as exc:
        log.warning("analyst: LLM call failed (%s); skipping", exc)
        return {"analyst": None}

    raw = _message_text(response).strip()
    parsed = parse_response(raw)
    cost = _llm_cost(response, user_msg)

    if parsed is None:
        log.warning("analyst: response parse failed; skipping")
        return {
            "analyst": None,
            "sql_result": _bump_cost(state.get("sql_result", {}), cost),
        }

    # Belt-and-suspenders: ``hop_count`` reflects how many SQL
    # Specialist runs have already completed. Phase 1.3 made the
    # budget intent-aware (2 for plain ``data``, 6 for
    # ``investigate``), so the cap moved from a hardcoded "2" to
    # whatever the supervisor told us via ``hop_budget``. Same
    # belt-and-suspenders principle: supervisor's
    # ``route_after_analyst`` would refuse anyway, but scrubbing it
    # here too keeps the response payload honest.
    if request.hop_count >= request.hop_budget and parsed.drill_down is not None:
        log.info(
            "analyst: discarding drill_down because hop_count=%d budget=%d",
            request.hop_count,
            request.hop_budget,
        )
        parsed = parsed.model_copy(update={"drill_down": None})

    log.info(
        "analyst: %d anomalies, %d followups, drill=%s",
        len(parsed.anomalies),
        len(parsed.followups),
        "yes" if parsed.drill_down else "no",
    )
    return {
        "analyst": parsed,
        "sql_result": _bump_cost(state.get("sql_result", {}), cost),
    }
