"""Eval case schema + YAML loader.

Cases are read from ``data/eval/cases.yaml`` and parsed into
``CaseSpec`` instances. The loader is intentionally strict — unknown
keys, unknown categories, or invalid expectation shapes raise so
typos can never silently downgrade an experiment to a no-op.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

import yaml


class _StrictLoader(yaml.SafeLoader):  # type: ignore[misc]
    """``SafeLoader`` that refuses to silently drop duplicate mapping keys.

    PyYAML's default behaviour is "last value wins", which let
    ``followup-only-the-discontinued`` ship with two ``content:`` lines
    in its user turn (one was silently discarded). We never want a typo
    in cases.yaml to half-corrupt an eval case while still loading
    cleanly — that risks misleading A/B numbers.

    The ``type: ignore`` is needed because PyYAML ships no type stubs
    so ``SafeLoader`` lands as ``Any`` under strict mypy.
    """


def _no_duplicate_keys(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"duplicate key {key!r} in mapping",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _no_duplicate_keys,
)

Category = Literal[
    "count",
    "single_table_filter",
    "aggregation",
    "join",
    "follow_up",
    "chitchat",
    "destructive",
    "ambiguous",
    "expensive",
]
"""The nine buckets we slice metrics by. Adding a new bucket means
both updating this Literal AND adding cases to cases.yaml.

