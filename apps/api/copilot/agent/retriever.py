"""Schema retrieval node — the core of the week-3 RAG pipeline.

Three jobs, kept separate so each is unit-testable in isolation:

1. ``vector_search_tables`` — turn a question into a vector and return
   the top-K most similar table names by cosine distance.
2. ``expand_with_foreign_keys`` — pull in tables linked by a foreign
   key one hop away from the seed set, so JOIN-bridge tables show up
   even when the user's question never mentions them.
3. ``retrieve_schema_node`` — a LangGraph node that ties (1) and (2)
   together, builds the focused DDL, and gracefully falls back to the
   full schema when retrieval fails.

The fallback is what keeps the agent useful even when the embedding
provider is down or the index has not been built. We never want a RAG
outage to take the whole agent offline.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import text

from copilot.agent.state import AgentState
from copilot.config import get_settings
from copilot.db import get_engine, get_foreign_keys, get_schema_ddl, get_table_ddl, list_tables
from copilot.embeddings import EmbeddingError, get_embedder

log = logging.getLogger(__name__)


def _pgvector_literal(vec: list[float]) -> str:
    """Same format as the indexer uses; kept duplicate-but-trivial to
    avoid coupling the agent runtime to the indexer module."""
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


# A table is "directly named" when its name appears as a whole word in
# the user's question. We use this as a shortcut to skip the vector
# search entirely on questions like "how many rows in customers" — the
# answer is unambiguous and the embedding round-trip is wasted effort.
_WORD_RE = re.compile(r"\b(\w+)\b", re.UNICODE)


def directly_named_tables(question: str, all_tables: list[str]) -> set[str]:
    """Return the subset of ``all_tables`` whose name appears as a whole
    word in ``question`` (case-insensitive)."""
    words = {w.lower() for w in _WORD_RE.findall(question)}
    return {t for t in all_tables if t.lower() in words}


def vector_search_tables(question: str, k: int) -> list[str]:
    """Embed the question and return the top-``k`` table names from
    ``schema_embeddings``, ordered by cosine distance.

    Raises:
        EmbeddingError: if the embedding service fails.
        RuntimeError: if the index table is empty (caller should
            fall back).
    """
    try:
        q_vec = get_embedder().embed_query(question)
    except Exception as exc:
        raise EmbeddingError(f"embed_query failed: {exc}") from exc

    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT table_name
                FROM schema_embeddings
                ORDER BY embedding <=> CAST(:qv AS vector)
                LIMIT :k
                """
            ),
            {"qv": _pgvector_literal(q_vec), "k": k},
        ).fetchall()

    if not rows:
        raise RuntimeError("schema_embeddings is empty; build the index first")

    return [r[0] for r in rows]


def expand_with_foreign_keys(
    base_tables: set[str],
    fk_graph: dict[str, set[str]],
    *,
    max_hops: int = 1,
) -> set[str]:
    """Walk ``fk_graph`` from ``base_tables`` up to ``max_hops`` away.

    For week 3 we ship ``max_hops=1``, which is enough for Northwind
    join chains (e.g. products -> order_details -> orders).
    Going to 2+ hops on broader schemas tends to pull in too much.
    """
    frontier = set(base_tables)
    visited = set(base_tables)
    for _ in range(max_hops):
        next_frontier: set[str] = set()
        for table in frontier:
            for neighbour in fk_graph.get(table, set()):
                if neighbour not in visited:
                    next_frontier.add(neighbour)
        if not next_frontier:
            break
        visited.update(next_frontier)
        frontier = next_frontier
    return visited


def retrieve_schema_node(state: AgentState) -> dict[str, Any]:
    """LangGraph node: pick a focused schema for the data branch.

    Strategy:
        1. If the question literally names tables, seed with those.
        2. Otherwise (or in addition), do a top-K vector search.
        3. Expand the seed set 1 hop along foreign keys.
        4. Build a focused DDL from those tables.
        5. On any error, fall back to the full schema.
    """
    settings = get_settings()
    question = state["question"]

    try:
        all_tables = list_tables()
        named = directly_named_tables(question, all_tables)
        try:
            top_k = set(vector_search_tables(question, settings.schema_top_k))
        except (EmbeddingError, RuntimeError) as exc:
            # If the question already names tables, those alone are
            # enough; otherwise we have to fall back to full schema.
            if not named:
                raise
            log.warning("vector search unavailable (%s); using named tables only", exc)
            top_k = set()

        seed = named | top_k
        if not seed:
            raise RuntimeError("no tables matched")

        fk_graph = get_foreign_keys()
        expanded = expand_with_foreign_keys(seed, fk_graph, max_hops=1)
        schema = get_table_ddl(sorted(expanded))

        log.info(
            "retrieve_schema: named=%s top_k=%s expanded=%s",
            sorted(named),
            sorted(top_k),
            sorted(expanded),
        )
        return {"relevant_schema": schema}

    except Exception as exc:
        log.warning(
            "schema retrieval failed (%s); falling back to full DDL",
            exc,
        )
        return {"relevant_schema": get_schema_ddl()}
