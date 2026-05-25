"""Schema profiler (Phase 1.1 / ADR 0016).

Builds a cheap, statistics-only profile of every business table — row
count, NULL ratio, distinct count, sample values, FK targets, comments —
and persists it to the ``schema_profiles`` table. The runtime agent
reads it back for two purposes:

* **Coverage check** decides whether the retrieved schema can answer
  the user's question, or whether to refuse with "no X in this DB".
* **Schema explorer** answers "what data do you have?" with a grouped
  tour.

Design choices (see ADR 0016):

* Everything comes from ``pg_stats``, ``pg_class``, ``information_schema``
  and ``pg_description``. We do **not** scan user data — ANALYZE has
  already done the sampling work and we trust its output.
* Table-level rows use a sentinel ``column_name = '*'``; column-level
  rows use the real column name. The PK is ``(table_name, column_name)``
  so a single ``INSERT`` per row works with the obvious upsert.
* ``format_profile_for_llm`` renders a token-efficient block fed to
  the coverage / explorer prompts. Same separator-on-newline style as
  ``get_table_ddl`` so the two blend in the prompt without seams.

The profiler is idempotent: rerunning with the same DB state produces
the same rows (modulo ``updated_at``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import Engine, text

from copilot.db import get_engine, list_tables

log = logging.getLogger(__name__)


# Sentinel column name used to mark the table-level summary row. Chosen
# because it is not a valid SQL identifier and so cannot collide with a
# real column name even on an exotic schema.
TABLE_SUMMARY_SENTINEL = "*"

# How many ``most_common_vals`` entries we keep per column. pg_stats
# already truncates to ``default_statistics_target`` (typically 100) so
# this cap is mostly about prompt size, not source data size.
_SAMPLE_VALUES_LIMIT = 5


# ---------------------------------------------------------------------------
# Data classes (plain dicts; profile is JSON-friendly by construction)
# ---------------------------------------------------------------------------


def _empty_column_profile(table: str, column: str, data_type: str | None) -> dict[str, Any]:
    """Return a zero-stats placeholder for a column we couldn't profile.

    Used when ``pg_stats`` has no row for a column (e.g. inheritance
    edge cases, or right after CREATE TABLE before any ANALYZE). The
    coverage / explorer prompts still see the column name + type so the
    LLM knows it exists.
    """
    return {
        "table_name": table,
        "column_name": column,
        "data_type": data_type,
        "row_count": None,
        "null_ratio": None,
        "distinct_count": None,
        "sample_values": None,
        "min_value": None,
        "max_value": None,
        "fk_target": None,
        "column_comment": None,
    }


# ---------------------------------------------------------------------------
# Building blocks — one query per concern, joined in Python.
# ---------------------------------------------------------------------------


def _table_row_count(eng: Engine, table: str) -> int | None:
    """Approximate row count via ``pg_class.reltuples``.

    ``reltuples`` is what the planner uses and is essentially free —
    no scan. The number is approximate (updated by ANALYZE) which is
    perfect for the use-case: the coverage prompt only needs the
    order of magnitude.
    """
    with eng.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT GREATEST(reltuples::bigint, 0) AS rows
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = :tbl
                """
            ),
            {"tbl": table},
        ).fetchone()
    return int(row[0]) if row is not None else None


