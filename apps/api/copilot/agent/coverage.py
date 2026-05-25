"""Schema-coverage gate + uncovered-question handler (Phase 1.1 / ADR 0016).

Two LangGraph nodes, both fail-soft:

* ``coverage_check_node`` runs between ``retrieve_schema`` and
  ``generate_sql``. It asks the LLM whether the retrieved tables can
  plausibly answer the user's question. On ``verdict="ok"`` (the
  expected case) the graph proceeds to SQL generation; on
  ``verdict="refuse"`` the graph diverts to ``explain_uncovered_node``.

* ``explain_uncovered_node`` produces a friendly, structured refusal
  that admits the missing concept and suggests answerable alternatives.
  Output shape mirrors the existing ``insight`` envelope so the front-
  end can render it with a single component.

Failure modes — all fail-OPEN (proceed to ``generate_sql``):

  * ``schema_profiles`` table is empty (no indexer run yet).
  * LLM returned non-JSON or schema-violating JSON.
  * Any unexpected exception inside the node.
  * ``COVERAGE_CHECK_ENABLED`` is False (eval baseline).

The reasoning: this gate is a quality-of-life feature, not a safety
boundary. A buggy gate that wrongly refuses a question is a worse
user experience than the pre-Phase-1.1 "hallucinated SQL" behaviour.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from copilot.agent.prompts import (
    COVERAGE_CHECK_SYSTEM,
    COVERAGE_CHECK_USER_TEMPLATE,
    EXPLAIN_UNCOVERED_SYSTEM,
    EXPLAIN_UNCOVERED_USER_TEMPLATE,
)
from copilot.agent.state import AgentState
from copilot.cost import (
    CostBreakdown,
    estimate_tokens_from_chars,
    llm_call_cost,
    usage_from_response,
)
from copilot.llm import get_llm
from copilot.profiler import format_profile_for_llm, load_profile

log = logging.getLogger(__name__)


# Match the same loose-fence stripper used by ``insight.py`` / analyst,
# so an LLM that ignores instructions and wraps JSON in ```json ...```
# still parses.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)


def _strip_fence(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def _message_text(msg: AIMessage) -> str:
    """Same coercion as ``copilot.agent.nodes._message_text``. Duplicated
    here to avoid a cross-module import that would create a cycle once
    nodes.py later imports this module."""
    content = msg.content
    if isinstance(content, str):
        return content
    return "".join(
        part.get("text", "") if isinstance(part, dict) else str(part) for part in content
    )


def _llm_cost(response: AIMessage, prompt_text: str) -> CostBreakdown:
    """Build a ``llm_call_cost`` increment from one LLM round-trip.

    Trimmed-down copy of ``nodes._llm_cost``; deliberately not imported
    to keep this module independent.
    """
    from copilot.config import get_settings  # local to avoid global import cost

    model = get_settings().deepseek_model
    usage = usage_from_response(response)
    if usage is not None:
        tokens_in, tokens_out = usage
    else:
        tokens_in = estimate_tokens_from_chars(prompt_text)
        tokens_out = estimate_tokens_from_chars(_message_text(response))
    return llm_call_cost(model, tokens_in=tokens_in, tokens_out=tokens_out)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _coerce_string_list(raw: Any, *, max_items: int = 5) -> list[str]:
    """Best-effort coerce ``raw`` into a clean ``list[str]`` of length
    ``<= max_items``. Drops empty / non-string entries silently."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if cleaned:
            out.append(cleaned)
        if len(out) >= max_items:
            break
    return out


def parse_coverage_response(raw: str) -> dict[str, Any] | None:
    """Parse one LLM reply into a coverage verdict dict.

    Returns ``None`` on any parse / validation failure so the caller
    can fail-open. Exported for unit tests so we can exercise the
    parser without a real LLM call.
    """
    if not raw or not raw.strip():
        return None
    cleaned = _strip_fence(raw)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning("coverage: JSON parse failed (%s)", exc)
        return None
    if not isinstance(payload, dict):
        log.warning("coverage: top-level JSON is not an object")
        return None

    verdict_raw = str(payload.get("verdict", "")).strip().lower()
    if verdict_raw not in {"ok", "refuse"}:
        log.warning("coverage: unrecognised verdict %r; treating as fail-open", verdict_raw)
        return None

    reason = str(payload.get("reason", "")).strip()
    missing = _coerce_string_list(payload.get("missing_concepts"), max_items=5)
    suggested = _coerce_string_list(payload.get("suggested_questions"), max_items=5)

    return {
        "verdict": verdict_raw,
        "reason": reason,
        "missing_concepts": missing,
        "suggested_questions": suggested,
    }


