"""Process-local TTL cache for embedding lookups (week 9).

This module is deliberately small: one ``TTLCache`` class with the
shape of a future ``RedisCache`` swap (``get`` / ``set`` / ``stats``),
plus a module-level singleton bound to the embedding cache.

Why not ``cachetools``
----------------------
``cachetools`` would add a dependency for ~30 lines of behaviour. The
agent is single-threaded inside the async event loop (graph nodes run
sequentially per turn, and ``run_eval`` serialises cases) so we don't
need the package's thread-safety story.

When Week 11 introduces multi-replica deployment, this file gets a
``RedisCache`` sibling with the same three-method surface. Callers
import ``get_embedding_cache()`` and never touch the backend
directly, so the migration is one ``if`` in this module.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Generic, TypeVar

from copilot.config import get_settings

log = logging.getLogger(__name__)

V = TypeVar("V")


@dataclass(frozen=True)
class CacheStats:
    """Snapshot of a cache's lifetime counters.

    All counters are monotonic since the cache was instantiated.
    ``hit_rate`` is ``hits / (hits + misses)`` for non-zero traffic,
    otherwise 0.0 (so dashboards don't divide by zero).
    """

    hits: int
    misses: int
    size: int
    evictions: int
    max_size: int
    ttl_seconds: int

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class TTLCache(Generic[V]):
    """Tiny TTL cache.

    * Keys: ``str``. (Embeddings are keyed by a stable hash of
      ``(model, text)``; using ``str`` rather than ``Hashable`` keeps
      the type narrow and the Redis swap obvious — Redis only takes
      string keys.)
    * Eviction: FIFO when capacity is exceeded. Not LRU. LRU would
      need ordered-dict bookkeeping for marginal hit-rate gains on a
      cache that is mostly insert-then-read.
    * Thread safety: a single ``RLock`` guards mutation. Reads also
      check the expiry timestamp under the lock so ``get`` cannot
      race with concurrent ``set`` invalidations.
    """

    def __init__(self, *, max_size: int, ttl_seconds: int) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        # Maps key -> (value, expires_at_monotonic).
        self._store: dict[str, tuple[V, float]] = {}
        # Insertion order, used for FIFO eviction.
        self._insertion: list[str] = []
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    # -- public surface ----------------------------------------------------

    def get(self, key: str) -> V | None:
        """Return the cached value or ``None`` on miss / expiry.

        Expired entries are evicted in-place so ``size`` stays honest.
        """
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            value, expires_at = entry
            if expires_at <= time.monotonic():
                self._evict(key)
                self._misses += 1
                return None
            self._hits += 1
            return value

    def set(self, key: str, value: V) -> None:
        """Insert or replace ``key`` with ``value``.

        If inserting would push past ``max_size``, the oldest entry by
        insertion order is dropped first.
        """
        with self._lock:
            if key in self._store:
                # Replace: update expiry but keep position in FIFO order.
                self._store[key] = (value, time.monotonic() + self._ttl_seconds)
                return
            while len(self._store) >= self._max_size and self._insertion:
                oldest = self._insertion.pop(0)
                if oldest in self._store:
                    del self._store[oldest]
                    self._evictions += 1
            self._store[key] = (value, time.monotonic() + self._ttl_seconds)
            self._insertion.append(key)

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(
                hits=self._hits,
                misses=self._misses,
                size=len(self._store),
                evictions=self._evictions,
                max_size=self._max_size,
                ttl_seconds=self._ttl_seconds,
            )

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._insertion.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0

    # -- internal ----------------------------------------------------------

    def _evict(self, key: str) -> None:
        if key in self._store:
            del self._store[key]
            self._evictions += 1
        try:
            self._insertion.remove(key)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Module-level embedding cache singleton
# ---------------------------------------------------------------------------


_embedding_cache: TTLCache[list[float]] | None = None


def get_embedding_cache() -> TTLCache[list[float]]:
    """Return the process-wide embedding cache, building it on first use.

    The cache is *not* reset across test runs — pytest's module-level
    fixtures often want it to persist. Call ``reset_embedding_cache()``
    explicitly in tests that need a clean slate.
    """
    global _embedding_cache
    if _embedding_cache is None:
        s = get_settings()
        _embedding_cache = TTLCache[list[float]](
            max_size=s.embedding_cache_max_size,
            ttl_seconds=s.embedding_cache_ttl_seconds,
        )
        log.info(
            "embedding cache initialised: max_size=%d ttl=%ds",
            s.embedding_cache_max_size,
            s.embedding_cache_ttl_seconds,
        )
    return _embedding_cache


def reset_embedding_cache() -> None:
    """Drop and re-create the embedding cache.

    Used by tests that need a fresh cache without monkey-patching the
    module; not part of the public runtime API.
    """
    global _embedding_cache
    _embedding_cache = None


def embedding_cache_key(model: str, text: str) -> str:
    """Compose a stable key for the ``(model, text)`` pair.

    Kept here (rather than at the call site) so a future Redis backend
    using the same key shape across replicas does not have to re-derive
    the format. We do *not* hash — `(model, text)` is small enough that
    storing it verbatim is fine, and the readable key helps debugging.
    """
    return f"{model}::{text}"
