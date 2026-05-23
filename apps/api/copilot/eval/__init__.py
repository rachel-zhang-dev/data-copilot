"""Evaluation harness (week 6).

Tools for running the agent on a fixed question set and measuring
quality, cost, and latency. Lives outside the request path; only the
``runner`` and ``experiments`` modules are entry points.
"""

from copilot.eval.cases import (
    DEFAULT_CASES_PATH,
    CaseSpec,
    Category,
    Expect,
    HistoryTurn,
    RowCountRange,
    load_cases,
)
from copilot.eval.config import (
    BASELINE_FULL,
    WITHOUT_DIALOGUE_CONTEXT,
    WITHOUT_SCHEMA_RAG,
    WITHOUT_SELF_HEALING,
    ExperimentConfig,
)

__all__ = [
    "BASELINE_FULL",
    "DEFAULT_CASES_PATH",
    "WITHOUT_DIALOGUE_CONTEXT",
    "WITHOUT_SCHEMA_RAG",
    "WITHOUT_SELF_HEALING",
    "CaseSpec",
    "Category",
    "Expect",
    "ExperimentConfig",
    "HistoryTurn",
    "RowCountRange",
    "load_cases",
]