def _column_types(eng: Engine, table: str) -> dict[str, str]:
    """``{column_name: data_type}`` for ``table`` in ordinal order."""
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = :tbl
                ORDER BY ordinal_position
                """
            ),
            {"tbl": table},
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def _column_stats(eng: Engine, table: str) -> dict[str, dict[str, Any]]:
    """Pull ``pg_stats`` rows for ``table`` and normalise to a dict.

    ``n_distinct`` is normalised from pg_stats' two-meanings encoding:
    a positive value is an absolute count; a negative value is a
    fraction of the table (e.g. -0.5 means "half the rows are distinct").
    We turn the latter into an absolute count using ``reltuples`` so the
    downstream consumer only ever sees a non-negative integer or NULL.

    ``most_common_vals`` comes back as a Postgres ``anyarray``; we cast
    it to ``text[]`` and trim to ``_SAMPLE_VALUES_LIMIT``.
    """
    row_count = _table_row_count(eng, table) or 0
    out: dict[str, dict[str, Any]] = {}
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    attname,
                    null_frac,
                    n_distinct,
                    most_common_vals::text AS mcv_text,
                    histogram_bounds::text AS hist_text
                FROM pg_stats
                WHERE schemaname = 'public' AND tablename = :tbl
                """
            ),
            {"tbl": table},
        ).fetchall()

    for r in rows:
        attname, null_frac, n_distinct, mcv_text, hist_text = r

        distinct_count: int | None
        if n_distinct is None:
            distinct_count = None
        elif n_distinct >= 0:
            distinct_count = int(n_distinct)
        else:
            # Negative => fraction of the table. Multiply by row count.
            distinct_count = int(round(abs(float(n_distinct)) * row_count)) if row_count else None

        sample_values = _parse_pg_array(mcv_text)[:_SAMPLE_VALUES_LIMIT] if mcv_text else None
        hist = _parse_pg_array(hist_text) if hist_text else None

        out[attname] = {
            "null_ratio": float(null_frac) if null_frac is not None else None,
            "distinct_count": distinct_count,
            "sample_values": sample_values,
            "min_value": hist[0] if hist else None,
            "max_value": hist[-1] if hist else None,
        }
    return out


def _parse_pg_array(literal: str) -> list[str]:
    """Parse Postgres' text-array literal into a Python list of strings.

    pg_stats columns like ``most_common_vals`` come back as either
    ``{val1,val2,val3}`` (Postgres' array text format) or already as
    JSON-ish text depending on driver. We handle the curly-brace form
    explicitly because it's the common case under psycopg3 for
    ``anyarray`` casts.

    Quoted entries (containing commas, braces, or backslashes) are
    de-quoted naively. The values are only used for prompt display, so
    a perfect round-trip is not required.
    """
    if not literal:
        return []
    s = literal.strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    if not s:
        return []

    # Lightweight split on top-level commas, respecting double-quoted entries.
    # ``had_quote`` tracks whether the current part contained ANY quoted
    # chunk; when true, we preserve the buffer verbatim (whitespace
    # inside quotes is significant). When false, we ``.strip()`` to
    # drop the surrounding cosmetic whitespace Postgres emits between
    # ``{`` / ``,`` / ``}`` and bare scalar entries.
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    escape = False
    had_quote = False
    for ch in s:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            had_quote = True
            continue
        if ch == "," and not in_quotes:
            entry = "".join(buf)
            parts.append(entry if had_quote else entry.strip())
            buf = []
            had_quote = False
            continue
        buf.append(ch)
    last = "".join(buf)
    parts.append(last if had_quote else last.strip())
    return [p for p in parts if p != ""]


def _column_comments(eng: Engine, table: str) -> dict[str, str]:
    """Return ``{column: comment}`` for any COMMENT ON COLUMN entries.

    Northwind doesn't ship comments out of the box, but real production
    schemas almost always do, and they're the highest-signal hint we
    can feed the LLM about column meaning. So we plumb them through
    even though the demo dataset shows them empty.
    """
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT a.attname, d.description
                FROM pg_class c
                JOIN pg_namespace n   ON n.oid = c.relnamespace
                JOIN pg_attribute a   ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                LEFT JOIN pg_description d
                    ON d.objoid = c.oid AND d.objsubid = a.attnum
                WHERE n.nspname = 'public' AND c.relname = :tbl
                """
            ),
            {"tbl": table},
        ).fetchall()
    return {r[0]: r[1] for r in rows if r[1]}


def _table_comment(eng: Engine, table: str) -> str | None:
    """The COMMENT ON TABLE text, if any."""
    with eng.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT obj_description(c.oid, 'pg_class')
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = :tbl
                """
            ),
            {"tbl": table},
        ).fetchone()
    return row[0] if row and row[0] else None


