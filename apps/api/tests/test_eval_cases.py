"""Unit tests for the eval cases loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from copilot.eval.cases import (
    DEFAULT_CASES_PATH,
    CaseSpec,
    Expect,
    HistoryTurn,
    RowCountRange,
    load_cases,
)


def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "cases.yaml"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_loads_minimal_count_case(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: x
  question: How many?
  category: count
  expects:
    sql_must_contain: [count]
""",
    )
    cases = load_cases(p)
    assert len(cases) == 1
    c = cases[0]
    assert isinstance(c, CaseSpec)
    assert c.id == "x"
    assert c.category == "count"
    assert c.expects.sql_must_contain == ("count",)


def test_real_cases_yaml_loads_and_has_all_categories() -> None:
    cases = load_cases(DEFAULT_CASES_PATH)
    assert len(cases) >= 20
    cats = {c.category for c in cases}
    assert {
        "count",
        "single_table_filter",
        "aggregation",
        "join",
        "follow_up",
        "chitchat",
        "destructive",
        "ambiguous",
        "expensive",
    } <= cats


def test_row_count_range_parsing(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: x
  question: q
  category: count
  expects:
    sql_must_contain: [a]
    row_count: { min: 1, max: 5 }
""",
    )
    case = load_cases(p)[0]
    assert case.expects.row_count == RowCountRange(1, 5)
    assert case.expects.row_count.contains(3) is True
    assert case.expects.row_count.contains(0) is False


def test_chitchat_with_sql_must_be_absent(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: x
  question: hi
  category: chitchat
  expects:
    sql_must_be_absent: true
""",
    )
    case = load_cases(p)[0]
    assert case.expects.sql_must_be_absent is True


def test_followup_setup_history_parsed(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: f
  question: And France?
  category: follow_up
  setup_history:
    - role: user
      content: Germany?
    - role: assistant
      content: 11
      sql: SELECT count(*) FROM customers
  expects:
    sql_must_contain: [customers]
""",
    )
    case = load_cases(p)[0]
    assert len(case.setup_history) == 2
    assert case.setup_history[0] == HistoryTurn(role="user", content="Germany?", sql=None)
    assert case.setup_history[1].sql is not None


# ---------------------------------------------------------------------------
# Error paths — strict loader
# ---------------------------------------------------------------------------


def test_unknown_category_rejected(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: x
  question: q
  category: martian
  expects:
    sql_must_contain: [a]
""",
    )
    with pytest.raises(ValueError, match="unknown category"):
        load_cases(p)


def test_unknown_expect_key_rejected(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: x
  question: q
  category: count
  expects:
    sql_must_kontain: [a]
""",
    )
    with pytest.raises(ValueError, match="unknown expect keys"):
        load_cases(p)


def test_empty_expects_rejected(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: x
  question: q
  category: count
  expects: {}
""",
    )
    with pytest.raises(ValueError, match="no assertions"):
        load_cases(p)


def test_followup_without_history_rejected(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: f
  question: q
  category: follow_up
  expects:
    sql_must_contain: [a]
""",
    )
    with pytest.raises(ValueError, match="follow_up"):
        load_cases(p)


def test_duplicate_ids_rejected(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: x
  question: a
  category: count
  expects:
    sql_must_contain: [a]
- id: x
  question: b
  category: count
  expects:
    sql_must_contain: [a]
""",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(p)


def test_invalid_regex_rejected(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: x
  question: q
  category: count
  expects:
    answer_must_match: '['
""",
    )
    with pytest.raises(ValueError, match="not valid regex"):
        load_cases(p)


def test_row_count_min_gt_max_rejected(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: x
  question: q
  category: count
  expects:
    row_count: { min: 5, max: 1 }
""",
    )
    with pytest.raises(ValueError, match=r"min .* > max"):
        load_cases(p)


def test_empty_yaml_returns_empty_list(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path, "")
    assert load_cases(p) == []


def test_duplicate_yaml_keys_rejected(tmp_path: Path) -> None:
    """YAML's default 'last value wins' silently corrupted one of the
    real cases (followup-only-the-discontinued had two `content:` keys
    on the same user turn). The strict loader must refuse that shape."""
    p = _write_yaml(
        tmp_path,
        """
- id: dup
  question: q
  category: follow_up
  setup_history:
    - role: user
      content: First
      content: Second
  expects:
    sql_must_contain: [a]
""",
    )
    with pytest.raises(ValueError, match="duplicate key"):
        load_cases(p)


def test_duplicate_top_level_keys_rejected(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
- id: x
  question: q
  question: q2
  category: count
  expects:
    sql_must_contain: [a]
""",
    )
    with pytest.raises(ValueError, match="duplicate key"):
        load_cases(p)


def test_expect_immutable() -> None:
    e = Expect(sql_must_contain=("a",))
    with pytest.raises(Exception):  # noqa: B017 — frozen=True raises FrozenInstanceError
        e.sql_must_contain = ("b",)  # type: ignore[misc]
