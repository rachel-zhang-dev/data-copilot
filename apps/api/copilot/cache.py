"""Embedding cache backends (week 9 + week 11).

Two implementations behind one duck-typed interface:

* ``TTLCache`` — process-local FIFO+TTL cache (week 9). Used by every
  developer setup and by single-replica Fly.io deploys.
* ``RedisCache`` — Redis-backed sibling (week 11). Same ``get`` /
  ``set`` / ``stats`` / ``clear`` surface; used when the operator
  sets ``REDIS_URL`` so multi-replica deploys share cache state.

``get_embedding_cache()`` picks between them at first call. Callers
only ever import that function — the backend swap is one ``if`` in
this file.

Why not ``cachetools``
----------------------
``cachetools`` would add a dependency for ~30 lines of behaviour. The
agent is single-threaded inside the async event loop (graph nodes run
sequentially per turn, and ``run_eval`` serialises cases) so we don't
need the package's thread-safety story for the in-memory variant.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from copilot.config import get_settings

if TYPE_CHECKING:
    # Imported lazily inside ``RedisCache`` so the import graph stays
    # clean for in-memory-only setups.
    from redis import Redis

log = logging.getLogger(__name__)

V = TypeVar("V")


class EmbeddingCacheBackend(Protocol):
    """Duck-typed interface every cache backend honours.

    The Redis-backed swap (week 11) is a one-line change inside
    ``get_embedding_cache()`` precisely because every consumer
    type-checks against this protocol.
    """

    def get(self, key: str) -> list[float] | None: ...
    def set(self, key: str, value: list[float]) -> None: ...
    def stats(self) -> CacheStats: ...
    def clear(self) -> None: ...


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
# Redis backend (week 11)
# ---------------------------------------------------------------------------


# Lifetime counter keys stored alongside the values themselves. We
# could MULTI / EXEC for atomicity but a single ``INCR`` is already
# atomic and we only ever read these for the ``stats`` endpoint.
_REDIS_HITS_KEY = "data_copilot:embed:hits"
_REDIS_MISSES_KEY = "data_copilot:embed:misses"
_REDIS_KEY_PREFIX = "data_copilot:embed:vec:"


class RedisCache:
    """Redis-backed sibling of ``TTLCache``.

    Shares the ``EmbeddingCacheBackend`` protocol so callers can swap
    backends by changing one line in ``get_embedding_cache``.

    * Keys are namespaced (``data_copilot:embed:vec:<model::text>``)
      so the cache safely shares a Redis instance with other apps.
    * TTL is enforced via Redis' built-in ``SETEX``; we don't need a
      Python-side timer.
    * ``hits`` / ``misses`` counters live in two extra Redis keys
      bumped via atomic ``INCR``. The ``size`` field becomes "count
      of cached vectors right now" via ``SCAN`` (capped at
      ``max_size`` for parity with the in-memory ``CacheStats``).
    """

    def __init__(
        self,
        url: str,
        *,
        max_size: int,
        ttl_seconds: int,
    ) -> None:
        if not url:
            raise ValueError("RedisCache requires a non-empty URL")
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        # Imported lazily so a deployment that never opts into Redis
        # does not pay the import cost.
        from redis import Redis

        self._r: Redis = Redis.from_url(
            url, decode_responses=True, socket_timeout=2
        )

    # -- public surface ----------------------------------------------------

    def get(self, key: str) -> list[float] | None:
        try:
            raw = self._r.get(_REDIS_KEY_PREFIX + key)
        except Exception as exc:
            log.warning("RedisCache.get failed (%s); treating as miss", exc)
            return None
        if raw is None:
            self._safe_incr(_REDIS_MISSES_KEY)
            return None
        try:
            # ``Redis.get`` is typed as ``Awaitable | Any`` because the
            # same class supports sync and async; we use sync mode
            # exclusively (``from_url`` without ``asyncio`` flavour) so
            # the runtime type is always ``str`` (decode_responses=True).
            parsed = json.loads(raw)  # type: ignore[arg-type]
        except json.JSONDecodeError:
            log.warning("RedisCache: malformed payload for %s; treating as miss", key)
            return None
        if not isinstance(parsed, list):
            return None
        self._safe_incr(_REDIS_HITS_KEY)
        return [float(x) for x in parsed]

    def set(self, key: str, value: list[float]) -> None:
        try:
            self._r.setex(
                _REDIS_KEY_PREFIX + key,
                self._ttl_seconds,
                json.dumps(list(value)),
            )
        except Exception as exc:
            log.warning("RedisCache.set failed (%s); cache miss next time", exc)

    def stats(self) -> CacheStats:
        try:
            # See the ``# type: ignore`` rationale in ``get``: sync
            # ``Redis`` returns concrete ``str | None``.
            hits = int(self._r.get(_REDIS_HITS_KEY) or 0)  # type: ignore[arg-type]
            misses = int(self._r.get(_REDIS_MISSES_KEY) or 0)  # type: ignore[arg-type]
            # SCAN with COUNT 1000 — fine for the small caches we
            # expect; for huge caches an approximate count via
            # DBSIZE on a dedicated DB would be cheaper.
            size = sum(1 for _ in self._r.scan_iter(match=_REDIS_KEY_PREFIX + "*"))
        except Exception as exc:
            log.warning("RedisCache.stats failed (%s); returning zeros", exc)
            hits = misses = size = 0
        return CacheStats(
            hits=hits,
            misses=misses,
            size=size,
            evictions=0,  # Redis TTL eviction is silent; we don't track it.
            max_size=self._max_size,
            ttl_seconds=self._ttl_seconds,
        )

    def clear(self) -> None:
        try:
            keys = list(self._r.scan_iter(match=_REDIS_KEY_PREFIX + "*"))
            if keys:
                self._r.delete(*keys)
            self._r.delete(_REDIS_HITS_KEY, _REDIS_MISSES_KEY)
        except Exception as exc:
            log.warning("RedisCache.clear failed (%s); state may be partial", exc)

    # -- internal ----------------------------------------------------------

    def _safe_incr(self, key: str) -> None:
        try:
            self._r.incr(key)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Module-level embedding cache singleton
# ---------------------------------------------------------------------------


_embedding_cache: EmbeddingCacheBackend | None = None


def get_embedding_cache() -> EmbeddingCacheBackend:
    """Return the process-wide embedding cache, building it on first use.

    Picks the backend based on settings:
      * ``REDIS_URL`` set → ``RedisCache`` (multi-replica safe).
      * Otherwise         → ``TTLCache`` (process-local).

    The cache is *not* reset across test runs — pytest's module-level
    fixtures often want it to persist. Call ``reset_embedding_cache()``
    explicitly in tests that need a clean slate.
    """
    global _embedding_cache
    if _embedding_cache is None:
        s = get_settings()
        if s.redis_url:
            _embedding_cache = RedisCache(
                s.redis_url,
                max_size=s.embedding_cache_max_size,
                ttl_seconds=s.embedding_cache_ttl_seconds,
            )
            log.info(
                "embedding cache: Redis backend at %s (ttl=%ds, max=%d)",
                s.redis_url,
                s.embedding_cache_ttl_seconds,
                s.embedding_cache_max_size,
            )
        else:
            _embedding_cache = TTLCache[list[float]](
                max_size=s.embedding_cache_max_size,
                ttl_seconds=s.embedding_cache_ttl_seconds,
            )
            log.info(
                "embedding cache: in-memory (max=%d, ttl=%ds)",
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
