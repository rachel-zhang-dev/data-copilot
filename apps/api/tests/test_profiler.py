"""Unit tests for ``copilot.profiler``.

The pure-Python pieces (array parsing, LLM-facing rendering, JSON
serialisation in ``write_profiles``) are tested directly. The SQL-
issuing helpers are exercised against a real Postgres instance only in
``test_agent_integration.py`` — those queries are vendor-specific and
mocking them would be a no-op.
"""

from __future__ import annotations

from typing import Any

import pytest

from copilot import profiler
from copilot.profiler import (
    TABLE_SUMMARY_SENTINEL,
    _format_column_line,
    _parse_pg_array,
    _truncate,
    format_profile_for_llm,
)


# ---------------------------------------------------------------------------
# _parse_pg_array — the only non-trivial piece of string parsing in the file.
# ---------------------------------------------------------------------------


def test_parse_pg_array_handles_plain_braces() -> None:
    assert _parse_pg_array("{a,b,c}") == ["a", "b", "c"]


def test_parse_pg_array_handles_quoted_entries_with_commas() -> None:
    # Postgres double-quotes entries that contain commas, braces, or backslashes.
    assert _parse_pg_array('{"a,b","c"}') == ["a,b", "c"]


def test_parse_pg_array_handles_escaped_quotes() -> None:
    assert _parse_pg_array(r'{"he said \"hi\""}') == ['he said "hi"']


def test_parse_pg_array_empty_inputs() -> None:
    assert _parse_pg_array("") == []
    assert _parse_pg_array("{}") == []


def test_parse_pg_array_preserves_quoted_whitespace_in_realistic_input() -> None:
    # Mirrors actual ``pg_stats.most_common_vals::text`` output: no
    # cosmetic whitespace outside quoted entries, but quoted entries
    # carry their content verbatim including internal spaces.
    assert _parse_pg_array('{"USA","United Kingdom","Germany"}') == [
        "USA",
        "United Kingdom",
        "Germany",
    ]
    # Mixed quoted + bare entries (common when most values are simple
    # ASCII but a few contain commas or spaces).
    assert _parse_pg_array('{42,"hello, world",99}') == [
        "42",
        "hello, world",
        "99",
    ]


# ---------------------------------------------------------------------------
# _truncate — tiny helper but easy to get the off-by-one wrong.
# ---------------------------------------------------------------------------


def test_truncate_short_string_unchanged() -> None:
    assert _truncate("abc", 5) == "abc"


def test_truncate_long_string_gets_ellipsis() -> None:
    out = _truncate("abcdefgh", 5)
    assert out == "abcd…"
    assert len(out) == 5


# ---------------------------------------------------------------------------
# format_profile_for_llm — the prompt-facing rendering.
# ---------------------------------------------------------------------------


