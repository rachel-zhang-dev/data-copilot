"""Deterministic grader.

Converts a ``(CaseSpec, RunResult)`` pair into a pass/fail verdict by
running each ``Expect`` assertion. No LLM, no DB, no network — runs
in microseconds and is fully reproducible.

Why deterministic-first
-----------------------
A SQL that doesn't even mention the right table will fail this layer,
and we don't waste an LLM-judge call on it. The optional LLM judge
only kicks in when deterministic checks pass but we still want to
score answer fluency / numeric correctness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from copilot.eval.cases import CaseSpec, Expect


@dataclass(frozen=True)
class CheckResult:
    """One assertion's outcome — name + pass/fail + optional detail."""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class GradeReport:
    """Aggregate of all assertions for a single case."""

    case_id: str
    passed: bool
    checks: tuple[CheckResult, ...] = field(default_factory=tuple)

    def fail_reasons(self) -> list[str]:
        """One-line summaries of every assertion that failed; useful
        for the markdown report's failure-sample section."""
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.passed]


@dataclass
class RunResult:
    """The slice of agent output the grader inspects.

    We deliberately do not give the grader the whole ``AgentState`` —
    coupling it tightly to LangGraph internals would make the grader
    brittle to refactors. Just the user-visible fields.
    """

    sql: str | None
    answer: str
    rows: list[dict[str, Any]] | None
    row_count: int | None
    error: str | None
    attempts: int
    latency_ms: float
    total_tokens: int = 0
    # Phase 1.1 — Coverage gate + intent. Default to ``None`` so the
    # 32 existing cases that don't assert on them stay green without
    # touching every constructor call.
    intent: str | None = None
    coverage_verdict: str | None = None
    # Phase 1.2 / ADR 0017 — list of pattern-detector findings, each
    # a ``{"kind", "column", "severity", "description_key", "payload"}``
    # dict. Empty / ``None`` when no patterns were detected (KPI,
    # constant data, non-numeric result). Default to ``None`` for the
    # same reason as the Phase-1.1 fields.
    patterns: list[dict[str, Any]] | None = None


def _check_sql_must_contain(expect: Expect, run: RunResult) -> list[CheckResult]:
    if not expect.sql_must_contain:
        return []
    sql = (run.sql or "").lower()
    out: list[CheckResult] = []
    for needle in expect.sql_must_contain:
        ok = needle.lower() in sql
        out.append(
            CheckResult(
                f"sql_has({needle!r})",
                ok,
                "" if ok else f"missing in sql={run.sql!r}",
            )
        )
    return out


def _check_sql_must_not_contain(expect: Expect, run: RunResult) -> list[CheckResult]:
    if not expect.sql_must_not_contain:
        return []
    sql = (run.sql or "").lower()
    out: list[CheckResult] = []
    for needle in expect.sql_must_not_contain:
        ok = needle.lower() not in sql
        out.append(
            CheckResult(
                f"sql_lacks({needle!r})",
                ok,
                "" if ok else f"unwanted in sql={run.sql!r}",
            )
        )
    return out


def _check_sql_should_contain_any(expect: Expect, run: RunResult) -> list[CheckResult]:
    if not expect.sql_should_contain_any:
        return []
    sql = (run.sql or "").lower()
    found_any = any(n.lower() in sql for n in expect.sql_should_contain_any)
    return [
        CheckResult(
            f"sql_has_any({list(expect.sql_should_contain_any)})",
            found_any,
            "" if found_any else f"none matched in sql={run.sql!r}",
        )
    ]


def _check_sql_must_be_absent(expect: Expect, run: RunResult) -> list[CheckResult]:
    if not expect.sql_must_be_absent:
        return []
    ok = run.sql is None
    return [
        CheckResult(
            "sql_absent",
            ok,
            "" if ok else f"unexpected sql={run.sql!r}",
        )
    ]


def _check_answer_must_match(expect: Expect, run: RunResult) -> list[CheckResult]:
    if expect.answer_must_match is None:
        return []
    ok = bool(re.search(expect.answer_must_match, run.answer or ""))
    return [
        CheckResult(
            f"answer_re({expect.answer_must_match!r})",
            ok,
            "" if ok else f"no match in answer={run.answer!r}",
        )
    ]


