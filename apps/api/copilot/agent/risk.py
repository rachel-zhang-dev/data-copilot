"""Risk check + human-in-the-loop confirmation (week 7).

The agent already rejects non-SELECT SQL in ``sql_safety`` and retries
recoverable execution errors in the self-healing loop. What this module
adds is the *third* failure mode: SQL that is technically legal but
expensive enough that running it without a human confirmation would be
irresponsible (think four-way JOIN with no WHERE clause).

Three pieces, kept small and individually testable:

* ``check_risk_node``        — runs Postgres ``EXPLAIN (FORMAT JSON)``
                               on the validated SQL and writes a
                               ``pending_risk`` payload to state when
                               the planner cost exceeds the configured
                               threshold.
* ``await_confirmation_node`` — calls LangGraph's ``interrupt()`` to
                                pause the graph and surface the
                                ``pending_risk`` payload to the caller.
                                When resumed via ``Command(resume=...)``,
                                stores the decision in ``risk_decision``.
* ``route_after_*``          — small router functions for the graph.

The risk check fails open: on any error (EXPLAIN parse failure,
statement_timeout, schema drift) we log a warning and route straight to
``execute_sql``. A broken risk check must never block legitimate
queries; the worst case is "the user runs an expensive query without a
confirm prompt", which is identical to the pre-week-7 behaviour.

See ``docs/decisions/0008-human-in-the-loop.md`` for the design
rationale, threshold tuning notes, and rejected alternatives.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.types import interrupt

from copilot.agent.state import AgentState, Attempt
from copilot.config import get_settings
from copilot.cost import db_explain_cost
from copilot.db import explain_cost

log = logging.getLogger(__name__)


def _build_pending_risk(sql: str, total_cost: float, threshold: float) -> dict[str, Any]:
    """Shape the diagnostic payload surfaced to the caller during a pause.

    Kept as plain JSON-able primitives so it round-trips cleanly through
    the FastAPI Pydantic layer and through the LangGraph checkpointer.
    """
    return {
        "sql": sql,
        "total_cost": float(total_cost),
        "threshold": float(threshold),
        "reason": (
            f"Postgres planner estimated total cost {total_cost:.1f} "
            f"exceeds the configured threshold of {threshold:.1f}. "
            "Approve to execute, reject to cancel."
        ),
    }


def check_risk_node(state: AgentState) -> dict[str, Any]:
    """Decide whether the SQL needs human confirmation before execution.

    Strategy:
      1. If ``risk_explain_cost_threshold`` is 0, the check is disabled —
         always route to ``execute_sql``.
      2. Run ``EXPLAIN (FORMAT JSON)`` on the validated SQL.
      3. Compare ``Plan.Total Cost`` against the threshold.
      4. Above threshold → write ``pending_risk`` and let
         ``route_after_risk`` send us to ``await_confirmation``.
      5. On any EXPLAIN error → fall through. A failing risk check must
         not block the user; ``execute_sql`` will produce the same
         error the user would have hit anyway, with the regular
         self-healing path taking over.
    """
    settings = get_settings()
    threshold = settings.risk_explain_cost_threshold
    sql = state.get("sql")

    if not sql:
        # No SQL to check (e.g. chitchat path). Should not normally reach
        # this node — defensive only.
        return {}

    if threshold <= 0:
        log.info("check_risk: threshold disabled, skipping")
        return {}

    explain_cost_increment = db_explain_cost()
    try:
        cost = explain_cost(sql, timeout_ms=settings.risk_explain_timeout_ms)
    except Exception as exc:
        log.warning("check_risk: EXPLAIN failed (%s); treating as low risk", exc)
        # Still charge for the attempted call so observability sees it.
        return {"cost": explain_cost_increment}

    if cost <= threshold:
        log.info("check_risk: cost=%.1f <= threshold=%.1f, low risk", cost, threshold)
        return {"cost": explain_cost_increment}

    log.info("check_risk: cost=%.1f > threshold=%.1f, will request confirmation", cost, threshold)
    return {
        "pending_risk": _build_pending_risk(sql, cost, threshold),
        "cost": explain_cost_increment,
    }


def await_confirmation_node(state: AgentState) -> dict[str, Any]:
    """Pause the graph until the caller sends ``Command(resume=...)``.

    ``interrupt(value)`` checkpoints the state and returns the value the
    caller eventually passes back. We normalise that value into one of
    ``"approved"`` / ``"rejected"`` so routers downstream only have to
    match against a closed set.

    On rejection we also synthesise an ``Attempt`` record so the user-
    facing ``finalize_error`` message can say "you rejected this query",
    and so the eval grader sees the same shape as any other terminal
    failure.

    Note on re-runs: LangGraph re-invokes this node from the top when
    the graph is resumed; ``interrupt()`` is the very first statement,
    so the only line that executes before the resume value is in hand
    is reading ``state["pending_risk"]``. No side effects to dedupe.
    """
    payload = state.get("pending_risk") or {}
    raw = interrupt(payload)

    decision = _coerce_decision(raw)
    log.info("await_confirmation: decision=%s (raw=%r)", decision, raw)

    if decision == "approved":
        return {"risk_decision": "approved"}

    turn_idx = state.get("turn_index", 1)
    sql = state.get("sql", "")
    return {
        "risk_decision": "rejected",
        "error": "user_rejected: confirmation declined",
        "attempts": [
            Attempt(
                sql=sql,
                error="user rejected the confirmation prompt",
                error_class="user_rejected",
                turn_idx=turn_idx,
            )
        ],
    }


def _coerce_decision(raw: Any) -> str:
    """Map whatever the caller passed to ``Command(resume=...)`` to one
    of ``"approved"`` / ``"rejected"``.

    Accepts:
      * ``True`` / ``"approve"`` / ``"approved"`` / ``"yes"`` / ``"y"`` -> approved
      * everything else -> rejected (safer default)
    """
    if raw is True:
        return "approved"
    if isinstance(raw, str) and raw.strip().lower() in {"approve", "approved", "yes", "y"}:
        return "approved"
    return "rejected"


def route_after_risk(state: AgentState) -> str:
    """``check_risk_node`` populates ``pending_risk`` iff confirmation
    is needed. No payload means low risk (or check disabled / errored)."""
    if state.get("pending_risk"):
        return "await_confirmation"
    return "execute_sql"


def route_after_confirmation(state: AgentState) -> str:
    """Approved → run the SQL; anything else → terminal user_rejected."""
    if state.get("risk_decision") == "approved":
        return "execute_sql"
    return "finalize_error"