def _make_profile(
    table: str,
    *,
    row_count: int | None = 91,
    table_comment: str | None = None,
    columns: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build a profile list the way ``profile_table`` would have."""
    summary = {
        "table_name": table,
        "column_name": TABLE_SUMMARY_SENTINEL,
        "data_type": None,
        "row_count": row_count,
        "null_ratio": None,
        "distinct_count": None,
        "sample_values": None,
        "min_value": None,
        "max_value": None,
        "fk_target": None,
        "column_comment": table_comment,
    }
    return [summary, *(columns or [])]


def _col(
    name: str,
    *,
    table: str = "t",
    data_type: str = "text",
    null_ratio: float | None = 0.0,
    distinct_count: int | None = None,
    sample_values: list[Any] | None = None,
    fk_target: str | None = None,
    column_comment: str | None = None,
) -> dict[str, Any]:
    return {
        "table_name": table,
        "column_name": name,
        "data_type": data_type,
        "row_count": None,
        "null_ratio": null_ratio,
        "distinct_count": distinct_count,
        "sample_values": sample_values,
        "min_value": None,
        "max_value": None,
        "fk_target": fk_target,
        "column_comment": column_comment,
    }


def test_format_profile_renders_header_with_row_count_and_comment() -> None:
    profile = {
        "customers": _make_profile(
            "customers",
            row_count=91,
            table_comment="Companies we sell to.",
            columns=[_col("customer_id", table="customers", distinct_count=91)],
        )
    }
    out = format_profile_for_llm(profile)
    first_line = out.splitlines()[0]
    assert "Table: customers" in first_line
    assert "91 rows" in first_line
    assert "Companies we sell to." in first_line


def test_format_profile_column_line_includes_fk_distinct_and_samples() -> None:
    profile = {
        "customers": _make_profile(
            "customers",
            columns=[
                _col(
                    "country",
                    table="customers",
                    data_type="character varying",
                    null_ratio=0.05,
                    distinct_count=21,
                    sample_values=["USA", "Germany", "Brazil"],
                ),
                _col(
                    "region_id",
                    table="customers",
                    data_type="integer",
                    fk_target="region.region_id",
                    distinct_count=4,
                ),
            ],
        )
    }
    out = format_profile_for_llm(profile)
    country_line = next(line for line in out.splitlines() if "country" in line)
    region_line = next(line for line in out.splitlines() if "region_id" in line)
    assert "21 distinct" in country_line
    assert "5% null" in country_line
    assert "samples: USA, Germany, Brazil" in country_line
    assert "FK -> region.region_id" in region_line


def test_format_profile_truncates_long_sample_values() -> None:
    profile = {
        "events": _make_profile(
            "events",
            columns=[
                _col(
                    "payload",
                    table="events",
                    sample_values=["a" * 100, "b" * 100],
                )
            ],
        )
    }
    line = next(
        line
        for line in format_profile_for_llm(profile).splitlines()
        if "payload" in line
    )
    # The truncation cap is 24 chars per sample value (see _format_column_line).
    assert "a" * 24 not in line  # full 24-char sample never appears
    assert "…" in line


def test_format_profile_truncates_long_column_lists() -> None:
    cols = [
        _col(f"col_{i}", table="wide", data_type="text") for i in range(50)
    ]
    profile = {"wide": _make_profile("wide", columns=cols)}
    out = format_profile_for_llm(profile, max_columns_per_table=10)
    rendered = [line for line in out.splitlines() if line.startswith("  - ")]
    assert len(rendered) == 10
    assert "... and 40 more columns" in out


def test_format_profile_empty_dict_returns_empty_string() -> None:
    assert format_profile_for_llm({}) == ""


def test_format_column_line_handles_zero_null_ratio() -> None:
    # 0% null suppresses the "0% null" tag (signal vs noise — the
    # interesting case is when nulls ARE present).
    line = _format_column_line(_col("id", null_ratio=0.0, distinct_count=10))
    assert "null" not in line


def test_format_column_line_handles_missing_distinct_count() -> None:
    line = _format_column_line(_col("x", distinct_count=None))
    assert "distinct" not in line


# ---------------------------------------------------------------------------
# write_profiles — verify the SQL parameter shape with a captured stub.
# ---------------------------------------------------------------------------


class _FakeConn:
    """Captures every ``conn.execute(text, params)`` call for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def execute(self, stmt: Any, params: Any = None) -> None:
        # ``stmt`` here is a SQLAlchemy ``TextClause`` — stringify so
        # tests can grep for keywords without importing TextClause.
        self.calls.append((str(stmt), params))


def test_write_profiles_truncates_and_inserts() -> None:
    conn = _FakeConn()
    rows = [
        {
            "table_name": "customers",
            "column_name": TABLE_SUMMARY_SENTINEL,
            "data_type": None,
            "row_count": 91,
            "null_ratio": None,
            "distinct_count": None,
            "sample_values": None,
            "min_value": None,
            "max_value": None,
            "fk_target": None,
            "column_comment": None,
        },
        {
            "table_name": "customers",
            "column_name": "country",
            "data_type": "character varying",
            "row_count": 91,
            "null_ratio": 0.05,
            "distinct_count": 21,
            "sample_values": ["USA", "Germany"],
            "min_value": None,
            "max_value": None,
            "fk_target": None,
            "column_comment": None,
        },
    ]
    n = profiler.write_profiles(rows, conn=conn)
    assert n == 2

    # First call truncates, second call inserts.
    assert "TRUNCATE" in conn.calls[0][0]
    assert "INSERT INTO schema_profiles" in conn.calls[1][0]

    inserted_params = conn.calls[1][1]
    assert len(inserted_params) == 2
    # sample_values must be JSON-serialised by the time it reaches the bind.
    assert inserted_params[1]["sample_values"] == '["USA", "Germany"]'
    # NULL sample stays NULL (not the JSON string "null").
    assert inserted_params[0]["sample_values"] is None


def test_write_profiles_empty_rows_only_truncates() -> None:
    conn = _FakeConn()
    n = profiler.write_profiles([], conn=conn)
    assert n == 0
    assert len(conn.calls) == 1
    assert "TRUNCATE" in conn.calls[0][0]
