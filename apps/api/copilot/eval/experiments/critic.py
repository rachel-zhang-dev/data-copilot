"""A8: SQL verification loop / critic on vs off (Phase 2.3 / ADR 0021).

Hypothesis: enabling the critic dramatically improves accuracy on
``semantic_trap`` cases (questions where the LLM's first SQL
syntactically works but semantically misses — wrong JOIN direction,
missing filter, wrong aggregation grain). Everything else should
stay flat modulo one extra LLM call's worth of cost / latency.

Baseline = "critic off" (the pre-Phase-2.3 behaviour: execute_sql
goes straight to summarize_result); treatment = production default
with the critic node in the loop.

The metric that should move the most is ``by_category()`` for the
``semantic_trap`` bucket — baseline shows near-0% (the agent
happily returns the wrong-but-plausible answer), treatment shows
high-percent (the critic catches it and either fixes via the
retry budget or downgrades to a ⚠️ low-confidence answer).
"""

from __future__ import annotations

from copilot.eval.cases import CaseSpec
from copilot.eval.config import BASELINE_FULL, WITHOUT_CRITIC
from copilot.eval.experiments._common import Comparison, run_ab


async def run_critic_ab(
    cases: list[CaseSpec], *, case_timeout_s: float | None = None
) -> Comparison:
    return await run_ab(
        "critic",
        cases,
        baseline=WITHOUT_CRITIC,
        treatment=BASELINE_FULL,
        case_timeout_s=case_timeout_s,
    )
