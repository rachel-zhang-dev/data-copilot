"""Structured insight envelope (week 8).

``summarize_result_node`` used to emit a single natural-language string
into ``state.answer``. Week 8 upgrades the LLM prompt to return a JSON
object with three sections (headline + bullets + metric highlights),
which is parsed here.

Two consumers care about the shape:

* Every existing caller (CLI, future Next.js UI, eval grader) keeps
  reading ``state.answer`` — the node sets that to the parsed
  ``headline`` so the contract is unchanged.
* New consumers (richer UI, alerting, slide-deck export) read
  ``state.insight`` and get the full structured envelope.

JSON parse failures degrade silently to "answer = raw text, insight =
None" so a misbehaving LLM never blocks a user.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

log = logging.getLogger(__name__)

# Cap so a runaway model cannot bloat the response payload.
_MAX_HEADLINE_CHARS = 400
_MAX_BULLET_CHARS = 240
_MAX_BULLETS = 6
_MAX_HIGHLIGHTS = 8


class MetricHighlight(BaseModel):
    """One KPI-style callout. The UI typically renders these as a row
    of tiles above the chart."""

    label: str = Field(..., min_length=1, max_length=120)
    value: float
    format: str = ""  # e.g. "currency" | "percent" | "integer" | ""


class Insight(BaseModel):
    """Structured replacement for the legacy single-sentence ``answer``.

    Constraints exist purely so a non-cooperating LLM cannot grow the
    response payload without bound. The bounds are generous for
    well-behaved models.
    """

    headline: str = Field(..., min_length=1, max_length=_MAX_HEADLINE_CHARS)
    bullets: list[str] = Field(default_factory=list, max_length=_MAX_BULLETS)
    metric_highlights: list[MetricHighlight] = Field(
        default_factory=list, max_length=_MAX_HIGHLIGHTS
    )

    @field_validator("bullets")
    @classmethod
    def _cap_bullet_length(cls, v: list[str]) -> list[str]:
        """Per-bullet length cap (week 9). The ``max_length`` above
        caps how many bullets we accept; this caps how long each
        bullet can be so a misbehaving LLM cannot bloat the response
        with one 50 KB bullet. Truncate-with-ellipsis rather than
        reject — partial information is more useful than nothing."""
        return [
            b if len(b) <= _MAX_BULLET_CHARS else b[: _MAX_BULLET_CHARS - 3] + "..."
            for b in v
        ]


# A model occasionally wraps its JSON in ```json ... ``` fences even
# when explicitly told not to. The summarize prompt already says "no
# fences", but this regex makes the parser robust to that one common
# disobedience rather than throwing the whole insight away.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)


def _strip_fence(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def parse_insight(raw: str) -> Insight | None:
    """Best-effort parse of an LLM reply into an ``Insight``.

    Returns ``None`` on any parse / schema error. Callers should treat
    ``None`` as "fall back to the legacy NL-only path".
    """
    if not raw or not raw.strip():
        return None
    cleaned = _strip_fence(raw)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning("insight JSON parse failed: %s", exc)
        return None
    try:
        return Insight.model_validate(payload)
    except ValidationError as exc:
        log.warning("insight schema validation failed: %s", exc)
        return None


def insight_to_state(insight: Insight) -> dict[str, Any]:
    """Project an ``Insight`` into the fields ``summarize_result_node``
    is expected to write — ``answer`` (str) and ``insight`` (dict).

    Keeps the truncation logic for ``answer`` in one place so a future
    headline-only consumer (e.g. Slack notification) can call this
    function directly.
    """
    return {
        "answer": insight.headline,
        "insight": insight.model_dump(),
    }
