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
