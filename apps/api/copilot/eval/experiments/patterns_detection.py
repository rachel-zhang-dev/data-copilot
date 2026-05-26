"""A6: statistical pattern detection on vs off (Phase 1.2 / ADR 0017).

Hypothesis: enabling the detector dramatically improves the
``has_pattern`` category (the detector either fires or it doesn't) and
adds a small but visible "more useful" bump on `aggregation` /
`join` categories whose result sets carry an outlier or trend the
LLM would otherwise miss. Plain count / chitchat / KPI categories
should stay completely flat — the detector short-circuits on those.

Baseline = "patterns_detection off" (pre-Phase-1.2 behaviour);
treatment = production default.
"""

from __future__ import annotations

from copilot.eval.cases import CaseSpec
from copilot.eval.config import BASELINE_FULL, WITHOUT_PATTERNS_DETECTION
from copilot.eval.experiments._common import Comparison, run_ab


async def run_patterns_detection_ab(
    cases: list[CaseSpec], *, case_timeout_s: float | None = None
) -> Comparison:
    return await run_ab(
        "patterns_detection",
        cases,
        baseline=WITHOUT_PATTERNS_DETECTION,
        treatment=BASELINE_FULL,
        case_timeout_s=case_timeout_s,
    )
