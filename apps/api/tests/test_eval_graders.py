"""Unit tests for the deterministic grader."""

from __future__ import annotations

from copilot.eval.cases import CaseSpec, Expect, RowCountRange
from copilot.eval.graders.deterministic import RunResult, grade


def _case(expects: Expect, *, category: str = "count") -> CaseSpec:
    return CaseSpec(
        id="t",
        question="q",
        category=category,  # type: ignore[arg-type]
        expects=expects,
    )


def _run(**kwargs) -> RunResult:  # type: ignore[no-untyped-def]
    defaults: dict[str, object] = {
        "sql": None,
        "answer": "",
        "rows": None,
        "row_count": None,
        "error": None,
        "attempts": 1,
        "latency_ms": 100.0,
    }
    return RunResult(**{**defaults, **kwargs})  # type: ignore[arg-type]


# expected_verdict / expected_intent (Phase 1.1) ----------------------------


def test_expected_verdict_passes_when_matching() -> None:
    case = _case(Expect(expected_verdict="refuse"), category="unanswerable")
    run = _run(coverage_verdict="refuse")
    assert grade(case, run).passed


def test_expected_verdict_fails_when_mismatched() -> None:
    case = _case(Expect(expected_verdict="refuse"), category="unanswerable")
    run = _run(coverage_verdict="ok")
    g = grade(case, run)
    assert not g.passed
    assert any("verdict" in c.name for c in g.checks)


def test_expected_verdict_skipped_when_none() -> None:
    # No verdict expected → the field is silently ignored, no check
    # gets appended, the existing assertions decide.
    case = _case(Expect(sql_must_contain=("customers",)))
    run = _run(sql="SELECT * FROM customers", coverage_verdict="refuse")
    g = grade(case, run)
    assert g.passed
    assert all("verdict" not in c.name for c in g.checks)


def test_expected_intent_passes_when_matching() -> None:
    case = _case(
        Expect(expected_intent="schema_explore"), category="schema_explore"
    )
    run = _run(intent="schema_explore", coverage_verdict="explore")
    assert grade(case, run).passed


def test_expected_intent_fails_on_wrong_route() -> None:
    case = _case(
        Expect(expected_intent="schema_explore"), category="schema_explore"
    )
    run = _run(intent="data")
    g = grade(case, run)
    assert not g.passed
    assert any("intent" in c.name for c in g.checks)


# sql_must_contain ----------------------------------------------------------


def test_sql_must_contain_passes_when_all_present() -> None:
    case = _case(Expect(sql_must_contain=("customers", "count")))
    run = _run(sql="SELECT COUNT(*) FROM customers")
    g = grade(case, run)
    assert g.passed


def test_sql_must_contain_fails_on_missing() -> None:
    case = _case(Expect(sql_must_contain=("orders",)))
    run = _run(sql="SELECT * FROM customers")
    g = grade(case, run)
    assert not g.passed
    assert any("orders" in r for r in g.fail_reasons())


def test_sql_must_contain_is_case_insensitive() -> None:
    case = _case(Expect(sql_must_contain=("CUSTOMERS",)))
    run = _run(sql="select * from customers")
    g = grade(case, run)
    assert g.passed


# sql_must_not_contain ------------------------------------------------------


def test_sql_must_not_contain_fails_when_present() -> None:
    case = _case(Expect(sql_must_not_contain=("shippers",)))
    run = _run(sql="SELECT * FROM customers JOIN shippers")
    g = grade(case, run)
    assert not g.passed


# sql_should_contain_any ----------------------------------------------------


def test_sql_should_contain_any_passes_on_one_match() -> None:
    case = _case(Expect(sql_should_contain_any=("count", "sum")))
    run = _run(sql="SELECT count(*) FROM x")
    g = grade(case, run)
    assert g.passed


def test_sql_should_contain_any_fails_when_none_match() -> None:
    case = _case(Expect(sql_should_contain_any=("count", "sum")))
    run = _run(sql="SELECT name FROM x")
    g = grade(case, run)
    assert not g.passed


# sql_must_be_absent --------------------------------------------------------


def test_sql_must_be_absent_passes_when_no_sql() -> None:
    case = _case(Expect(sql_must_be_absent=True), category="chitchat")
    run = _run(sql=None, answer="Hi!")
    g = grade(case, run)
    assert g.passed


def test_sql_must_be_absent_fails_when_sql_present() -> None:
    case = _case(Expect(sql_must_be_absent=True), category="chitchat")
    run = _run(sql="SELECT 1", answer="Hi")
    g = grade(case, run)
    assert not g.passed


# answer_must_match ---------------------------------------------------------


def test_answer_must_match_passes_on_regex_hit() -> None:
    case = _case(Expect(answer_must_match=r"\b\d+\b"))
    run = _run(sql="x", answer="There are 91 customers")
    g = grade(case, run)
    assert g.passed


def test_answer_must_match_fails_on_no_match() -> None:
    case = _case(Expect(answer_must_match=r"\b\d+\b"))
    run = _run(sql="x", answer="There are no numbers here")
    g = grade(case, run)
    assert not g.passed


# answer_must_contain_any ---------------------------------------------------


def test_answer_must_contain_any_passes_case_insensitively() -> None:
    case = _case(Expect(answer_must_contain_any=("read-only", "cannot")))
    run = _run(sql=None, answer="This is read-only.")
    g = grade(case, run)
    assert g.passed


# row_count -----------------------------------------------------------------


def test_row_count_in_range_passes() -> None:
    case = _case(Expect(row_count=RowCountRange(1, 5)))
    run = _run(sql="x", row_count=3)
    g = grade(case, run)
    assert g.passed


def test_row_count_below_range_fails() -> None:
    case = _case(Expect(row_count=RowCountRange(1, 5)))
    run = _run(sql="x", row_count=0)
    g = grade(case, run)
    assert not g.passed


def test_row_count_none_treated_as_zero() -> None:
    case = _case(Expect(row_count=RowCountRange(1, 5)))
    run = _run(sql="x", row_count=None)
    g = grade(case, run)
    assert not g.passed


# combined ------------------------------------------------------------------


def test_all_assertions_must_pass() -> None:
    case = _case(
        Expect(
            sql_must_contain=("customers",),
            row_count=RowCountRange(1, 1),
            answer_must_match=r"\d+",
        )
    )
    # Two pass, one fails (row_count out of range)
    run = _run(sql="SELECT count(*) FROM customers", row_count=99, answer="91")
    g = grade(case, run)
    assert not g.passed
    # Three checks total; row_count one failed
    assert sum(1 for c in g.checks if c.passed) == 2
    assert sum(1 for c in g.checks if not c.passed) == 1


def test_no_assertions_yields_passed_true() -> None:
    """Empty Expect (no fields) shouldn't happen via loader, but the
    grader should be robust if it does."""
    case = _case(Expect())
    run = _run()
    g = grade(case, run)
    assert g.passed  # vacuously true
