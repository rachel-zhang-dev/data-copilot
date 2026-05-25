"""Eval runner.

Wires together: cases loader → graph builder → invoke → deterministic
grader → aggregation. Outputs an ``ExperimentResult`` that the report
renderer turns into markdown.

Concurrency note
----------------
Cases are run sequentially within a single experiment because:

* The feature_flags are global mutable state — running two cases in
  parallel inside one experiment would race on the flags.
* DeepSeek's free tier rate-limits aggressively; sequential calls
  produce a more predictable cost / latency profile than burst
  parallelism that ends up serialised by the provider anyway.

If we ever need to parallelise, the right fix is per-experiment
isolated state, not naive ``asyncio.gather``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from copilot.agent import build_graph, feature_flags
from copilot.agent.state import Turn
from copilot.eval.cases import CaseSpec, HistoryTurn, load_cases
from copilot.eval.config import ExperimentConfig
from copilot.eval.graders.deterministic import GradeReport, RunResult, grade

log = logging.getLogger(__name__)


@dataclass
class CaseOutcome:
    """Per-case result combining the run, the grade, and timing data."""

    case: CaseSpec
    run: RunResult
    grade: GradeReport


@dataclass
class ExperimentResult:
    """Aggregated outcome of running a config against the case set."""

    config: ExperimentConfig
    outcomes: list[CaseOutcome] = field(default_factory=list)

    # ------- summary metrics ----------
    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.grade.passed)

    @property
    def success_rate(self) -> float:
        return (self.passed / self.total) if self.total else 0.0

    @property
    def avg_attempts(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(o.run.attempts for o in self.outcomes) / self.total

    @property
    def avg_latency_ms(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(o.run.latency_ms for o in self.outcomes) / self.total

    @property
    def avg_total_tokens(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(o.run.total_tokens for o in self.outcomes) / self.total

    def by_category(self) -> dict[str, dict[str, float]]:
        """Return per-category aggregates: success_rate, n, avg_attempts."""
        buckets: dict[str, list[CaseOutcome]] = {}
        for o in self.outcomes:
            buckets.setdefault(o.case.category, []).append(o)
        return {
            cat: {
                "n": len(group),
                "passed": sum(1 for o in group if o.grade.passed),
                "success_rate": sum(1 for o in group if o.grade.passed) / len(group),
                "avg_attempts": sum(o.run.attempts for o in group) / len(group),
                "avg_latency_ms": sum(o.run.latency_ms for o in group) / len(group),
            }
            for cat, group in buckets.items()
        }

    def failures(self) -> list[CaseOutcome]:
        return [o for o in self.outcomes if not o.grade.passed]


# ---------------------------------------------------------------------------
# Per-case invocation
# ---------------------------------------------------------------------------


def _history_turn_to_state_turn(h: HistoryTurn) -> Turn:
    """Convert an eval-side ``HistoryTurn`` to a runtime ``Turn``."""
    out: Turn = {"role": h.role, "content": h.content}
    if h.sql is not None:
        out["sql"] = h.sql
    return out


def _initial_state(case: CaseSpec) -> dict[str, Any]:
    """Build the input dict for ``graph.ainvoke``.

    For follow-up cases we seed ``dialogue`` with the synthetic history
    turns directly, then ask the actual question. This avoids
    re-running prior turns through the graph (which would burn LLM
    calls and add nondeterminism for no benefit).
    """
    state: dict[str, Any] = {"question": case.question}
    if case.setup_history:
        state["dialogue"] = [_history_turn_to_state_turn(h) for h in case.setup_history]
    return state


def _estimate_tokens_from_messages(state: dict[str, Any]) -> int:
    """Rough token estimate from the agent's emitted state.

    We don't have direct access to the LLM's usage report (would
    require provider hooks). Approximate via ``chars / 4`` over the
    SQL + answer + recorded attempts. Good enough for relative
    comparisons between A/B runs even if absolute numbers are off.
    """
    chars = 0
    chars += len(state.get("sql") or "")
    chars += len(state.get("answer") or "")
    for a in state.get("attempts") or []:
        chars += len(a.get("sql", "")) + len(a.get("error", ""))
    for t in state.get("dialogue") or []:
        chars += len(t.get("content", "")) + len(t.get("sql") or "")
    # Schema is the dominant cost; charge based on relevant_schema size.
    chars += len(state.get("relevant_schema") or "")
    return chars // 4


async def _invoke_case(
    case: CaseSpec, graph: Any, *, timeout_s: float | None
) -> RunResult:
    """Run one case through the graph and return a grading-friendly RunResult.

    ``timeout_s`` (when set) caps how long a single ``graph.ainvoke`` is
    allowed to run. A hung LLM call previously stalled the entire eval;
    with a timeout, the case is recorded as a ``runner_timeout`` failure
    and the run moves on. ``None`` keeps the legacy unbounded behaviour
    (handy for tests).
    """
    config = {"configurable": {"thread_id": f"eval-{case.id}-{uuid.uuid4().hex[:8]}"}}
    initial = _initial_state(case)

    t0 = time.perf_counter()
    try:
        invocation = graph.ainvoke(initial, config=config)
        if timeout_s is not None:
            state = await asyncio.wait_for(invocation, timeout=timeout_s)
        else:
            state = await invocation
        # Week 7: if the agent paused at the HITL gate, auto-approve so
        # the rest of the pipeline runs and the case is gradable. The
        # pause behaviour itself is verified by ``tests/test_risk.py``;
        # the eval set's job is to grade the *output*, not the gating
        # decision. Re-applies the same per-case wall budget so a slow
        # resume cannot evade the timeout.
        while state.get("__interrupt__"):
            log.info("case %s paused at HITL gate; auto-approving", case.id)
            resume_call = graph.ainvoke(Command(resume="approve"), config=config)
            if timeout_s is not None:
                state = await asyncio.wait_for(resume_call, timeout=timeout_s)
            else:
                state = await resume_call
    except TimeoutError:
        elapsed = (time.perf_counter() - t0) * 1000
        log.warning("case %s timed out after %.1fs", case.id, timeout_s)
        return RunResult(
            sql=None,
            answer=f"<timeout after {timeout_s:.1f}s>",
            rows=None,
            row_count=None,
            error=f"runner_timeout: exceeded {timeout_s:.1f}s",
            attempts=0,
            latency_ms=elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        log.exception("case %s crashed: %s", case.id, exc)
        return RunResult(
            sql=None,
            answer=f"<crashed: {exc}>",
            rows=None,
            row_count=None,
            error=f"runner_crash: {exc}",
            attempts=0,
            latency_ms=elapsed,
        )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    failures = state.get("attempts") or []
    turn_idx = state.get("turn_index") or 1
    this_turn = [f for f in failures if f.get("turn_idx", 0) == turn_idx]
    if not state.get("sql"):
        attempts_count = 0
    elif state.get("error"):
        attempts_count = len(this_turn)
    else:
        attempts_count = len(this_turn) + 1

    coverage = state.get("coverage") or {}
    return RunResult(
        sql=state.get("sql"),
        answer=state.get("answer", ""),
        rows=state.get("sql_result"),
        row_count=state.get("row_count"),
        error=state.get("error"),
        attempts=attempts_count,
        latency_ms=elapsed_ms,
        total_tokens=_estimate_tokens_from_messages(state),
        intent=state.get("intent"),
        coverage_verdict=coverage.get("verdict"),
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def run_eval(
    cases: list[CaseSpec],
    cfg: ExperimentConfig,
    *,
    graph: Any | None = None,
    case_timeout_s: float | None = None,
) -> ExperimentResult:
    """Run every case under the given experiment config.

    Args:
        cases: list of CaseSpec to evaluate.
        cfg: which feature flags to flip (passed through ``feature_flags.override``).
        graph: pre-built graph (for tests). When ``None``, a fresh graph
            with an InMemorySaver is built — this is the normal path.
        case_timeout_s: optional per-case wall-clock budget. Cases that
            exceed it are recorded as ``runner_timeout`` failures and the
            run continues. ``None`` keeps the legacy unbounded behaviour.

    Returns:
        ExperimentResult with per-case outcomes and aggregates.
    """
    g = graph if graph is not None else build_graph(checkpointer=InMemorySaver())
    result = ExperimentResult(config=cfg)

    log.info("running %d cases under config=%s", len(cases), cfg.label)
    with feature_flags.override(
        schema_rag_enabled=cfg.schema_rag_enabled,
        dialogue_context_enabled=cfg.dialogue_context_enabled,
        retry_budget=cfg.retry_budget_override,
        analyst_enabled=cfg.analyst_enabled,
        coverage_check_enabled=cfg.coverage_check_enabled,
    ):
        for case in cases:
            log.info("  [%s] %s — %s", cfg.label, case.id, case.category)
            run = await _invoke_case(case, g, timeout_s=case_timeout_s)
            grade_report = grade(case, run)
            result.outcomes.append(CaseOutcome(case=case, run=run, grade=grade_report))

    log.info(
        "%s done: %d/%d passed (%.1f%%), avg_attempts=%.2f, avg_latency_ms=%.0f",
        cfg.label,
        result.passed,
        result.total,
        result.success_rate * 100,
        result.avg_attempts,
        result.avg_latency_ms,
    )
    return result


def run_eval_sync(
    cases: list[CaseSpec], cfg: ExperimentConfig, *, graph: Any | None = None
) -> ExperimentResult:
    """Convenience for CLI / scripts that don't want to manage an event loop."""
    return asyncio.run(run_eval(cases, cfg, graph=graph))


def load_default_cases(path: str | Path | None = None) -> list[CaseSpec]:
    """Load cases.yaml from the repo's standard location."""
    from copilot.eval.cases import DEFAULT_CASES_PATH

    return load_cases(path or DEFAULT_CASES_PATH)