def parse_uncovered_response(raw: str) -> dict[str, Any] | None:
    """Parse the ``explain_uncovered`` LLM reply into a structured
    refusal envelope. Same fail-soft policy as ``parse_coverage_response``."""
    if not raw or not raw.strip():
        return None
    cleaned = _strip_fence(raw)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning("explain_uncovered: JSON parse failed (%s)", exc)
        return None
    if not isinstance(payload, dict):
        return None

    headline = str(payload.get("headline", "")).strip()
    bullets = _coerce_string_list(payload.get("bullets"), max_items=5)
    suggested = _coerce_string_list(payload.get("suggested_questions"), max_items=5)

    if not headline:
        return None

    return {
        "headline": headline,
        "bullets": bullets,
        "suggested_questions": suggested,
    }


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def _load_profile_text(tables: list[str]) -> str:
    """Look up the profile rows for ``tables`` and render them for the
    LLM. Returns the empty string if the table is missing or empty,
    which the caller treats as "skip the gate" (fail-open)."""
    if not tables:
        return ""
    try:
        by_table = load_profile(tables)
    except Exception as exc:
        # Most likely cause: schema_profiles doesn't exist yet (fresh DB
        # without indexer run). Log loudly once, then fail-open.
        log.warning("coverage: load_profile failed (%s); skipping gate", exc)
        return ""
    if not by_table:
        return ""
    return format_profile_for_llm(by_table)


def coverage_check_node(state: AgentState) -> dict[str, Any]:
    """Decide whether the retrieved schema can answer ``state['question']``.

    Always returns at least ``{"coverage": ...}``. On any failure the
    coverage record carries ``verdict="ok"`` so the downstream router
    behaves as if the gate hadn't run.
    """
    from copilot.agent import feature_flags

    if not feature_flags.COVERAGE_CHECK_ENABLED:
        log.info("coverage_check: disabled (feature flag) → ok")
        return {
            "coverage": {
                "verdict": "ok",
                "reason": "coverage check disabled",
                "missing_concepts": [],
                "suggested_questions": [],
            }
        }

    question = state["question"]
    tables = list(state.get("relevant_tables") or [])

    profile_text = _load_profile_text(tables)
    if not profile_text:
        log.info("coverage_check: no profile available → ok (fail-open)")
        return {
            "coverage": {
                "verdict": "ok",
                "reason": "schema profile unavailable",
                "missing_concepts": [],
                "suggested_questions": [],
            }
        }

    user_msg = COVERAGE_CHECK_USER_TEMPLATE.format(
        profile=profile_text, question=question
    )

    try:
        llm = get_llm(
            temperature=0.0,
            model_kwargs={"response_format": {"type": "json_object"}},
        )
        response = llm.invoke(
            [
                SystemMessage(content=COVERAGE_CHECK_SYSTEM),
                HumanMessage(content=user_msg),
            ]
        )
    except Exception as exc:
        log.warning("coverage_check: LLM call failed (%s); fail-open", exc)
        return {
            "coverage": {
                "verdict": "ok",
                "reason": "coverage check unavailable",
                "missing_concepts": [],
                "suggested_questions": [],
            }
        }

    raw = _message_text(response).strip()
    cost = _llm_cost(response, user_msg)
    parsed = parse_coverage_response(raw)

    if parsed is None:
        log.warning("coverage_check: unparsable reply → fail-open. raw=%r", raw[:300])
        return {
            "coverage": {
                "verdict": "ok",
                "reason": "coverage verdict unparsable",
                "missing_concepts": [],
                "suggested_questions": [],
            },
            "cost": cost,
        }

    log.info(
        "coverage_check: verdict=%s reason=%r missing=%s",
        parsed["verdict"],
        parsed["reason"][:80],
        parsed["missing_concepts"],
    )
    return {"coverage": parsed, "cost": cost}


