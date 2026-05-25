"""Pydantic envelopes that flow between Supervisor, SQL Specialist
and Analyst.

We deliberately use Pydantic (not LangChain ``BaseMessage`` blobs)
for inter-agent traffic. Three reasons:

* Strong typing across the call boundary — every consumer can rely
  on the shape, not re-parse a string.
* The shapes round-trip through ``AskResponse`` and the
  ``/admin/stats`` endpoint without a ``model_dump_json`` adapter.
* Tests can construct envelopes directly without spinning up a
  whole graph.

ADR 0014 §"Why Pydantic envelopes" tracks the rationale.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Analyst output shapes
# ---------------------------------------------------------------------------


class AnalystAnomaly(BaseModel):
    """One callout about something interesting in the rows.

    ``severity`` is a coarse signal — ``"info"`` for "noteworthy but
    unsurprising", ``"warn"`` for "outlier worth a second look",
    ``"critical"`` for "this likely indicates a data issue or a
    significant event". The UI uses it to colour the badge.
    """

    label: str = Field(..., min_length=1, max_length=200)
    detail: str = Field(..., min_length=1, max_length=400)
    severity: Literal["info", "warn", "critical"] = "info"


class AnalystFollowup(BaseModel):
    """One "you might also want to know..." suggestion.

    Each follow-up is a fully-formed question plus a one-line
    rationale. The UI renders these as clickable chips that re-submit
    via ``/ask`` when the user picks one.
    """

    question: str = Field(..., min_length=1, max_length=300)
    rationale: str = Field(..., min_length=1, max_length=300)
    expected_chart_kind: Literal["kpi", "bar", "line", "grouped_bar", "table"] | None = None


class DrillDownRequest(BaseModel):
    """The Analyst's request to recursively invoke the SQL Specialist.

    Bounded by ``SupervisorState.hop_count`` — the supervisor refuses
    the request once the cap is reached. The Analyst itself also
    refuses to emit one if the parent turn was already a drill-down.
    """

    question: str = Field(..., min_length=1, max_length=400)
    why: str = Field(..., min_length=1, max_length=400)


class AnalystResponse(BaseModel):
    """Everything the Analyst produces for a single SQL answer.

    Every list field is allowed to be empty: the Analyst's correct
    behaviour on "nothing interesting here" is to fall silent rather
    than invent observations.
    """

    anomalies: list[AnalystAnomaly] = Field(default_factory=list, max_length=4)
    followups: list[AnalystFollowup] = Field(default_factory=list, max_length=3)
    drill_down: DrillDownRequest | None = None


class AnalystRequest(BaseModel):
    """Input envelope handed to the Analyst.

    Built by ``supervisor.py`` from the SQL Specialist's output state.
    Captured as its own model (rather than a ``dict``) so future
    changes to the Specialist's output shape only have to update one
    adapter site — Analyst code only sees the typed contract.
    """

    question: str
    sql: str | None
    answer: str
    rows: list[dict[str, Any]]
    row_count: int | None
    chart_kind: str | None
    chart_spec: dict[str, Any] | None
    dialogue_recent: list[dict[str, Any]] = Field(default_factory=list)
    """Last ~6 turns from the conversation, used by the Analyst to
    decide whether a drill-down would even be novel."""
    hop_count: int = 0
    """How many SQL Specialist invocations have already happened this
    turn. The Analyst is forbidden from emitting a drill_down when
    ``hop_count >= 1`` (single drill-down per user turn)."""
