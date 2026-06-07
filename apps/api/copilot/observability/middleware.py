"""HTTP middleware: W3C TraceContext + request-scoped log context.

For every incoming request we either:

* **Continue** an upstream trace — if the request carries a valid
  ``traceparent`` header (per `W3C Trace Context`_), we extract the
  ``trace_id`` and mint a new child ``span_id``; or
* **Start** a fresh trace — generate a new ``trace_id`` (16 random
  bytes, 32 hex chars) and ``span_id`` (8 bytes, 16 hex chars).

Either way, the IDs are bound to ``structlog.contextvars`` so every
log statement emitted while handling the request carries them, and
``traceparent`` is echoed back on the response so downstream callers
can follow the trace.

We deliberately use the OpenTelemetry API helpers ``format_trace_id``
and ``format_span_id`` so the IDs are wire-compatible with the OTel
SDK we'll wire up in week 11 — no rewrite of ID generation later.

Why no manual unbind? Starlette runs every request inside its own
asyncio task, which gets a fresh ``contextvars`` copy. The bindings
are therefore naturally scoped to one request and don't leak into
the next. ``clear_contextvars()`` at the top is a defensive paranoia
fence.

.. _W3C Trace Context: https://www.w3.org/TR/trace-context/
"""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Request, Response
from opentelemetry.trace import format_span_id, format_trace_id

_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[\da-f]{2})-"
    r"(?P<trace_id>[\da-f]{32})-"
    r"(?P<span_id>[\da-f]{16})-"
    r"(?P<flags>[\da-f]{2})$"
)
_INVALID_TRACE_ID = "0" * 32
_INVALID_SPAN_ID = "0" * 16


def _new_trace_id() -> str:
    return format_trace_id(int.from_bytes(os.urandom(16), "big"))


def _new_span_id() -> str:
    return format_span_id(int.from_bytes(os.urandom(8), "big"))


async def request_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Bind W3C trace context to the log scope of this request."""
    structlog.contextvars.clear_contextvars()

    incoming = request.headers.get("traceparent")
    if incoming and (m := _TRACEPARENT_RE.match(incoming.strip())):
        incoming_trace = m.group("trace_id")
        # All-zero IDs are reserved per the spec; treat as invalid.
        if incoming_trace != _INVALID_TRACE_ID:
            trace_id = incoming_trace
            span_id = _new_span_id()
        else:
            trace_id = _new_trace_id()
            span_id = _new_span_id()
    else:
        trace_id = _new_trace_id()
        span_id = _new_span_id()

    # ``request_id`` is an alias for ``trace_id`` so the familiar field
    # name still works when grepping ("show me request abc...") without
    # diverging from the W3C identifier.
    structlog.contextvars.bind_contextvars(
        trace_id=trace_id,
        span_id=span_id,
        request_id=trace_id,
    )

    response = await call_next(request)
    # Echo so downstream callers can continue the trace and so curl -v
    # users can copy the ID to grep logs.
    response.headers["traceparent"] = f"00-{trace_id}-{span_id}-01"
    return response
