"""A7: investigate-mode hop budget on vs off (Phase 1.3 / ADR 0018).

Hypothesis: bumping ``HOP_BUDGETS["investigate"]`` from 2 to 6 lets
the analyst chain multiple drill-downs on open-ended research
questions ("why is X declining", "deep dive into Y") and noticeably
improves their resolution. Plain ``data`` questions are unaffected —
their budget stays at 2 in both arms.

Baseline = "investigate_mode off" (legacy 2-hop ceiling for every
intent); treatment = production default (6 for investigate).

Note: ``drill_count`` is only observable in the run result if the
eval runner is wired through the supervisor graph; today it uses the
SQL Specialist directly so the field stays at 0 across both arms.
This experiment ships so the harness contract is complete; the
runner migration is tracked in ADR 0018 §"Eval gap".
"""

from __future__ import annotations

from copilot.eval.cases import CaseSpec
from copilot.eval.config import BASELINE_FULL, WITHOUT_INVESTIGATE_MODE
from copilot.eval.experiments._common import Comparison, run_ab


async def run_investigate_mode_ab(
    cases: list[CaseSpec], *, case_timeout_s: float | None = None
) -> Comparison:
    return await run_ab(
        "investigate_mode",
        cases,
        baseline=WITHOUT_INVESTIGATE_MODE,
        treatment=BASELINE_FULL,
        case_timeout_s=case_timeout_s,
    )
