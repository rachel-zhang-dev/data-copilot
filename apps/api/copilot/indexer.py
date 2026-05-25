"""Build (or rebuild) the ``schema_embeddings`` + ``schema_profiles`` tables.

Run this once at provisioning time and any time the database schema or
the embedding model changes::

    ./scripts/dev.sh index

What it does
------------
1. Verifies the embedding service is reachable and returns the
   configured dimension (cheap fail-fast).
2. Lists every business table (operational tables like
   ``schema_embeddings`` / ``schema_profiles`` are filtered out at the
   ``list_tables`` layer).
3. Generates a textual description per table (name, columns, FKs).
4. Embeds the descriptions in a single batched API call.
5. Runs ``ANALYZE`` so ``pg_stats`` reflects the current data shape,
   then profiles every table (row counts, NULL ratios, distinct counts,
   sample values, FK targets, comments) via ``copilot.profiler``.
6. Truncates and re-populates ``schema_embeddings`` **and**
   ``schema_profiles`` inside ONE transaction, so the two derived
   tables can never drift out of sync (see ADR 0016 §"Why same txn").

Idempotent: running it twice leaves the same final state. The
description format is deterministic and the profiler is statistics-
only, so re-runs only churn the derived tables when the underlying
schema or data shape changes meaningfully.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from copilot.config import get_settings
from copilot.db import get_engine, get_table_ddl, list_tables
from copilot.embeddings import EmbeddingError, check_embedding_dimension, get_embedder
from copilot.profiler import profile_all_tables, write_profiles

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
            profile_rows = conn.execute(
                text("SELECT count(*) FROM schema_profiles")
            ).scalar_one()
        # Skip only when BOTH derived tables look populated. After
        # adding schema_profiles (Phase 1.1) the previous "embeddings
        # row count == table count" check is necessary but no longer
        # sufficient — a fresh DB on an older image would have the
        # embeddings filled in but the profiles still empty.
        if existing == len(tables) and profile_rows > 0:
            log.info(
                "schema_embeddings has %d rows (= %d tables) and "
                "schema_profiles has %d rows; skip with --force to rebuild",
                existing,
                len(tables),
                profile_rows,
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

    # Refresh pg_stats so the profiler sees current null_frac /
    # n_distinct / histogram_bounds. ANALYZE is cheap (~1-2s for
    # Northwind, seconds-to-minutes for production-sized DBs), and
    # without it pg_stats lags whatever the seed scripts inserted.
    log.info("running ANALYZE to refresh pg_stats")
    with eng.begin() as conn:
        conn.execute(text("ANALYZE"))

    log.info("profiling %d tables", len(tables))
    profile_rows = profile_all_tables(engine=eng)

    log.info(
        "writing %d embedding rows + %d profile rows in one transaction",
        len(tables),
        len(profile_rows),
    )
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
        # Write profiles in the SAME transaction so the embedding index
        # and the profile table can never drift apart (ADR 0016).
        write_profiles(profile_rows, conn=conn)
    log.info(
        "schema index built: %d tables, %d profile rows", len(tables), len(profile_rows)
    )
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
            emb_count = conn.execute(
                text("SELECT count(*) FROM schema_embeddings")
            ).scalar_one()
            emb_sample = conn.execute(
                text(
                    "SELECT table_name, length(description) "
                    "FROM schema_embeddings ORDER BY table_name LIMIT 20"
                )
            ).fetchall()
            prof_count = conn.execute(
                text("SELECT count(*) FROM schema_profiles")
            ).scalar_one()
            prof_sample = conn.execute(
                text(
                    """
                    SELECT table_name, count(*) AS cols, max(row_count) AS rows
                    FROM schema_profiles
                    GROUP BY table_name
                    ORDER BY table_name
                    LIMIT 20
                    """
                )
            ).fetchall()
        log.info("schema_embeddings has %d rows", emb_count)
        for row in emb_sample:
            log.info("  %-25s desc=%d chars", row[0], row[1])
        log.info("schema_profiles has %d rows", prof_count)
        for row in prof_sample:
            log.info(
                "  %-25s %d profile rows (table_rows=%s)", row[0], row[1], row[2]
            )
        return 0

    written = index_schema(force=force)
    log.info("done. %d tables indexed.", written)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
