"""Build (or rebuild) the ``schema_embeddings`` table.

Run this once at provisioning time and any time the database schema or
the embedding model changes::

    ./scripts/dev.sh index

What it does
------------
1. Verifies the embedding service is reachable and returns the
   configured dimension (cheap fail-fast).
2. Lists every business table (``schema_embeddings`` itself excluded).
3. Generates a textual description per table (name, columns, FKs).
4. Embeds the descriptions in a single batched API call.
5. Truncates and re-populates ``schema_embeddings`` inside one
   transaction, so the table is never half-populated.

Idempotent: running it twice leaves the same final state. The
description format is deterministic so re-runs only churn the
embeddings table when the underlying schema or model changes.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from copilot.config import get_settings
from copilot.db import get_engine, get_table_ddl, list_tables
from copilot.embeddings import EmbeddingError, check_embedding_dimension, get_embedder

log = logging.getLogger(__name__)


def describe_table(table: str) -> str:
    """Return the text we will embed for ``table``.

    We reuse ``get_table_ddl`` so the description shown to the LLM at
    SQL-generation time and the description used to index this table
    are guaranteed to share the same vocabulary. That alignment is
    important for retrieval quality.
    """
    return get_table_ddl([table])


def index_schema(*, force: bool = False) -> int:
    """Embed each business table and upsert into ``schema_embeddings``.

    Args:
        force: when ``False`` and the table already has the same row
            count as ``list_tables()``, skip the work. When ``True``,
            always rebuild.

    Returns:
        The number of rows written.

    Raises:
        EmbeddingError: if the dimension probe fails.
    """
    check_embedding_dimension()

    eng = get_engine()
    tables = list_tables()
    if not tables:
        log.warning("no business tables found; skipping index build")
        return 0

    if not force:
        with eng.connect() as conn:
            existing = conn.execute(text("SELECT count(*) FROM schema_embeddings")).scalar_one()
        if existing == len(tables):
            log.info(
                "schema_embeddings already has %d rows (= %d tables); skip with --force to rebuild",
                existing,
                len(tables),
            )
            return int(existing)

    log.info("describing %d tables", len(tables))
    descriptions = [describe_table(t) for t in tables]

    log.info("embedding %d descriptions in one batch", len(descriptions))
    embedder = get_embedder()
    vectors = embedder.embed_documents(descriptions)

    settings = get_settings()
    if any(len(v) != settings.embedding_dim for v in vectors):
        raise EmbeddingError(
            f"some embeddings did not match configured dim {settings.embedding_dim}"
        )

    log.info("writing %d rows to schema_embeddings", len(tables))
    with eng.begin() as conn:
        conn.execute(text("TRUNCATE TABLE schema_embeddings RESTART IDENTITY"))
        conn.execute(
            text(
                """
                INSERT INTO schema_embeddings (table_name, description, embedding)
                VALUES (:tbl, :desc, CAST(:emb AS vector))
                """
            ),
            [
                {"tbl": tbl, "desc": desc, "emb": _pgvector_literal(vec)}
                for tbl, desc, vec in zip(tables, descriptions, vectors, strict=True)
            ],
        )
    log.info("schema index built: %d tables", len(tables))
    return len(tables)


def _pgvector_literal(vec: list[float]) -> str:
    """Format a Python list as a pgvector literal: ``[0.1,0.2,...]``.

    The pgvector type accepts text input in this exact shape; using
    ``CAST(:emb AS vector)`` lets us bind it as a normal SQL parameter
    without needing the ``pgvector`` SDK adapter on the engine.
    """
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


def cli(argv: list[str] | None = None) -> int:
    """Entry-point for ``./scripts/dev.sh index``.

    Args (parsed manually to keep stdlib-only):
        --force : always rebuild even if the row count looks right
        --check : only print the current state, do not write
    """
    import sys

    args = argv if argv is not None else sys.argv[1:]
    force = "--force" in args
    check_only = "--check" in args

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if check_only:
        eng = get_engine()
        with eng.connect() as conn:
            count = conn.execute(text("SELECT count(*) FROM schema_embeddings")).scalar_one()
            sample = conn.execute(
                text(
                    "SELECT table_name, length(description) "
                    "FROM schema_embeddings ORDER BY table_name LIMIT 20"
                )
            ).fetchall()
        log.info("schema_embeddings has %d rows", count)
        for row in sample:
            log.info("  %-25s desc=%d chars", row[0], row[1])
        return 0

    written = index_schema(force=force)
    log.info("done. %d tables indexed.", written)
    return 0
