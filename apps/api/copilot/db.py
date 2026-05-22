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


@lru_cache(maxsize=1)
def get_schema_ddl(engine: Engine | None = None) -> str:
    """Return a compact, LLM-friendly schema description of the DB.

    The output looks like::

        Table: customers
          - customer_id (varchar, PK)
          - company_name (varchar, NOT NULL)
          - ...

        Table: orders
          - ...

    We deliberately do **not** dump full ``CREATE TABLE`` DDL — those
    contain noise like tablespace pragmas that waste tokens. The output
    above is dense, readable, and well within DeepSeek's 64K context.
    """
    eng = engine or get_engine()
    with eng.connect() as conn:
        tables = [
            row[0]
            for row in conn.execute(
                text(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                    """
                )
            ).fetchall()
        ]

        pieces: list[str] = []
        for table in tables:
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

            lines = [f"Table: {table}"]
            for name, dtype, nullable, pk in cols:
                tags = []
                if pk:
                    tags.append("PK")
                if nullable == "NO":
                    tags.append("NOT NULL")
                tag_str = f" [{', '.join(tags)}]" if tags else ""
                lines.append(f"  - {name} ({dtype}){tag_str}")
            pieces.append("\n".join(lines))

        return "\n\n".join(pieces)
