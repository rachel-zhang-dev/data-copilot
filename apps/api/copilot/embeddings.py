"""Embedding model factory.

Mirrors the design of ``llm.py``: a single function returns a configured
LangChain client, hiding which provider sits behind it.

Why ``OpenAIEmbeddings`` against SiliconFlow
--------------------------------------------
SiliconFlow exposes BGE-M3 (and many other models) through an
OpenAI-compatible ``/v1/embeddings`` endpoint. Reusing
``langchain-openai`` — which we already depend on for the chat model —
means zero new wheels, zero new SDKs, and a one-env-var swap to any
other OpenAI-compatible embedding provider (Bailian, FireworksAI,
Together, etc.).

The ``check_embedding_dimension()`` helper is meant to be called once
at startup or from the indexer; it verifies the vendor returns the
dimension we expect, otherwise schema_embeddings inserts will fail
with an opaque pgvector error later.
"""

from __future__ import annotations

import logging

from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr

from copilot.config import get_settings

log = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """Raised when the embedding service is misconfigured or unreachable.

    Caught by ``retrieve_schema_node`` to fall back to dumping the full
    schema rather than failing the whole agent run.
    """


def get_embedder() -> OpenAIEmbeddings:
    """Return a configured embedder pointed at the SiliconFlow API.

    Switching providers is a ``.env`` change:

        EMBEDDING_BASE_URL=https://api.openai.com/v1
        EMBEDDING_MODEL=text-embedding-3-small
        EMBEDDING_DIM=1536

    No code change needed.
    """
    s = get_settings()
    return OpenAIEmbeddings(
        api_key=SecretStr(s.siliconflow_api_key),
        base_url=s.embedding_base_url,
        model=s.embedding_model,
        dimensions=s.embedding_dim,
    )


def check_embedding_dimension(probe: str = "ping") -> int:
    """Embed a tiny string and return the vector dimension.

    Raises:
        EmbeddingError: if the call fails OR the dimension does not
            match the configured ``embedding_dim`` (which would later
            cause silent corruption of the pgvector column).
    """
    s = get_settings()
    try:
        vec = get_embedder().embed_query(probe)
    except Exception as exc:
        raise EmbeddingError(f"embedding probe failed: {exc}") from exc

    if len(vec) != s.embedding_dim:
        raise EmbeddingError(
            f"embedding dim mismatch: configured {s.embedding_dim}, "
            f"provider returned {len(vec)}. Update EMBEDDING_DIM in .env "
            f"and rebuild the schema index."
        )
    log.info(
        "embedding probe ok: model=%s base=%s dim=%d",
        s.embedding_model,
        s.embedding_base_url,
        len(vec),
    )
    return len(vec)
