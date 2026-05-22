"""Unit tests for the SQL safety / rewriter.

These tests are intentionally chatty — each case documents one rule
the policy enforces. They run in milliseconds (no LLM, no DB).
"""

from __future__ import annotations

import pytest
from copilot.agent.sql_safety import SqlSafetyError, strip_fence, validate_and_rewrite

# ----------------------------------------------------------------------
# strip_fence
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("SELECT 1", "SELECT 1"),
        ("```sql\nSELECT 1\n```", "SELECT 1"),
        ("```SQL\nSELECT 1\n```", "SELECT 1"),
        ("```\nSELECT 1\n```", "SELECT 1"),
        ("  SELECT 1 ;  ", "SELECT 1"),
    ],
)
def test_strip_fence_handles_common_llm_outputs(raw: str, expected: str) -> None:
    assert strip_fence(raw) == expected


# ----------------------------------------------------------------------
# happy path
# ----------------------------------------------------------------------


def test_select_passes_and_limit_is_injected() -> None:
    rewritten = validate_and_rewrite("SELECT * FROM customers", max_rows=50)
    assert "LIMIT 50" in rewritten.upper()


def test_existing_limit_is_preserved() -> None:
    rewritten = validate_and_rewrite("SELECT * FROM customers LIMIT 7", max_rows=100)
    assert "LIMIT 7" in rewritten.upper()
    assert "LIMIT 100" not in rewritten.upper()


def test_count_query_passes() -> None:
    rewritten = validate_and_rewrite("SELECT COUNT(*) FROM customers")
    assert "COUNT" in rewritten.upper()


def test_cte_select_is_allowed() -> None:
    sql = """
        WITH top_categories AS (
            SELECT category_id FROM categories
        )
        SELECT * FROM top_categories
    """
    rewritten = validate_and_rewrite(sql)
    assert "LIMIT" in rewritten.upper()


def test_fenced_input_is_unwrapped_then_validated() -> None:
    rewritten = validate_and_rewrite("```sql\nSELECT 1\n```")
    assert "SELECT 1" in rewritten


# ----------------------------------------------------------------------
# rejections
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO customers (id) VALUES (1)",
        "UPDATE customers SET name = 'x' WHERE id = 1",
        "DELETE FROM customers",
        "DROP TABLE customers",
        "TRUNCATE TABLE customers",
        "ALTER TABLE customers ADD COLUMN x INT",
        "CREATE TABLE foo (id INT)",
    ],
)
def test_write_statements_are_rejected(sql: str) -> None:
    with pytest.raises(SqlSafetyError):
        validate_and_rewrite(sql)


def test_stacked_statements_are_rejected() -> None:
    with pytest.raises(SqlSafetyError, match="Multiple statements"):
        validate_and_rewrite("SELECT 1; DROP TABLE customers")


def test_select_into_is_rejected() -> None:
    with pytest.raises(SqlSafetyError, match="INTO"):
        validate_and_rewrite("SELECT * INTO archive FROM customers")


def test_select_for_update_is_rejected() -> None:
    with pytest.raises(SqlSafetyError, match="locks"):
        validate_and_rewrite("SELECT * FROM customers FOR UPDATE")


def test_string_literal_with_dangerous_word_is_not_rejected() -> None:
    """The literal ``'DELETE FROM users'`` must not trigger a regex misfire."""
    rewritten = validate_and_rewrite(
        "SELECT id FROM customers WHERE company_name = 'please DELETE this row'"
    )
    assert "DELETE" in rewritten.upper()
    assert "LIMIT" in rewritten.upper()


def test_empty_sql_is_rejected() -> None:
    with pytest.raises(SqlSafetyError, match="Empty"):
        validate_and_rewrite("   \n  ")


def test_garbage_input_raises_safety_error() -> None:
    with pytest.raises(SqlSafetyError):
        validate_and_rewrite("this is not sql at all !!!")
