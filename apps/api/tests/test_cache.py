"""Unit tests for the week-9 in-memory embedding cache.

The cache surface is small (``get`` / ``set`` / ``stats`` / ``clear``)
and the test coverage matches that — happy path, FIFO eviction, TTL
expiry, and stats counter correctness.
"""

from __future__ import annotations

import time

import pytest
from copilot.cache import (
    TTLCache,
    embedding_cache_key,
    get_embedding_cache,
    reset_embedding_cache,
)


def test_set_then_get_returns_value() -> None:
    cache: TTLCache[int] = TTLCache(max_size=8, ttl_seconds=60)
    cache.set("a", 1)
    assert cache.get("a") == 1


def test_miss_returns_none_and_counts() -> None:
    cache: TTLCache[int] = TTLCache(max_size=8, ttl_seconds=60)
    assert cache.get("nope") is None
    stats = cache.stats()
    assert stats.misses == 1
    assert stats.hits == 0


def test_stats_track_hits_and_misses() -> None:
    cache: TTLCache[int] = TTLCache(max_size=8, ttl_seconds=60)
    cache.set("a", 1)
    cache.get("a")  # hit
    cache.get("a")  # hit
    cache.get("b")  # miss
    stats = cache.stats()
    assert stats.hits == 2
    assert stats.misses == 1
    assert abs(stats.hit_rate - 2 / 3) < 1e-9


def test_fifo_eviction_when_over_capacity() -> None:
    cache: TTLCache[int] = TTLCache(max_size=2, ttl_seconds=60)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)  # evicts "a"
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3
    assert cache.stats().evictions == 1


def test_replace_does_not_double_insert() -> None:
    """Re-setting an existing key should NOT evict another entry."""
    cache: TTLCache[int] = TTLCache(max_size=2, ttl_seconds=60)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("a", 11)  # update — must not evict "b"
    assert cache.get("a") == 11
    assert cache.get("b") == 2
    assert cache.stats().evictions == 0


def test_ttl_expiry_returns_none() -> None:
    cache: TTLCache[int] = TTLCache(max_size=8, ttl_seconds=1)
    cache.set("a", 1)
    time.sleep(1.1)
    assert cache.get("a") is None
    # An expired entry counts as both an eviction and a miss
    stats = cache.stats()
    assert stats.misses == 1
    assert stats.evictions == 1


def test_clear_resets_everything() -> None:
    cache: TTLCache[int] = TTLCache(max_size=8, ttl_seconds=60)
    cache.set("a", 1)
    cache.get("a")
    cache.clear()
    stats = cache.stats()
    assert stats.size == 0
    assert stats.hits == 0
    assert stats.misses == 0


def test_rejects_invalid_construction() -> None:
    with pytest.raises(ValueError):
        TTLCache[int](max_size=0, ttl_seconds=60)
    with pytest.raises(ValueError):
        TTLCache[int](max_size=8, ttl_seconds=0)


def test_embedding_cache_key_is_stable_and_model_specific() -> None:
    assert embedding_cache_key("m1", "hello") == "m1::hello"
    # Different model → different key (so a model swap invalidates entries)
    assert embedding_cache_key("m1", "hello") != embedding_cache_key("m2", "hello")


def test_module_singleton_can_be_reset() -> None:
    """``reset_embedding_cache`` is the only way to drop the process-wide
    cache; without it, two tests would share state. Pin the contract."""
    c1 = get_embedding_cache()
    c1.set("x", [1.0])
    reset_embedding_cache()
    c2 = get_embedding_cache()
    assert c1 is not c2
    assert c2.get("x") is None


# ---------------------------------------------------------------------------
# Week 11: RedisCache backend (mocked redis client)
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal sync-redis stand-in. Enough surface for ``RedisCache``:
    ``get`` / ``setex`` / ``incr`` / ``delete`` / ``scan_iter``.
    Decoded-responses semantics so we mirror ``Redis.from_url(...,
    decode_responses=True)``."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    @classmethod
    def from_url(cls, *_a, **_k) -> _FakeRedis:
        return cls()

    def get(self, key: str) -> str | None:
        if key in self._counters:
            return str(self._counters[key])
        return self._store.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self._store[key] = value

    def incr(self, key: str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    def delete(self, *keys: str) -> None:
        for k in keys:
            self._store.pop(k, None)
            self._counters.pop(k, None)

    def scan_iter(self, *, match: str):
        prefix = match.rstrip("*")
        for k in list(self._store.keys()):
            if k.startswith(prefix):
                yield k


@pytest.fixture()
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    """Patch ``redis.Redis`` so ``RedisCache(...)`` constructs against
    the stand-in. Tests get the underlying ``_FakeRedis`` back so
    they can assert on store state directly."""
    fake = _FakeRedis()
    import redis as redis_module

    monkeypatch.setattr(redis_module, "Redis", _FakeRedis)
    # The from_url classmethod is reused by ``RedisCache.__init__``;
    # patching the *class* covers both ``Redis.from_url`` and
    # ``Redis(...)`` callsites.
    return fake


def test_redis_cache_round_trip(fake_redis: _FakeRedis) -> None:
    from copilot.cache import RedisCache

    c = RedisCache("redis://test", max_size=10, ttl_seconds=60)
    assert c.get("k") is None
    c.set("k", [0.1, 0.2])
    out = c.get("k")
    assert out == [0.1, 0.2]


def test_redis_cache_stats_track_hits_and_misses(fake_redis: _FakeRedis) -> None:
    from copilot.cache import RedisCache

    c = RedisCache("redis://test", max_size=10, ttl_seconds=60)
    c.get("missing")
    c.set("a", [1.0])
    c.get("a")
    c.get("a")
    s = c.stats()
    assert s.hits == 2
    assert s.misses == 1
    assert s.size == 1
    # FIFO eviction isn't tracked by Redis TTL; always 0.
    assert s.evictions == 0


def test_redis_cache_handles_backend_errors_softly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis outage must never propagate an exception to the caller —
    the cache contract is "best-effort"."""
    from copilot.cache import RedisCache

    class _Broken(_FakeRedis):
        def get(self, key: str) -> str | None:
            raise ConnectionError("redis down")

        def setex(self, *_a, **_k) -> None:
            raise ConnectionError("redis down")

    import redis as redis_module

    monkeypatch.setattr(redis_module, "Redis", _Broken)

    c = RedisCache("redis://test", max_size=10, ttl_seconds=60)
    # Neither call should raise; the cache simply behaves like a miss.
    c.set("x", [1.0])
    assert c.get("x") is None


def test_redis_cache_rejects_invalid_construction() -> None:
    from copilot.cache import RedisCache

    with pytest.raises(ValueError):
        RedisCache("", max_size=10, ttl_seconds=60)


def test_get_embedding_cache_picks_redis_when_url_set(
    fake_redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The factory function is the only place backend selection lives;
    pin that ``REDIS_URL`` flips it deterministically."""
    from copilot.config import get_settings

    reset_embedding_cache()
    monkeypatch.setattr(get_settings(), "redis_url", "redis://test")
    cache = get_embedding_cache()
    assert type(cache).__name__ == "RedisCache"
    reset_embedding_cache()
    monkeypatch.setattr(get_settings(), "redis_url", None)
    cache2 = get_embedding_cache()
    assert type(cache2).__name__ == "TTLCache"
