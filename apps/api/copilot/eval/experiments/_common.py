"""Shared scaffolding for all three A/B experiments.

Each experiment is essentially the same shape — run baseline, run
treatment, package the pair as a ``Comparison``. Concentrating that
plumbing here keeps each experiment file down to a dozen lines of
"what's actually being compared".
"""

from __future__ import annotations

from dataclasses import dataclass

from copilot.eval.cases import CaseSpec
from copilot.eval.config import ExperimentConfig
from copilot.eval.runner import ExperimentResult, run_eval


@dataclass
class Comparison:
    """Two ``ExperimentResult`` runs, ready for a delta table."""

    name: str
    baseline: ExperimentResult
    treatment: ExperimentResult

    @property
    def success_rate_delta(self) -> float:
        return self.treatment.success_rate - self.baseline.success_rate

    @property
    def avg_attempts_delta(self) -> float:
        return self.treatment.avg_attempts - self.baseline.avg_attempts

    @property
    def avg_latency_ms_delta(self) -> float:
        return self.treatment.avg_latency_ms - self.baseline.avg_latency_ms

    @property
    def avg_total_tokens_delta(self) -> float:
        return self.treatment.avg_total_tokens - self.baseline.avg_total_tokens


async def run_ab(
    name: str,
    cases: list[CaseSpec],
    *,
    baseline: ExperimentConfig,
    treatment: ExperimentConfig,
    cases_filter: str | None = None,
) -> Comparison:
    """Run baseline + treatment and pair them.

    ``cases_filter`` is a category name; when set, only cases of that
    category are evaluated. The dialogue-context experiment uses this
    to focus on follow-up cases (where the flag actually matters).
    """
    selected = cases if cases_filter is None else [c for c in cases if c.category == cases_filter]
    base = await run_eval(selected, baseline)
    treat = await run_eval(selected, treatment)
    return Comparison(name=name, baseline=base, treatment=treat)
