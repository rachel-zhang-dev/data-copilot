"""Deploy-side defence: API-key gate + per-IP rate limiter (Phase 3.2 / ADR 0024).

Both pieces are no-ops in their default configuration so local dev is
unaffected. They turn ON when the corresponding env vars are set on a
public deploy:

* ``DEMO_API_KEY`` — shared secret. Required header on protected
  routes; missing / wrong → 401. The Next.js Route Handlers forward
  this server-side so legitimate browser users never see the key.

* ``RATE_LIMIT_PER_MINUTE`` — per-IP request budget on the expensive
  routes (``/ask``, ``/ask/stream``, ``/mcp/*``). Sliding-window
  in-memory counter; over budget → 429.

Why hand-rolled instead of ``slowapi`` / ``starlette-limiter``:

* The whole module is ~80 lines including docstring. The rate-limit
  shape (one window, one key function, one bypass list) doesn't
  warrant a dependency.
* slowapi attaches at the route-decorator level, which means the
  Streamable HTTP MCP sub-app (mounted at ``/mcp``) can't pick up
  the limits without extra wiring. A middleware that owns the whole
  request path handles both surfaces uniformly.
* Tests can poke the limiter directly without standing up the FastAPI
  app — ``RateLimit.allow(key)`` is the entire public surface.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from copilot.config import get_settings

log = logging.getLogger(__name__)


# Routes that bypass BOTH the API-key gate and the rate limit. The
# health probe and Prometheus scrape need to stay open for Fly /
# external monitors; ``/admin/stats`` is gated since it exposes
# cache stats + config knobs that aren't strictly secret but are
# operational telemetry.
_BYPASS_PATHS = frozenset(("/health", "/metrics"))

# Routes whose per-call cost is dominated by an LLM call (the only
# unbounded cost surface). Rate-limited under
# ``RATE_LIMIT_PER_MINUTE``; the API-key gate covers a broader set.
_RATE_LIMITED_PREFIXES = ("/ask", "/mcp")


class RateLimit:
    """Tiny sliding-window per-key rate limiter.

    Each key (typically an IP) maps to a deque of recent request
    timestamps. ``allow`` drops timestamps outside the window before
    deciding whether to admit. A lazy GC bounds the dict size when
    the process sees many distinct keys; the deque approach plus the
    GC keeps memory at O(N_active_keys * window_max_calls).

    Not thread-safe in the strict sense, but uvicorn + asyncio runs
    middleware sequentially per worker — the worst case is a couple
    of timestamps racing into the deque, which doesn't change the
    bound by more than 1 request. Acceptable.
    """

    def __init__(self, max_per_window: int, window_seconds: int) -> None:
        self.max = max_per_window
        self.window = window_seconds
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        """Return True iff this request fits the budget. Side-effect:
        records the request when admitted."""
        if self.max <= 0:
            return True
        now = time.monotonic()
        cutoff = now - self.window
        bucket = self._buckets[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.max:
            return False
        bucket.append(now)
        # Lazy GC — when the dict grows past a soft cap, drop empty
        # entries. Cheap O(N) scan but only runs occasionally.
        if len(self._buckets) > 4096:
            self._gc(cutoff)
        return True

    def _gc(self, cutoff: float) -> None:
        empty_keys = [
            k for k, b in self._buckets.items() if not b or b[-1] < cutoff
        ]
        for k in empty_keys:
            del self._buckets[k]


# Process-local singleton; settings are read once at module import.
# Tests that want a fresh limiter can call ``reset_for_tests``.
_settings = get_settings()
_rate_limit = RateLimit(
    max_per_window=_settings.rate_limit_per_minute,
    window_seconds=60,
)


def reset_for_tests() -> None:
    """Reset the module-level limiter. Tests call this to avoid
    leaking state between cases."""
    global _rate_limit
    _rate_limit = RateLimit(
        max_per_window=get_settings().rate_limit_per_minute,
        window_seconds=60,
    )


def _client_ip(request: Request) -> str:
    """Pick the client IP for the rate-limit key.

    Behind Fly's edge the real IP is in ``Fly-Client-IP``; behind
    generic proxies it's in ``X-Forwarded-For`` (first hop). Fall
    back to ``request.client.host`` for direct connections / tests.
    """
    return (
        request.headers.get("fly-client-ip")
        or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )


async def security_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[JSONResponse]],
) -> JSONResponse:
    """FastAPI middleware combining the API-key gate and rate limiter.

    Order matters: API-key check runs FIRST so an unauthenticated
    request never burns the rate-limit budget of an attacker scanning
    for the URL. The actual call (``call_next``) only happens after
    both gates admit.
    """
    settings = get_settings()
    path = request.url.path

    # Bypass paths: liveness probe + Prometheus scrape.
    if path in _BYPASS_PATHS:
        return await call_next(request)

    # API-key gate. Disabled when the env var is unset (local dev).
    if settings.demo_api_key:
        provided = request.headers.get("x-api-key")
        if provided != settings.demo_api_key:
            log.info(
                "security: 401 missing/invalid X-API-Key on %s from %s",
                path,
                _client_ip(request),
            )
            return JSONResponse(
                {"detail": "X-API-Key header missing or invalid"},
                status_code=401,
            )

    # Rate limit — only on the LLM-expensive surfaces. Health,
    # metrics, dashboards reads, etc. don't burn DeepSeek.
    if settings.rate_limit_per_minute > 0 and any(
        path == p or path.startswith(p + "/") for p in _RATE_LIMITED_PREFIXES
    ):
        ip = _client_ip(request)
        if not _rate_limit.allow(ip):
            log.info(
                "security: 429 rate-limit on %s for %s (%d/min cap)",
                path,
                ip,
                settings.rate_limit_per_minute,
            )
            return JSONResponse(
                {
                    "detail": (
                        f"rate limit exceeded: max {settings.rate_limit_per_minute} "
                        "requests per minute per IP"
                    )
                },
                status_code=429,
                headers={"Retry-After": "60"},
            )

    return await call_next(request)
