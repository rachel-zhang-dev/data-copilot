"""A3: dialogue context on vs off.

Hypothesis: injecting prior turns into ``generate_sql``'s prompt is
critical for follow-up resolution ("And France?" alone is meaningless).

Filtered to the ``follow_up`` category — the flag is irrelevant for
single-shot questions and including them would dilute the signal in
the report.
"""

from __future__ import annotations

from copilot.eval.cases import CaseSpec
from copilot.eval.config import BASELINE_FULL, WITHOUT_DIALOGUE_CONTEXT
from copilot.eval.experiments._common import Comparison, run_ab


async def run_dialogue_context_ab(cases: list[CaseSpec]) -> Comparison:
    return await run_ab(
        "dialogue_context",
        cases,
        baseline=WITHOUT_DIALOGUE_CONTEXT,
        treatment=BASELINE_FULL,
        cases_filter="follow_up",
    )
