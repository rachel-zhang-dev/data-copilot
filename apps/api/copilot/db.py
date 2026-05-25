"""Database access layer.

A thin, dependency-free wrapper around SQLAlchemy that gives the rest of
the codebase three things:

1. ``get_engine()``        — a lazily-built, process-wide connection pool.
2. ``run_select(sql)``     — execute a SELECT and return rows as
                              ``list[dict]``. Read-only by convention.
3. ``get_schema_ddl()``    — introspect the live database and produce a
                              human-readable schema string for the LLM
                              prompt. Cached per process.

Why synchronous SQLAlchemy
--------------------------
LangGraph nodes are synchronous functions. The LLM call is by far the
slowest step (~1-3 s vs ~5 ms for a Northwind query), so the marginal
benefit of asyncpg would be small while making testing and mocking
significantly noisier. If the database ever becomes the bottleneck we
can swap the engine implementation without touching node code.

Why introspect at runtime instead of hand-writing the schema
------------------------------------------------------------
The seed SQL (``data/seed/01-northwind.sql``) might change; keeping the
prompt in sync with a hand-typed schema string would be a constant
source of drift. ``information_schema`` is the canonical source of
truth, so we read from there once at startup and cache.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Row

from copilot.config import get_settings


def _normalise_database_url(url: str) -> str:
    """Force SQLAlchemy to use psycopg3 (which we ship) instead of
    psycopg2 (the legacy default for the bare ``postgresql://`` scheme).

    Users do not have to remember the ``+psycopg`` suffix in their
    ``.env`` — we patch it on here.
    """
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine.

    ``pool_pre_ping`` survives connections dropped by the database
    (common in dev when Postgres restarts). ``pool_size=5`` is enough for
    a single dev box; production tuning is a Week 11 problem.
    """
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured. Did you copy .env.example to .env?")
    return create_engine(
        _normalise_database_url(settings.database_url),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        future=True,
    )


def dispose_engine() -> None:
    """Close all pooled connections. Call from FastAPI shutdown hook."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
        get_engine.cache_clear()


def _row_to_dict(row: Row[Any]) -> dict[str, Any]:
    """Convert a SQLAlchemy Row to a plain dict.

    SQLAlchemy 2.x Rows behave like tuples; ``._mapping`` exposes them as
    a read-only ``dict``-like view, which is what we want for JSON
    serialisation later.
    """
    return dict(row._mapping)


def run_select(sql: str, *, engine: Engine | None = None) -> list[dict[str, Any]]:
    """Execute ``sql`` and return rows as a list of dicts.

    The caller is responsible for ensuring ``sql`` is a single SELECT
    (the agent's ``sql_safety`` module enforces this). This function
    deliberately does not validate — keeping I/O and policy separate
    makes both easier to test.
    """
    eng = engine or get_engine()
    with eng.connect() as conn:
        result = conn.execute(text(sql))
        return [_row_to_dict(r) for r in result.fetchall()]


def explain_cost(
    sql: str, *, engine: Engine | None = None, timeout_ms: int = 500
) -> float:
    """Return Postgres' planner ``Total Cost`` for ``sql``.

    Uses ``EXPLAIN (FORMAT JSON)`` so the result is dialect-stable and
    easy to parse without scraping text. The query is **not** executed;
    only the planner runs.

    ``timeout_ms`` is enforced via ``SET LOCAL statement_timeout`` so a
    pathological query that stalls the planner cannot stall the agent.
    On any failure — timeout, parse error, planner exception — this
    function raises and the caller is expected to fall through (the
    risk node treats a failed cost lookup as "unknown, let it run").
    """
    eng = engine or get_engine()
    with eng.connect() as conn:
        # Wrap in a transaction so SET LOCAL is scoped to this call only
        # and never leaks into the next checked-out connection.
        with conn.begin():
            conn.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
            row = conn.execute(text(f"EXPLAIN (FORMAT JSON) {sql}")).fetchone()
    if row is None:
        raise RuntimeError("EXPLAIN returned no rows")
    payload = row[0]
    # Postgres' explain JSON shape: [{"Plan": {"Total Cost": float, ...}, ...}]
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"unexpected EXPLAIN JSON shape: {payload!r}")
    plan = payload[0].get("Plan")
    if not isinstance(plan, dict) or "Total Cost" not in plan:
        raise RuntimeError(f"EXPLAIN JSON missing Plan.Total Cost: {payload!r}")
    return float(plan["Total Cost"])


_BUSINESS_TABLE_FILTER = (
    "table_schema = 'public' "
    "AND table_type = 'BASE TABLE' "
    # Exclude tables created by the agent itself so they never appear in
    # user-facing schema dumps or get retrieved as candidates for SQL
    # generation. Week 3 added ``schema_embeddings``; Phase 1.1 (ADR
    # 0016) added ``schema_profiles``. Both are operational state, not
    # business data.
    "AND table_name NOT IN ('schema_embeddings', 'schema_profiles')"
)


def list_tables(engine: Engine | None = None) -> list[str]:
    """Return the sorted list of business tables in the public schema.

    "Business" excludes tables the agent itself owns (schema_embeddings),
    so the LLM never accidentally writes SQL against them.
    """
    eng = engine or get_engine()
    with eng.connect() as conn:
        return [
            row[0]
            for row in conn.execute(
                text(
                    f"""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE {_BUSINESS_TABLE_FILTER}
                    ORDER BY table_name
                    """
                )
            ).fetchall()
        ]


def _format_table_block(
    table: str,
    cols: list[tuple[str, str, str, str]],
    fks_out: list[tuple[str, str, str]],
    fks_in: list[tuple[str, str, str]],
) -> str:
    """Render one table block for the LLM prompt.

    ``fks_out``: this table's columns referencing another table.
    ``fks_in``:  another table's columns referencing this one.
    Both are surfaced because the LLM needs them to write JOINs in
    either direction.
    """
    lines = [f"Table: {table}"]
    for name, dtype, nullable, pk in cols:
        tags = []
        if pk:
            tags.append("PK")
        if nullable == "NO":
            tags.append("NOT NULL")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"  - {name} ({dtype}){tag_str}")

    if fks_out:
        lines.append("  Foreign keys (out):")
        for col, ref_tbl, ref_col in fks_out:
            lines.append(f"    - {col} -> {ref_tbl}.{ref_col}")
    if fks_in:
        lines.append("  Referenced by:")
        for ref_tbl, ref_col, col in fks_in:
            lines.append(f"    - {ref_tbl}.{ref_col} -> {col}")

    return "\n".join(lines)


def get_table_ddl(table_names: list[str], engine: Engine | None = None) -> str:
    """Return an LLM-friendly schema description for the given tables.

    Output format::

        Table: customers
          - customer_id (varchar) [PK, NOT NULL]
          - company_name (varchar) [NOT NULL]
          ...
          Foreign keys (out):
            - region_id -> region.region_id
          Referenced by:
            - orders.customer_id -> customer_id

    Each table block is self-contained: column types, nullability, PK
    membership, AND foreign-key relationships in both directions. The
    last bit is what lets the LLM write JOINs for "top products by
    sales" type questions where the question never names the bridge
    table.
    """
    if not table_names:
        return ""

    eng = engine or get_engine()
    fk_out_map = _get_fk_details_outgoing(eng)
    fk_in_map = _get_fk_details_incoming(eng)

    with eng.connect() as conn:
        pieces: list[str] = []
        for table in sorted(table_names):
            cols = conn.execute(
                text(
                    """
                    SELECT
                        c.column_name,
                        c.data_type,
                        c.is_nullable,
                        CASE WHEN kcu.column_name IS NOT NULL THEN 'PK' ELSE '' END AS pk
                    FROM information_schema.columns c
                    LEFT JOIN information_schema.key_column_usage kcu
                        ON kcu.table_name = c.table_name
                       AND kcu.column_name = c.column_name
                       AND kcu.constraint_name IN (
                           SELECT constraint_name
                           FROM information_schema.table_constraints
                           WHERE table_name = c.table_name
                             AND constraint_type = 'PRIMARY KEY'
                       )
                    WHERE c.table_schema = 'public'
                      AND c.table_name = :tbl
                    ORDER BY c.ordinal_position
                    """
                ),
                {"tbl": table},
            ).fetchall()

            block = _format_table_block(
                table=table,
                cols=[(c[0], c[1], c[2], c[3]) for c in cols],
                fks_out=fk_out_map.get(table, []),
                fks_in=fk_in_map.get(table, []),
            )
            pieces.append(block)

        return "\n\n".join(pieces)


@lru_cache(maxsize=1)
def get_schema_ddl(engine: Engine | None = None) -> str:
    """Return DDL for **all** business tables — the legacy week-2 dump.

    Used as fallback when retrieval fails (see ``retrieve_schema_node``)
    and by the indexer when generating per-table descriptions.
    """
    return get_table_ddl(list_tables(engine), engine=engine)


@lru_cache(maxsize=1)
def get_foreign_keys(engine: Engine | None = None) -> dict[str, set[str]]:
    """Return the undirected FK adjacency graph: ``{table: {linked_tables}}``.

    Used by the retriever to expand the top-K embedding hits one hop
    along foreign keys, so JOIN-bridge tables get pulled in even when
    the user's question never names them.
    """
    eng = engine or get_engine()
    graph: dict[str, set[str]] = {}
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    tc.table_name AS src,
                    ccu.table_name AS dst
                FROM information_schema.table_constraints tc
                JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                   AND tc.table_schema = ccu.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = 'public'
                """
            )
        ).fetchall()
        for src, dst in rows:
            if src == dst:
                continue
            graph.setdefault(src, set()).add(dst)
            graph.setdefault(dst, set()).add(src)
    return graph


def _get_fk_details_outgoing(
    engine: Engine,
) -> dict[str, list[tuple[str, str, str]]]:
    """Return ``{table: [(local_col, ref_tbl, ref_col), ...]}``."""
    out: dict[str, list[tuple[str, str, str]]] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    tc.table_name  AS src_tbl,
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
                ORDER BY tc.table_name, kcu.ordinal_position
                """
            )
        ).fetchall()
        for src_tbl, src_col, dst_tbl, dst_col in rows:
            out.setdefault(src_tbl, []).append((src_col, dst_tbl, dst_col))
    return out


def _get_fk_details_incoming(
    engine: Engine,
) -> dict[str, list[tuple[str, str, str]]]:
    """Return ``{table: [(referencing_tbl, referencing_col, local_col), ...]}``."""
    inc: dict[str, list[tuple[str, str, str]]] = {}
    for src_tbl, fks in _get_fk_details_outgoing(engine).items():
        for src_col, dst_tbl, dst_col in fks:
            inc.setdefault(dst_tbl, []).append((src_tbl, src_col, dst_col))
    return inc
