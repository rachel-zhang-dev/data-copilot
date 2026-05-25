"""A4: Analyst agent on vs off (week 12.5).

Hypothesis: the week-12.5 Analyst agent adds **observation quality**
(follow-up suggestions, anomaly callouts, optional drill-downs) at
the cost of ~250-500 extra tokens per data turn. ``success_rate`` is
expected to stay flat because the Analyst is additive — it doesn't
gate the SQL answer.

The baseline / treatment naming follows the other three A/Bs:
"baseline" = the older behaviour (no Analyst), "treatment" = the
production default. ``avg_total_tokens`` and ``avg_latency_ms`` are
the metrics that should move.
"""

from __future__ import annotations

from copilot.eval.cases import CaseSpec
from copilot.eval.config import BASELINE_FULL, WITHOUT_ANALYST
from copilot.eval.experiments._common import Comparison, run_ab


async def run_analyst_ab(
    cases: list[CaseSpec], *, case_timeout_s: float | None = None
) -> Comparison:
    return await run_ab(
        "analyst",
        cases,
        baseline=WITHOUT_ANALYST,
        treatment=BASELINE_FULL,
        case_timeout_s=case_timeout_s,
    )