def route_after_coverage(state: AgentState) -> str:
    """Pick the next node after ``coverage_check_node`` ran.

    The verdict is always populated (the node fails open to ``ok``).
    Unknown values are treated as ``ok`` defensively.
    """
    coverage = state.get("coverage") or {}
    if coverage.get("verdict") == "refuse":
        return "explain_uncovered"
    return "generate_sql"


def explain_uncovered_node(state: AgentState) -> dict[str, Any]:
    """Produce a friendly, structured refusal when the gate says refuse.

    Output mirrors the existing ``insight`` envelope so the front-end
    can render this turn with the same component as a normal answer
    plus a small "I cannot answer" badge and clickable suggested
    follow-ups (apps/web/components/CoverageRefusal.tsx).

    On LLM failure or parse failure we fall back to a deterministic
    template — never an exception. This node MUST always populate
    ``answer`` because ``append_to_dialogue_node`` reads it.
    """
    coverage = state.get("coverage") or {}
    question = state["question"]
    tables = list(state.get("relevant_tables") or [])
    profile_text = _load_profile_text(tables)

    missing = coverage.get("missing_concepts") or []
    suggested_in = coverage.get("suggested_questions") or []

    user_msg = EXPLAIN_UNCOVERED_USER_TEMPLATE.format(
        question=question,
        reason=coverage.get("reason") or "no specific reason recorded",
        missing_concepts=", ".join(missing) if missing else "(none specified)",
        suggested_questions=(
            "\n  - " + "\n  - ".join(suggested_in) if suggested_in else "(none specified)"
        ),
        profile=profile_text or "(schema profile unavailable)",
    )

    parsed: dict[str, Any] | None = None
    cost: CostBreakdown | None = None
    try:
        llm = get_llm(
            temperature=0.2,
            model_kwargs={"response_format": {"type": "json_object"}},
        )
        response = llm.invoke(
            [
                SystemMessage(content=EXPLAIN_UNCOVERED_SYSTEM),
                HumanMessage(content=user_msg),
            ]
        )
        raw = _message_text(response).strip()
        cost = _llm_cost(response, user_msg)
        parsed = parse_uncovered_response(raw)
    except Exception as exc:
        log.warning("explain_uncovered: LLM call failed (%s); using template", exc)

    if parsed is None:
        # Deterministic fallback so the turn always finalises with an
        # ``answer``. Use whatever the gate already gave us.
        headline = (
            "This database doesn't seem to have the information needed "
            "to answer that question."
        )
        if missing:
            headline = (
                "I can't answer that here — the database has no "
                + ", ".join(missing[:2])
                + " data."
            )
        parsed = {
            "headline": headline,
            "bullets": [],
            "suggested_questions": suggested_in,
        }

    # Merge gate-side suggestions with LLM-side suggestions, dedup
    # case-insensitively, cap at 3 so the front-end chip row stays tidy.
    seen: set[str] = set()
    merged: list[str] = []
    for q in (*parsed.get("suggested_questions", []), *suggested_in):
        key = q.strip().lower()
        if key and key not in seen:
            seen.add(key)
            merged.append(q.strip())
        if len(merged) >= 3:
            break
    parsed["suggested_questions"] = merged

    log.info(
        "explain_uncovered: headline=%r suggested=%d",
        parsed["headline"][:80],
        len(merged),
    )

    out: dict[str, Any] = {
        "answer": parsed["headline"],
        # Surface the structured envelope on ``coverage`` so the front-
        # end can render bullets + chips without inventing a new field.
        "coverage": {
            **coverage,
            "verdict": "refuse",
            "headline": parsed["headline"],
            "bullets": parsed.get("bullets", []),
            "suggested_questions": parsed["suggested_questions"],
        },
    }
    if cost is not None:
        out["cost"] = cost
    return out
