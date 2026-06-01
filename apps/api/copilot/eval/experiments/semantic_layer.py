"""A9: deterministic semantic layer on vs off (Phase 3.1 / ADR 0023).

Hypothesis: turning the semantic layer ON improves accuracy on
questions whose metric + dimensions are modelled in
``data/semantic.yml`` (mostly the ``aggregation``, ``count``,
``join``, and simple ``single_table_filter`` categories). It should
NOT change accuracy on uncovered categories (``follow_up`` because
the router declines, ``investigate`` because the budget belongs to
the supervisor, ``has_pattern`` / ``schema_explore`` because the
router defers to the existing path), and should add ~+1 LLM call
worth of cost / latency per turn from the router's classification.

Baseline = "semantic_layer off" (pre-Phase-3.1 behaviour); treatment
= production default with router + resolver active.
"""

from __future__ import annotations

from copilot.eval.cases import CaseSpec
from copilot.eval.config import BASELINE_FULL, WITHOUT_SEMANTIC_LAYER
from copilot.eval.experiments._common import Comparison, run_ab


async def run_semantic_layer_ab(
    cases: list[CaseSpec], *, case_timeout_s: float | None = None
) -> Comparison:
    return await run_ab(
        "semantic_layer",
        cases,
        baseline=WITHOUT_SEMANTIC_LAYER,
        treatment=BASELINE_FULL,
        case_timeout_s=case_timeout_s,
    )
