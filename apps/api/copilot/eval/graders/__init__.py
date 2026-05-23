"""Graders convert (case, run) -> (pass/fail, reason)."""

from copilot.eval.graders.deterministic import GradeReport, grade

__all__ = ["GradeReport", "grade"]