def _check_answer_must_contain_any(expect: Expect, run: RunResult) -> list[CheckResult]:
    if not expect.answer_must_contain_any:
        return []
    answer_lower = (run.answer or "").lower()
    found_any = any(n.lower() in answer_lower for n in expect.answer_must_contain_any)
    return [
        CheckResult(
            f"answer_has_any({list(expect.answer_must_contain_any)})",
            found_any,
            "" if found_any else f"none matched in answer={run.answer!r}",
        )
    ]


def _check_row_count(expect: Expect, run: RunResult) -> list[CheckResult]:
    if expect.row_count is None:
        return []
    rc = run.row_count if run.row_count is not None else 0
    ok = expect.row_count.contains(rc)
    return [
        CheckResult(
            f"row_count_in[{expect.row_count.min},{expect.row_count.max}]",
            ok,
            "" if ok else f"got row_count={rc}",
        )
    ]


def _check_expected_verdict(expect: Expect, run: RunResult) -> list[CheckResult]:
    if expect.expected_verdict is None:
        return []
    actual = run.coverage_verdict
    ok = actual == expect.expected_verdict
    return [
        CheckResult(
            f"verdict({expect.expected_verdict!r})",
            ok,
            "" if ok else f"got coverage_verdict={actual!r}",
        )
    ]


def _check_expected_intent(expect: Expect, run: RunResult) -> list[CheckResult]:
    if expect.expected_intent is None:
        return []
    actual = run.intent
    ok = actual == expect.expected_intent
    return [
        CheckResult(
            f"intent({expect.expected_intent!r})",
            ok,
            "" if ok else f"got intent={actual!r}",
        )
    ]


def _check_pattern_kinds(expect: Expect, run: RunResult) -> list[CheckResult]:
    """Phase 1.2 — assert that every expected pattern kind appears in
    ``run.patterns``. ``expected_pattern_kinds`` is treated as a set,
    not a sequence: ordering doesn't matter, duplicates are ignored."""
    if not expect.expected_pattern_kinds:
        return []
    actual_kinds = {p.get("kind") for p in (run.patterns or [])}
    out: list[CheckResult] = []
    for kind in expect.expected_pattern_kinds:
        ok = kind in actual_kinds
        out.append(
            CheckResult(
                f"pattern_kind({kind!r})",
                ok,
                "" if ok else f"missing in actual kinds={sorted(str(k) for k in actual_kinds)}",
            )
        )
    return out


def _check_pattern_min_count(expect: Expect, run: RunResult) -> list[CheckResult]:
    if expect.expected_pattern_min_count is None:
        return []
    n = len(run.patterns or [])
    ok = n >= expect.expected_pattern_min_count
    return [
        CheckResult(
            f"pattern_min_count(>={expect.expected_pattern_min_count})",
            ok,
            "" if ok else f"got {n} pattern(s)",
        )
    ]


def grade(case: CaseSpec, run: RunResult) -> GradeReport:
    """Run all applicable assertions in sequence; result is AND of all."""
    checks: list[CheckResult] = []
    checks.extend(_check_sql_must_contain(case.expects, run))
    checks.extend(_check_sql_must_not_contain(case.expects, run))
    checks.extend(_check_sql_should_contain_any(case.expects, run))
    checks.extend(_check_sql_must_be_absent(case.expects, run))
    checks.extend(_check_answer_must_match(case.expects, run))
    checks.extend(_check_answer_must_contain_any(case.expects, run))
    checks.extend(_check_row_count(case.expects, run))
    checks.extend(_check_expected_verdict(case.expects, run))
    checks.extend(_check_expected_intent(case.expects, run))
    checks.extend(_check_pattern_kinds(case.expects, run))
    checks.extend(_check_pattern_min_count(case.expects, run))

    overall = all(c.passed for c in checks) if checks else True
    return GradeReport(case_id=case.id, passed=overall, checks=tuple(checks))
