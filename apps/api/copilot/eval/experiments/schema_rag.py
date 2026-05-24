"""A1: schema RAG on vs off.

Hypothesis: schema_rag improves SQL quality on JOIN-class questions
(where the FK-expansion bridge is non-obvious to the LLM) without
hurting simple-count / single-table cases. It also reduces token
cost because the LLM no longer sees the full DDL.

We run the full case set so the per-category breakdown can confirm
the "doesn't hurt simple cases" claim alongside the "helps JOINs"
claim.
"""

from __future__ import annotations

from copilot.eval.cases import CaseSpec
from copilot.eval.config import BASELINE_FULL, WITHOUT_SCHEMA_RAG
from copilot.eval.experiments._common import Comparison, run_ab


async def run_schema_rag_ab(
    cases: list[CaseSpec], *, case_timeout_s: float | None = None
) -> Comparison:
    return await run_ab(
        "schema_rag",
        cases,
        baseline=WITHOUT_SCHEMA_RAG,  # "off" is baseline (week-2 behaviour)
        treatment=BASELINE_FULL,  # "on" is treatment (week-3 default)
        case_timeout_s=case_timeout_s,
    )
