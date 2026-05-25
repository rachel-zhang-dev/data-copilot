"""Supervisor-level state.

Sits one level above ``copilot.agent.state.AgentState`` (which the
SQL Specialist owns). The Supervisor reads from the Specialist's
output, hands it to the Analyst as a typed request, and may loop
back into the Specialist with a drill-down question — bounded by
``hop_count``.

We deliberately keep the two states separate (rather than merging
into one mega-TypedDict) so the Specialist's existing 12-node graph
keeps its self-contained contract and tests.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from copilot.agents.messages import AnalystResponse


def _last(_left: AnalystResponse | None, right: AnalystResponse | None) -> AnalystResponse | None:
    """Reducer for ``analyst``: keep the latest non-None value.

    On a recursive drill-down round-trip the Analyst can fire twice
    (once on the parent, once on the drill-down); the user-facing
    output should be the *final* one. ``None`` from a later write
    leaves the previous value in place — handy for the supervisor
    short-circuit branches that never produce an Analyst result.
    """
    return right if right is not None else _left


def _append(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reducer for ``drill_downs``: plain append. The supervisor adds
    one entry per recursive Specialist invocation."""
    return [*left, *right]


class SupervisorState(TypedDict, total=False):
    """State carried through the multi-agent graph.

    Three groups of fields:

    * **Inputs** — the same shape as ``AskRequest``. The supervisor
      passes ``question`` (or a drill-down ``question`` on the
      recursive loop) into the SQL Specialist.

    * **Specialist output** — ``sql_result`` is the entire
      ``AgentState`` dict the Specialist's compiled graph returns.
      We embed it rather than promote individual fields so the
      Specialist stays self-contained.

    * **Analyst output** — ``analyst`` plus optional ``drill_downs``
      (a list of further ``AskResponse``-shaped dicts produced when
      the Analyst asked for a recursive Specialist call).

    Plus the bounded-recursion counter (``hop_count``) and the
    ``analyst_enabled`` flag the eval harness flips for A/B runs.
    """

    # ---------- Inputs ----------
    question: str | None
    conversation_id: str | None
    resume: Literal["approve", "reject"] | None
    debug: bool

    # ---------- Specialist output (one full AgentState worth) ----------
    sql_result: dict[str, Any]
    """The Specialist's final state. Treated as an opaque dict at
    the supervisor level so its shape can evolve without churn here."""

    # ---------- Analyst output ----------
    analyst: Annotated[AnalystResponse | None, _last]
    """The Analyst's structured envelope for THIS turn (or the
    drill-down's, after a recursive call). ``None`` when Analyst
    was disabled or skipped."""

    drill_downs: Annotated[list[dict[str, Any]], _append]
    """One per recursive Specialist invocation triggered by the
    Analyst. Each entry is an ``AskResponse``-shaped dict."""

    # ---------- Control ----------
    hop_count: int
    """How many times the SQL Specialist has run this turn. The
    initial invocation increments to 1; one drill-down bumps to 2;
    further drill-downs are refused by ``route_after_analyst``."""

    analyst_enabled: bool
    """Feature-flag mirror so ``feature_flags.ANALYST_ENABLED``
    propagates cleanly into the routing decision without each route
    re-reading the module global."""
