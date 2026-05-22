"""SQL safety: parse, validate, and rewrite LLM-generated SQL.

We never let an LLM-produced string reach the database without going
through this module. The rules enforced here are deliberately strict:

* exactly one top-level statement
* the statement must be a SELECT (CTEs that ultimately SELECT are fine)
* no ``SELECT ... INTO`` (which writes a new table)
* no ``FOR UPDATE`` / ``FOR SHARE`` locks
* an explicit row cap — if the LLM forgets ``LIMIT`` we inject one

Why a parser (sqlglot) instead of a regex
-----------------------------------------
A regex looking for "INSERT" / "DROP" inevitably misfires on inputs
like::

    SELECT id FROM t WHERE note = 'please DELETE this row'

Parsing into an AST and inspecting the root node is the only robust
approach. ``sqlparse`` is a tokenizer, not a true parser, and cannot
reliably distinguish SELECT from INSERT at the root.

See ``docs/decisions/0002-sql-safety.md`` for the full rationale.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp


class SqlSafetyError(ValueError):
    """Raised when the SQL is rejected by the safety policy.

    Inheriting from ``ValueError`` keeps it pythonic — callers that
    treat all input-validation errors uniformly do not need to import
    this class.
    """


_FENCE_RE = re.compile(r"^\s*```(?:sql)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)


def strip_fence(text_: str) -> str:
    """Remove ```sql ... ``` markdown fences if the LLM ignored its prompt.

    Idempotent and safe to call on un-fenced strings. We also trim a
    trailing semicolon since sqlglot does not require one and downstream
    tooling sometimes objects.
    """
    cleaned = _FENCE_RE.sub("", text_).strip()
    return cleaned.rstrip(";").strip()


def _has_lock(node: exp.Select) -> bool:
    """sqlglot represents FOR UPDATE / FOR SHARE in the ``locks`` arg."""
    locks = node.args.get("locks")
    return bool(locks)


def validate_and_rewrite(sql: str, *, max_rows: int = 100) -> str:
    """Validate the SQL and return a normalised, LIMIT-bounded version.

    Args:
        sql: raw SQL string, possibly fenced or with trailing whitespace.
        max_rows: row cap applied when the query has no explicit LIMIT.

    Returns:
        A normalised PostgreSQL SQL string, guaranteed to be a single
        read-only SELECT with a LIMIT.

    Raises:
        SqlSafetyError: if any rule is violated. The message is safe to
            surface to end users — it never contains the offending SQL
            itself, only a short explanation.
    """
    cleaned = strip_fence(sql)
    if not cleaned:
        raise SqlSafetyError("Empty SQL.")

    try:
        statements = sqlglot.parse(cleaned, read="postgres")
    except sqlglot.errors.ParseError as exc:
        raise SqlSafetyError(f"SQL could not be parsed: {exc}") from exc

    # Reject "stacked" statements like ``SELECT 1; DROP TABLE x``.
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SqlSafetyError("Multiple statements are not allowed; submit a single SELECT.")

    parsed = statements[0]

    if not isinstance(parsed, exp.Select):
        kind = type(parsed).__name__.upper()
        raise SqlSafetyError(f"Only SELECT statements are allowed (got {kind}).")

    if parsed.find(exp.Into):
        raise SqlSafetyError("SELECT ... INTO is a write operation and is not allowed.")

    if _has_lock(parsed):
        raise SqlSafetyError("Row locks (FOR UPDATE / FOR SHARE) are not allowed.")

    if not parsed.args.get("limit"):
        parsed = parsed.limit(max_rows)

    return parsed.sql(dialect="postgres")
