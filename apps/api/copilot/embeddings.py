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
from tenacity import (
    RetryError,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from copilot.cache import embedding_cache_key, get_embedding_cache
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


def embed_query(text: str) -> list[float]:
    """Embed ``text`` once, hitting the cache when configured.

    This is the function the agent should call (not
    ``get_embedder().embed_query`` directly). It centralises three
    concerns the runtime cares about:

    * **Cache lookup / store.** When ``embedding_cache_enabled`` is
      True, a ``(model, text)`` hit skips the network call entirely.
    * **Retries.** Transient provider failures (429 / 5xx / timeouts)
      are retried with exponential backoff up to
      ``embedding_max_retries``; the underlying ``OpenAIEmbeddings``
      class does not expose a retry knob.
    * **Cost accounting hook (week 9).** A cache hit increments a
      different counter than a network miss; the agent's cost
      reducer reads ``embedding_cache_last_was_hit`` after each call
      to know whether to charge for the embedding.

    Raises:
        EmbeddingError: when retries are exhausted or the provider
            returns a malformed response. ``retrieve_schema_node``
            already catches this and falls back to the full DDL.
    """
    s = get_settings()
    cache = get_embedding_cache() if s.embedding_cache_enabled else None
    key = embedding_cache_key(s.embedding_model, text)

    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            _set_last_hit(True)
            return cached

    try:
        vec = _embed_with_retry(text, max_retries=s.embedding_max_retries)
    except RetryError as exc:
        raise EmbeddingError(f"embed_query exhausted retries: {exc}") from exc
    except Exception as exc:
        raise EmbeddingError(f"embed_query failed: {exc}") from exc

    if cache is not None:
        cache.set(key, vec)
    _set_last_hit(False)
    return vec


# Module-level "did the most recent embed_query hit the cache?" flag.
# This is intentionally process-local rather than passed through the
# return signature: callers who care (the cost reducer) read it once
# right after calling embed_query, before any other thread could
# concurrently overwrite it. The single-threaded event-loop model
# makes this safe in practice.
_last_was_hit = False


def _set_last_hit(hit: bool) -> None:
    global _last_was_hit
    _last_was_hit = hit


def last_was_cache_hit() -> bool:
    """Return True iff the most recent ``embed_query`` was a cache hit.

    Used by the cost reducer to charge only for misses. Caller must
    invoke this immediately after ``embed_query`` to avoid clobbering
    by a subsequent call (the agent's nodes run sequentially so this
    is a safe contract in practice).
    """
    return _last_was_hit


# Retry-classifier helpers -------------------------------------------------

# We retry on anything that quacks like a "try again later" signal. The
# OpenAIEmbeddings client wraps everything as ``Exception`` subclasses
# from openai.* / httpx.*; matching on string contents is brittle but
# matching on class names that exist across versions of those clients
# is more so. Pragmatic: only retry exceptions that look transient.
# ``RetryError`` from tenacity is raised when the budget is exhausted.


def _is_transient(exc: BaseException) -> bool:
    text = str(exc).lower()
    if "429" in text or "rate limit" in text or "timeout" in text:
        return True
    # 5xx in body / status_code attribute
    if any(f" {code}" in text or f"({code}" in text for code in ("500", "502", "503", "504")):
        return True
    # httpx connection errors / read timeouts
    cls = type(exc).__name__.lower()
    if "timeout" in cls or "connect" in cls:
        return True
    return False


def _embed_with_retry(text: str, *, max_retries: int) -> list[float]:
    """Wrap ``embed_query`` with a transient-failure retry loop.

    Re-builds the ``tenacity.retry`` decorator per call so the budget
    reflects the current settings. ``max_retries`` is the *total*
    attempt count (so ``max_retries=3`` means up to 3 tries, with 2
    retries between them); a permanent error (e.g. 401) fails fast
    on the first attempt because ``_is_transient`` returns False.
    """
    attempts = max(1, max_retries)

    runner = retry(
        retry=retry_if_exception(_is_transient),
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )

    @runner
    def _call() -> list[float]:
        return list(get_embedder().embed_query(text))

    return _call()


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
