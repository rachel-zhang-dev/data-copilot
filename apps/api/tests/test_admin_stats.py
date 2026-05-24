"""Tests for the week-11 ``/admin/stats`` endpoint.

The dashboard surface is deliberately small (cache stats + uptime +
non-secret settings), and the unit tests pin three contracts:

1. Cache counters round-trip from the backend into the JSON payload.
2. The ``backend`` field flips between ``"in-memory"`` and ``"redis"``
   depending on ``REDIS_URL``.
3. No secret-shaped value leaks into the response (defence against a
   well-meaning future ``settings.dict()`` refactor).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """FastAPI TestClient that mocks the DB lifespan bits.

    Same pattern as ``test_streaming.py``; ``conversation_lock`` is
    not needed here because ``/admin/stats`` does not touch the
    advisory-lock pool.
    """
    monkeypatch.setattr("copilot.main.get_engine", lambda: None)
    monkeypatch.setattr("copilot.main.get_schema_ddl", lambda: "")
    monkeypatch.setattr("copilot.main.dispose_engine", lambda: None)

    async def _async_noop() -> None:
        return None

    monkeypatch.setattr("copilot.main.setup_checkpointer", _async_noop)
    monkeypatch.setattr("copilot.main.dispose_checkpointer", _async_noop)
    monkeypatch.setattr("copilot.main.get_checkpointer", _async_noop)
    monkeypatch.setattr("copilot.main.build_graph", lambda **_k: MagicMock())

    @asynccontextmanager
    async def _fake_lock(_thread_id: str):
        yield None

    monkeypatch.setattr("copilot.main.conversation_lock", _fake_lock)

    from copilot.cache import reset_embedding_cache

    reset_embedding_cache()

    from copilot.main import app

    with TestClient(app) as c:
        yield c


def test_admin_stats_returns_zeroed_cache_on_fresh_process(client: TestClient) -> None:
    """No traffic yet → counters are all zero but every key is present."""
    r = client.get("/admin/stats")
    assert r.status_code == 200
    body = r.json()

    assert body["version"]
    assert isinstance(body["uptime_seconds"], int)
    assert body["uptime_seconds"] >= 0

    cache = body["embedding_cache"]
    for key in (
        "hits",
        "misses",
        "size",
        "evictions",
        "max_size",
        "ttl_seconds",
        "hit_rate",
        "backend",
    ):
        assert key in cache, f"missing key in admin_stats.embedding_cache: {key}"
    assert cache["hits"] == 0
    assert cache["misses"] == 0
    assert cache["size"] == 0
    assert cache["hit_rate"] == 0.0
    assert cache["backend"] in ("in-memory", "redis")


def test_admin_stats_reflects_cache_traffic(client: TestClient) -> None:
    """Counters must round-trip through the JSON payload after real use."""
    from copilot.cache import get_embedding_cache

    cache = get_embedding_cache()
    cache.set("a", [0.1])
    cache.get("a")  # hit
    cache.get("b")  # miss

    body = client.get("/admin/stats").json()
    c = body["embedding_cache"]
    assert c["hits"] == 1
    assert c["misses"] == 1
    assert c["size"] == 1
    assert abs(c["hit_rate"] - 0.5) < 1e-6


def test_admin_stats_settings_block_has_no_secrets(client: TestClient) -> None:
    """Belt-and-suspenders: every key in the ``settings`` echo must be
    in the explicit allowlist, so a future ``settings.dict()`` refactor
    cannot silently spill ``DEEPSEEK_API_KEY`` or friends."""
    body = client.get("/admin/stats").json()
    settings_keys = set(body["settings"].keys())
    expected = {
        "deepseek_model",
        "embedding_model",
        "embedding_cache_enabled",
        "embedding_cache_max_size",
        "embedding_cache_ttl_seconds",
        "llm_max_retries",
        "risk_explain_cost_threshold",
        "app_env",
    }
    assert settings_keys == expected, (
        f"unexpected admin_stats.settings keys: {settings_keys - expected}; "
        f"missing: {expected - settings_keys}"
    )
    # Negative check — secret-shaped keys must NOT leak.
    leak_substrings = ("key", "secret", "password", "token", "url")
    actual_keys_lower = {k.lower() for k in settings_keys}
    for needle in leak_substrings:
        leaked = {k for k in actual_keys_lower if needle in k}
        assert not leaked, f"admin_stats.settings leaks secret-shaped key(s): {leaked}"