def _fk_targets(eng: Engine, table: str) -> dict[str, str]:
    """``{local_col: "ref_table.ref_col"}`` for FK columns in ``table``."""
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    kcu.column_name AS src_col,
                    ccu.table_name  AS dst_tbl,
                    ccu.column_name AS dst_col
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = 'public'
                  AND tc.table_name = :tbl
                """
            ),
            {"tbl": table},
        ).fetchall()
    return {src: f"{dst_tbl}.{dst_col}" for src, dst_tbl, dst_col in rows}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def profile_table(table: str, *, engine: Engine | None = None) -> list[dict[str, Any]]:
    """Return one table-level summary row + one row per column.

    The shape mirrors the ``schema_profiles`` table. Used by the
    indexer to batch-insert and by tests for direct assertion.
    """
    eng = engine or get_engine()

    types = _column_types(eng, table)
    if not types:
        log.warning("profile_table: %s has no columns; skipping", table)
        return []

    stats = _column_stats(eng, table)
    comments = _column_comments(eng, table)
    fks = _fk_targets(eng, table)
    table_rows = _table_row_count(eng, table)
    table_doc = _table_comment(eng, table)

    rows: list[dict[str, Any]] = [
        # Table-level summary row.
        {
            "table_name": table,
            "column_name": TABLE_SUMMARY_SENTINEL,
            "data_type": None,
            "row_count": table_rows,
            "null_ratio": None,
            "distinct_count": None,
            "sample_values": None,
            "min_value": None,
            "max_value": None,
            "fk_target": None,
            "column_comment": table_doc,
        }
    ]

    for col, dtype in types.items():
        base = _empty_column_profile(table, col, dtype)
        s = stats.get(col, {})
        base["row_count"] = table_rows
        base["null_ratio"] = s.get("null_ratio")
        base["distinct_count"] = s.get("distinct_count")
        base["sample_values"] = s.get("sample_values")
        base["min_value"] = s.get("min_value")
        base["max_value"] = s.get("max_value")
        base["fk_target"] = fks.get(col)
        base["column_comment"] = comments.get(col)
        rows.append(base)

    return rows


def profile_all_tables(*, engine: Engine | None = None) -> list[dict[str, Any]]:
    """Profile every business table and return a flat list of rows."""
    eng = engine or get_engine()
    out: list[dict[str, Any]] = []
    for tbl in list_tables(eng):
        out.extend(profile_table(tbl, engine=eng))
    return out


def write_profiles(
    rows: list[dict[str, Any]], *, conn: Any
) -> int:
    """Truncate ``schema_profiles`` and bulk-insert ``rows``.

    Takes an open SQLAlchemy connection so the caller controls the
    transaction (typically the same one that just rebuilt
    ``schema_embeddings``).

    ``sample_values`` is JSON-encoded; everything else passes through as-is.
    """
    conn.execute(text("TRUNCATE TABLE schema_profiles"))
    if not rows:
        return 0
    conn.execute(
        text(
            """
            INSERT INTO schema_profiles (
                table_name, column_name, data_type, row_count,
                null_ratio, distinct_count, sample_values,
                min_value, max_value, fk_target, column_comment
            )
            VALUES (
                :table_name, :column_name, :data_type, :row_count,
                :null_ratio, :distinct_count, CAST(:sample_values AS jsonb),
                :min_value, :max_value, :fk_target, :column_comment
            )
            """
        ),
        [
            {
                **r,
                "sample_values": (
                    json.dumps(r["sample_values"]) if r["sample_values"] is not None else None
                ),
            }
            for r in rows
        ],
    )
    return len(rows)


def load_profile(
    table_names: list[str], *, engine: Engine | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Read profiles back from ``schema_profiles`` keyed by table.

    The agent runtime calls this once per request with the list of
    retrieved tables. We always include the table-summary row first
    (``column_name = '*'``) followed by the columns in their original
    ordinal order — same shape the indexer wrote.
    """
    if not table_names:
        return {}
    eng = engine or get_engine()
    out: dict[str, list[dict[str, Any]]] = {}
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    table_name, column_name, data_type, row_count,
                    null_ratio, distinct_count, sample_values,
                    min_value, max_value, fk_target, column_comment
                FROM schema_profiles
                WHERE table_name = ANY(:tbls)
                ORDER BY
                    table_name,
                    CASE WHEN column_name = :sentinel THEN 0 ELSE 1 END,
                    column_name
                """
            ),
            {"tbls": list(table_names), "sentinel": TABLE_SUMMARY_SENTINEL},
        ).fetchall()
    for r in rows:
        d = dict(r._mapping)
        out.setdefault(d["table_name"], []).append(d)
    return out


# ---------------------------------------------------------------------------
# LLM-facing rendering
# ---------------------------------------------------------------------------


def format_profile_for_llm(
    profile_by_table: dict[str, list[dict[str, Any]]],
    *,
    max_columns_per_table: int = 30,
) -> str:
    """Render the profile as a compact, prompt-ready string.

    Output shape (one block per table)::

        Table: customers (91 rows) — Customers we sell to.
          - customer_id (varchar) [PK-like, 91 distinct, 0% null]
          - country (varchar) [21 distinct, 0% null, samples: USA, Germany, Brazil]
          - region (varchar) [4 distinct, 70% null]
          - region_id (int4) [FK -> region.region_id, 4 distinct]
          ...

    Designed to be both human-readable (operators tail this in logs)
    AND compact (the coverage prompt sees this block + a question and
    has to decide ok / refuse — every extra token is taxed twice).
    """
    if not profile_by_table:
        return ""

    blocks: list[str] = []
    for table in sorted(profile_by_table.keys()):
        rows = profile_by_table[table]
        summary = next((r for r in rows if r["column_name"] == TABLE_SUMMARY_SENTINEL), None)
        columns = [r for r in rows if r["column_name"] != TABLE_SUMMARY_SENTINEL]

        header = f"Table: {table}"
        if summary is not None and summary.get("row_count") is not None:
            header += f" ({summary['row_count']} rows)"
        if summary is not None and summary.get("column_comment"):
            header += f" — {summary['column_comment']}"
        block_lines = [header]

        for col in columns[:max_columns_per_table]:
            block_lines.append(_format_column_line(col))
        if len(columns) > max_columns_per_table:
            block_lines.append(
                f"  ... and {len(columns) - max_columns_per_table} more columns"
            )

        blocks.append("\n".join(block_lines))

    return "\n\n".join(blocks)


def _format_column_line(col: dict[str, Any]) -> str:
    """Render a single column row into one bullet line."""
    tags: list[str] = []

    dtype = col.get("data_type")
    ndv = col.get("distinct_count")
    null_ratio = col.get("null_ratio")
    fk = col.get("fk_target")
    samples = col.get("sample_values")
    comment = col.get("column_comment")

    if fk:
        tags.append(f"FK -> {fk}")
    if ndv is not None:
        tags.append(f"{ndv} distinct")
    if null_ratio is not None:
        pct = int(round(null_ratio * 100))
        if pct > 0:
            tags.append(f"{pct}% null")
    if samples:
        # Truncate each value to keep lines short on wide TEXT columns.
        preview = [_truncate(str(v), 24) for v in samples[:3]]
        tags.append("samples: " + ", ".join(preview))

    head = f"  - {col['column_name']}"
    if dtype:
        head += f" ({dtype})"
    if tags:
        head += " [" + ", ".join(tags) + "]"
    if comment:
        head += f" — {comment}"
    return head


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"