``expensive`` (week 7) covers questions that should produce SQL with a
Postgres planner cost above the HITL threshold. The eval auto-approves
the resulting pause so the rest of the pipeline can be graded; the
pause behaviour itself is unit-tested in ``tests/test_risk.py``."""

_CATEGORIES: set[str] = set(get_args(Category))


@dataclass(frozen=True)
class RowCountRange:
    """Inclusive [min, max] bound on the number of rows the SQL
    should return. Used by the deterministic grader."""

    min: int
    max: int

    def contains(self, n: int) -> bool:
        return self.min <= n <= self.max


@dataclass(frozen=True)
class HistoryTurn:
    """One pre-baked dialogue entry for ``follow_up`` cases. The runner
    seeds the graph state with these so the agent sees prior context
    without having to actually re-execute earlier turns (which would
    cost LLM tokens and add flakiness)."""

    role: Literal["user", "assistant"]
    content: str
    sql: str | None = None


@dataclass(frozen=True)
class Expect:
    """Assertions against an agent run.

    All fields are AND-ed. ``sql_must_contain`` and
    ``answer_must_contain_any`` are case-insensitive. Regex patterns
    in ``answer_must_match`` are matched with ``re.search`` (not
    fullmatch) so partial matches count.
    """

    sql_must_contain: tuple[str, ...] = ()
    sql_must_not_contain: tuple[str, ...] = ()
    sql_should_contain_any: tuple[str, ...] = ()
    sql_must_be_absent: bool = False
    answer_must_match: str | None = None
    answer_must_contain_any: tuple[str, ...] = ()
    row_count: RowCountRange | None = None


@dataclass(frozen=True)
class CaseSpec:
    """One eval case."""

    id: str
    question: str
    category: Category
    expects: Expect
    setup_history: tuple[HistoryTurn, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# YAML parsing — strict so typos fail loud
# ---------------------------------------------------------------------------


_VALID_EXPECT_KEYS = {
    "sql_must_contain",
    "sql_must_not_contain",
    "sql_should_contain_any",
    "sql_must_be_absent",
    "answer_must_match",
    "answer_must_contain_any",
    "row_count",
}


def _parse_expect(raw: dict[str, Any], case_id: str) -> Expect:
    unknown = set(raw) - _VALID_EXPECT_KEYS
    if unknown:
        raise ValueError(
            f"case {case_id!r}: unknown expect keys {sorted(unknown)} "
            f"(valid: {sorted(_VALID_EXPECT_KEYS)})"
        )

    row_count = raw.get("row_count")
    rc: RowCountRange | None = None
    if row_count is not None:
        if not isinstance(row_count, dict) or "min" not in row_count or "max" not in row_count:
            raise ValueError(
                f"case {case_id!r}: row_count must be a dict with 'min' and 'max'"
            )
        rc = RowCountRange(min=int(row_count["min"]), max=int(row_count["max"]))
        if rc.min > rc.max:
            raise ValueError(
                f"case {case_id!r}: row_count min ({rc.min}) > max ({rc.max})"
            )

    answer_match = raw.get("answer_must_match")
    if answer_match is not None:
        try:
            re.compile(answer_match)
        except re.error as exc:
            raise ValueError(
                f"case {case_id!r}: answer_must_match is not valid regex: {exc}"
            ) from exc

    expect = Expect(
        sql_must_contain=tuple(raw.get("sql_must_contain", []) or []),
        sql_must_not_contain=tuple(raw.get("sql_must_not_contain", []) or []),
        sql_should_contain_any=tuple(raw.get("sql_should_contain_any", []) or []),
        sql_must_be_absent=bool(raw.get("sql_must_be_absent", False)),
        answer_must_match=answer_match,
        answer_must_contain_any=tuple(raw.get("answer_must_contain_any", []) or []),
        row_count=rc,
    )

    has_any = (
        expect.sql_must_contain
        or expect.sql_must_not_contain
        or expect.sql_should_contain_any
        or expect.sql_must_be_absent
        or expect.answer_must_match
        or expect.answer_must_contain_any
        or expect.row_count is not None
    )
    if not has_any:
        raise ValueError(
            f"case {case_id!r}: expects has no assertions; "
            "add at least one or set sql_must_be_absent: true"
        )
    return expect


def _parse_history_turn(raw: dict[str, Any], case_id: str, idx: int) -> HistoryTurn:
    role = raw.get("role")
    if role not in ("user", "assistant"):
        raise ValueError(
            f"case {case_id!r}: setup_history[{idx}].role must be 'user' or "
            f"'assistant', got {role!r}"
        )
    return HistoryTurn(
        role=role,
        content=str(raw.get("content", "")),
        sql=raw.get("sql"),
    )


def _parse_case(raw: dict[str, Any]) -> CaseSpec:
    case_id = raw.get("id")
    if not case_id or not isinstance(case_id, str):
        raise ValueError(f"case missing or non-string 'id': {raw!r}")

    cat = raw.get("category")
    if cat not in _CATEGORIES:
        raise ValueError(
            f"case {case_id!r}: unknown category {cat!r} "
            f"(valid: {sorted(_CATEGORIES)})"
        )

    question = raw.get("question")
    if not question or not isinstance(question, str):
        raise ValueError(f"case {case_id!r}: missing or empty 'question'")

    expects_raw = raw.get("expects")
    if not isinstance(expects_raw, dict):
        raise ValueError(f"case {case_id!r}: 'expects' must be a dict")
    expects = _parse_expect(expects_raw, case_id)

    history_raw = raw.get("setup_history") or []
    if not isinstance(history_raw, list):
        raise ValueError(f"case {case_id!r}: setup_history must be a list")
    history = tuple(
        _parse_history_turn(h, case_id, i) for i, h in enumerate(history_raw)
    )

    if cat == "follow_up" and not history:
        raise ValueError(
            f"case {case_id!r}: follow_up category requires non-empty setup_history"
        )

    return CaseSpec(
        id=case_id,
        question=question,
        category=cat,
        expects=expects,
        setup_history=history,
    )


def load_cases(path: str | Path) -> list[CaseSpec]:
    """Read a YAML cases file and return parsed ``CaseSpec`` instances.

    Raises:
        FileNotFoundError: path does not exist.
        ValueError: duplicate IDs, invalid category / expect, etc.
    """
    p = Path(path)
    try:
        raw = yaml.load(p.read_text(), Loader=_StrictLoader)
    except yaml.constructor.ConstructorError as exc:
        # Surface duplicate-key errors with the same ValueError shape as
        # every other strictness violation, so callers only catch one type.
        raise ValueError(f"{p}: {exc.problem} ({exc.problem_mark})") from exc
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{p}: top-level YAML must be a list of cases")

    cases = [_parse_case(item) for item in raw]

    seen: set[str] = set()
    for c in cases:
        if c.id in seen:
            raise ValueError(f"duplicate case id: {c.id!r}")
        seen.add(c.id)

    return cases


DEFAULT_CASES_PATH = Path(__file__).resolve().parents[3].parent / "data" / "eval" / "cases.yaml"
"""Where ``cases.yaml`` lives relative to this file. Used as the
default by ``runner.run_eval`` when no path is supplied."""
