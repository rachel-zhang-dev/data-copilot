"""Observability primitives — structured logging, tracing, metrics.

Week 3: structured logging via ``structlog`` + W3C TraceContext IDs
through ``RequestIDMiddleware``. Both stdlib ``logging`` and native
structlog calls flow into the same processor pipeline so third-party
library logs are also structured.

The pipeline grows incrementally:

* W3  — structured logging + request_id / trace_id              ← you are here
* W7  — business metrics (Prometheus client) wired into ``/metrics``
* W10 — distributed tracing backends (Jaeger first)
* W11 — full OpenTelemetry SDK takeover (replaces ad-hoc IDs)
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

from copilot.observability.logging import get_logger, setup_logging
from copilot.observability.middleware import request_id_middleware

__all__ = [
    "get_logger",
    "log_context",
    "request_id_middleware",
    "setup_logging",
]


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Bind fields to ``structlog.contextvars`` for the lifetime of the block.

    Use this when you want a scope-bounded enrichment that doesn't leak
    once the block exits. The HTTP middleware already covers per-request
    ``trace_id`` / ``span_id`` / ``request_id``; this helper is for
    additional scoping — most commonly the ``conversation_id`` bound at
    the ``/ask`` endpoint right before invoking the agent::

        async def ask(req):
            conv_id = req.conversation_id or str(uuid.uuid4())
            with log_context(conversation_id=conv_id):
                result = await graph.ainvoke(state, config=...)
            return result
    """
    tokens = structlog.contextvars.bind_contextvars(**fields)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
