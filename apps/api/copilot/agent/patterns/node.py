"""LangGraph node: detect statistical patterns and merge findings into
the insight envelope (Phase 1.2 / ADR 0017).

Pipeline per turn (on the data success path only):

  1. ``detect_patterns`` runs the pure-stat detectors — outliers, trend
     — over ``state['sql_result']`` and returns 0..N ``Finding``s.
  2. If there are no findings, the node returns ``{}`` (no-op). This
     is the common case for KPI / chitchat / error / refused turns;
     the existing graph behaviour is unchanged.
  3. Otherwise we ask the LLM (JSON mode) to render one bullet per
     finding in the user's language. The renderer is the **only**
     LLM call this node makes; if it fails or returns malformed
     JSON we fall back to a deterministic template so the turn
     still surfaces SOMETHING — but every bullet is anchored to a
     specific number from the payload, so there's no risk of
     hallucinated facts.
  4. Patterns are emitted on two state fields:
       * ``state['patterns']``        — structured list (FE can render
         badges, chart annotations later).
       * ``state['insight']['bullets']`` — pattern bullets are
         prepended to the existing summary bullets so the front-end's
         InsightPanel surfaces them with zero extra code.

Failure modes (all fail-soft — pattern bullets simply don't appear):

  * No numeric column / too few rows                → no LLM call, no-op.
  * ``PATTERNS_DETECTION_ENABLED=False`` feature    → no LLM call, no-op.
  * LLM call raises (rate limit / network / etc.)   → use templated
    bullets, log warning.
  * LLM returns non-JSON / wrong shape              → use templated
    bullets.
  * LLM returns more / fewer bullets than findings  → truncate / pad
    deterministically.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from copilot.agent.coverage import _llm_cost, _message_text
from copilot.agent.patterns.detectors import (
    Finding,
    detect_patterns,
    finding_to_dict,
)
from copilot.agent.prompts import (
    PATTERN_RENDER_SYSTEM,
    PATTERN_RENDER_USER_TEMPLATE,
)
from copilot.agent.state import AgentState
from copilot.llm import get_llm

log = logging.getLogger(__name__)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)


def _strip_fence(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def parse_render_response(raw: str, *, expected_count: int) -> list[str] | None:
    """Parse the renderer LLM reply into a list of bullet strings.

    Returns ``None`` on any parse / validation failure. Exposed for
    unit tests so the parser can be exercised without an LLM call.
    """
    if not raw or not raw.strip():
        return None
    cleaned = _strip_fence(raw)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning("pattern renderer: JSON parse failed (%s)", exc)
        return None
    if not isinstance(payload, dict):
        return None

    bullets_raw = payload.get("bullets")
    if not isinstance(bullets_raw, list):
        return None

    bullets: list[str] = []
    for item in bullets_raw:
        if isinstance(item, str) and item.strip():
            bullets.append(item.strip())

    if not bullets:
        return None

    # Length normalisation: truncate to expected_count, pad with the
    # last bullet repeated if the model under-delivered. The latter
    # is a defensive corner case — better than dropping findings.
    if len(bullets) >= expected_count:
        return bullets[:expected_count]
    while len(bullets) < expected_count:
        bullets.append(bullets[-1])
    return bullets


# ---------------------------------------------------------------------------
# Deterministic fallback templates
# ---------------------------------------------------------------------------


def _fallback_bullet(f: Finding) -> str:
    """Render one finding without the LLM. Always English (we don't
    know the user's language at this layer) but always grounded in
    the payload numbers — never wrong, just less polished.

    Used when the renderer LLM call fails for any reason.
    """
    p = f.payload
    if f.kind == "outlier":
        label = p.get("label") or "an entry"
        value = p.get("value")
        z = p.get("z_score")
        direction = "above" if str(f.description_key).startswith("high") else "below"
        z_text = f"{abs(float(z)):.1f}σ" if z is not None else "outlier"
        return f"{label} ({value}) is {z_text} {direction} the mean — notable outlier."

    if f.kind == "trend":
        first = p.get("first_value")
        last = p.get("last_value")
        pct = p.get("delta_pct")
        direction = "rose" if str(f.description_key) == "trend_up" else "fell"
        if pct is not None:
            return f"{f.column} {direction} from {first} to {last} ({pct:+.1f}%) — clear trend."
        return f"{f.column} {direction} from {first} to {last} — clear trend."

    return f"Pattern detected on {f.column}."


def _fallback_bullets(findings: list[Finding]) -> list[str]:
    return [_fallback_bullet(f) for f in findings]


# ---------------------------------------------------------------------------
# Merging into the insight envelope
# ---------------------------------------------------------------------------


_BULLET_CAP = 6
"""Total bullets shown to the user. summarize_result already aims for
0-4 bullets; we cap the merged list at 6 so 2-3 pattern bullets land
without crowding the legacy insight out of view."""


def _merge_into_insight(
    existing: dict[str, Any] | None, pattern_bullets: list[str]
) -> dict[str, Any] | None:
    """Prepend pattern bullets to the existing ``insight.bullets`` list.

    Why prepend, not append: pattern findings are typically the most
    informative thing about the result ("Brazil is an outlier") and
    deserve to be read first. The legacy bullets ("21 countries
    total") are still visible below.

    Returns ``None`` when the existing insight is ``None`` and no
    pattern bullets exist (preserves the old "no insight" semantics
    for empty result sets). When pattern bullets exist but insight
    is missing, we materialise a minimal insight so the bullets
    still show.
    """
    if not pattern_bullets:
        return existing

    if existing is None:
        return {
            "headline": "",
            "bullets": pattern_bullets[:_BULLET_CAP],
            "metric_highlights": [],
        }

    legacy = list(existing.get("bullets") or [])
    merged = pattern_bullets + legacy
    out = dict(existing)
    out["bullets"] = merged[:_BULLET_CAP]
    return out


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


def detect_patterns_node(state: AgentState) -> dict[str, Any]:
    """LangGraph node entrypoint.

    Always returns a dict (possibly empty). The graph compiles
    ``state['patterns']`` and ``state['insight']`` updates back into
    the rolling state via the default replace reducer.
    """
    from copilot.agent import feature_flags

    if not feature_flags.PATTERNS_DETECTION_ENABLED:
        log.info("detect_patterns: disabled by feature flag → skip")
        return {}

    rows = state.get("sql_result") or []
    findings = detect_patterns(rows)
    if not findings:
        # The common case (KPI, single-row results, constant data).
        # We deliberately do NOT emit ``patterns: []`` — leaving the
        # field unset keeps the AskResponse small.
        return {}

    log.info(
        "detect_patterns: %d finding(s): %s",
        len(findings),
        [f"{f.kind}/{f.description_key}({f.severity})" for f in findings],
    )

    # Render via LLM (JSON mode). Fail-soft to template on any error.
    bullets: list[str] | None = None
    cost = None
    try:
        llm = get_llm(
            temperature=0.0,
            model_kwargs={"response_format": {"type": "json_object"}},
        )
        findings_json = json.dumps(
            [finding_to_dict(f) for f in findings], ensure_ascii=False
        )
        user_msg = PATTERN_RENDER_USER_TEMPLATE.format(
            question=state["question"],
            sql=state.get("sql", ""),
            findings_json=findings_json,
        )
        response = llm.invoke(
            [
                SystemMessage(content=PATTERN_RENDER_SYSTEM),
                HumanMessage(content=user_msg),
            ]
        )
        raw = _message_text(response).strip()
        cost = _llm_cost(response, user_msg)
        bullets = parse_render_response(raw, expected_count=len(findings))
        if bullets is None:
            log.warning("pattern renderer: unparsable reply, falling back. raw=%r", raw[:300])
    except Exception as exc:
        log.warning("pattern renderer: LLM call failed (%s); falling back", exc)

    if bullets is None:
        bullets = _fallback_bullets(findings)

    merged_insight = _merge_into_insight(state.get("insight"), bullets)

    out: dict[str, Any] = {
        "patterns": [finding_to_dict(f) for f in findings],
        "insight": merged_insight,
    }
    if cost is not None:
        out["cost"] = cost
    return out
