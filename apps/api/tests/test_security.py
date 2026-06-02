"""Tests for the Phase 3.2 API-key gate + per-IP rate limiter.

Two layers under test:

* ``RateLimit`` (the unit) — pure-Python sliding-window counter.
* ``security_middleware`` (the integration) — installed on the real
  FastAPI app via ``TestClient`` so we exercise the same code path
  production requests hit.

Both default to no-op behaviour (``DEMO_API_KEY`` unset,
``RATE_LIMIT_PER_MINUTE = 30`` is the production default but tests
either disable it or call ``reset_for_tests`` between cases).
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from copilot import security as security_mod
from copilot.config import get_settings
from copilot.security import RateLimit, reset_for_tests
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# RateLimit unit tests
# ---------------------------------------------------------------------------


def test_rate_limit_admits_under_budget() -> None:
    rl = RateLimit(max_per_window=3, window_seconds=60)
    assert rl.allow("ip-a") is True
    assert rl.allow("ip-a") is True
    assert rl.allow("ip-a") is True


def test_rate_limit_rejects_over_budget() -> None:
    rl = RateLimit(max_per_window=2, window_seconds=60)
    assert rl.allow("ip-a") is True
    assert rl.allow("ip-a") is True
    assert rl.allow("ip-a") is False
    # A different key has its own bucket.
    assert rl.allow("ip-b") is True


def test_rate_limit_max_zero_always_allows() -> None:
    """``RATE_LIMIT_PER_MINUTE=0`` means "no limit" — every call admits."""
    rl = RateLimit(max_per_window=0, window_seconds=60)
    for _ in range(1000):
        assert rl.allow("ip-a") is True


def test_rate_limit_expires_old_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timestamps older than the window MUST be dropped from the bucket
    so a quiet client never has its old activity counted against it."""
    rl = RateLimit(max_per_window=2, window_seconds=60)
    base = 1_000_000.0
    monkeypatch.setattr(time, "monotonic", lambda: base)
    assert rl.allow("ip-a") is True
    assert rl.allow("ip-a") is True
    assert rl.allow("ip-a") is False
    # Fast-forward past the window.
    monkeypatch.setattr(time, "monotonic", lambda: base + 61)
    assert rl.allow("ip-a") is True


def test_rate_limit_gc_drops_empty_buckets() -> None:
    """When many keys arrive, the lazy GC must drop empty buckets so
    memory doesn't grow with cardinality of attackers."""
    rl = RateLimit(max_per_window=1, window_seconds=1)
    # Fill many keys
    for i in range(5000):
        rl.allow(f"ip-{i}")
    # Wait for window to elapse
    time.sleep(1.1)
    # Trigger a GC by allowing one more
    rl.allow("ip-trigger")
    # GC threshold is 4096; should have dropped most expired
    # buckets. Tolerant assertion: bucket dict should be much
    # smaller than the 5000 we inserted.
    assert len(rl._buckets) < 1000


# ---------------------------------------------------------------------------
# Middleware integration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A fresh TestClient with the security middleware loaded.

    We don't run the full FastAPI lifespan (which would try to connect
    to Postgres + LangSmith) — just construct a minimal app with the
    middleware installed so we can poke /health (bypassed) and a fake
    /ask (rate-limited).
    """
    from fastapi import FastAPI

    reset_for_tests()
    app = FastAPI()
    app.middleware("http")(security_mod.security_middleware)

    @app.get("/health")
    async def _health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/ask")
    async def _ask() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/admin/stats")
    async def _admin() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app)


def test_health_bypasses_api_key_gate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with the API key configured, ``/health`` MUST stay open —
    Fly's liveness probe runs unauthenticated."""
    monkeypatch.setattr(get_settings(), "demo_api_key", "secret-123")
    r = client.get("/health")
    assert r.status_code == 200


def test_missing_api_key_returns_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "demo_api_key", "secret-123")
    r = client.post("/ask")
    assert r.status_code == 401
    assert "X-API-Key" in r.json()["detail"]


def test_wrong_api_key_returns_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "demo_api_key", "secret-123")
    r = client.post("/ask", headers={"X-API-Key": "totally-wrong"})
    assert r.status_code == 401


def test_correct_api_key_passes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "demo_api_key", "secret-123")
    r = client.post("/ask", headers={"X-API-Key": "secret-123"})
    assert r.status_code == 200


def test_admin_stats_is_also_gated(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/admin/stats`` leaks cache + settings info; should require the
    key when configured (only /health and /metrics are bypassed)."""
    monkeypatch.setattr(get_settings(), "demo_api_key", "secret-123")
    r = client.get("/admin/stats")
    assert r.status_code == 401


def test_api_key_unset_means_no_gate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local dev: ``DEMO_API_KEY`` unset → middleware is no-op on the
    key check; every request passes regardless of header."""
    monkeypatch.setattr(get_settings(), "demo_api_key", None)
    r = client.post("/ask")
    assert r.status_code == 200


def test_rate_limit_triggers_on_burst(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the rate cap, calls go through; past the cap, 429 with
    a ``Retry-After`` header so a polite client backs off."""
    monkeypatch.setattr(get_settings(), "demo_api_key", None)
    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 3)
    reset_for_tests()

    for _ in range(3):
        r = client.post("/ask")
        assert r.status_code == 200

    r = client.post("/ask")
    assert r.status_code == 429
    assert r.headers.get("retry-after") == "60"
    assert "rate limit" in r.json()["detail"].lower()


def test_rate_limit_does_not_apply_to_bypass_paths(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/health`` skips the rate limiter so a Fly liveness probe that
    polls every 30s never trips the 30/min cap on its own."""
    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 2)
    reset_for_tests()
    for _ in range(10):
        r = client.get("/health")
        assert r.status_code == 200


def test_rate_limit_does_not_apply_to_non_llm_paths(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only ``/ask``, ``/ask/stream`` and ``/mcp/*`` should be rate
    limited — those are the LLM-cost surfaces. ``/admin/stats`` and
    dashboard reads stay open (still API-key-gated; just not throttled)."""
    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 1)
    monkeypatch.setattr(get_settings(), "demo_api_key", None)
    reset_for_tests()
    for _ in range(5):
        r = client.get("/admin/stats")
        assert r.status_code == 200
