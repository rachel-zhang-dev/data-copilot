"""A2: self-healing retry on vs off.

Hypothesis: self-healing rescues cases where the LLM's first SQL is
syntactically off but recoverable from a Postgres error message.
Cost is at most one extra LLM call per affected case.

Run on the full set so we can see which categories benefit most
(typically ``join`` and ``ambiguous``).
"""

from __future__ import annotations

from copilot.eval.cases import CaseSpec
from copilot.eval.config import BASELINE_FULL, WITHOUT_SELF_HEALING
from copilot.eval.experiments._common import Comparison, run_ab


async def run_self_healing_ab(
    cases: list[CaseSpec], *, case_timeout_s: float | None = None
) -> Comparison:
    return await run_ab(
        "self_healing",
        cases,
        baseline=WITHOUT_SELF_HEALING,
        treatment=BASELINE_FULL,
        case_timeout_s=case_timeout_s,
    )
