"""A5: schema coverage gate on vs off (Phase 1.1 / ADR 0016).

Hypothesis: enabling the gate dramatically improves accuracy on
``unanswerable`` and ``schema_explore`` cases (the agent stops
hallucinating SQL it can't justify) while leaving the original 32
cases untouched (``success_rate`` on data categories should be flat).

Baseline = "coverage_check off" (pre-Phase-1.1 behaviour); treatment
= production default. The metric that should move the most is
``by_category()`` for the two new buckets — both go from near-0%
under baseline to near-100% under treatment.
"""

from __future__ import annotations

from copilot.eval.cases import CaseSpec
from copilot.eval.config import BASELINE_FULL, WITHOUT_COVERAGE_CHECK
from copilot.eval.experiments._common import Comparison, run_ab


async def run_coverage_check_ab(
    cases: list[CaseSpec], *, case_timeout_s: float | None = None
) -> Comparison:
    return await run_ab(
        "coverage_check",
        cases,
        baseline=WITHOUT_COVERAGE_CHECK,
        treatment=BASELINE_FULL,
        case_timeout_s=case_timeout_s,
    )
