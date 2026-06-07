"""Structured logging via structlog.

Initialised once at process startup (see ``main.py`` lifespan). Two
render modes selected by ``APP_ENV``:

* ``development`` → ``ConsoleRenderer`` (coloured, human-readable)
* ``production``  → ``JSONRenderer``    (one event per line, Loki/ELK)

stdlib ``logging`` continues to work — every ``logging.getLogger(__name__)``
call flows through ``ProcessorFormatter`` so third-party libraries
(FastAPI, SQLAlchemy, httpx, LangChain) emit the same structured shape.

Three contextvars are merged into every event automatically (bound by
``RequestIDMiddleware`` + the ``/ask`` endpoint):

* ``trace_id``        — 32 hex chars, W3C TraceContext format
* ``span_id``         — 16 hex chars, W3C TraceContext format
* ``request_id``      — alias for ``trace_id`` (familiar field name)
* ``conversation_id`` — set in ``/ask`` before invoking the agent

Secret redaction: any field whose key matches ``*_key`` / ``*_token`` /
``*_secret`` / ``password`` / ``passwd`` / ``pwd`` is replaced with
``"***"`` before rendering. Defensive only — we still avoid logging
secrets by hand.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from copilot.config import get_settings

_SECRET_KEY_PATTERN = re.compile(
    r"(?i)^(.*_)?(api_?key|key|token|secret|password|passwd|pwd)$"
)


def _redact_secrets(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """Replace values of secret-looking field names with ``"***"``."""
    for k in list(event_dict.keys()):
        if isinstance(k, str) and _SECRET_KEY_PATTERN.match(k):
            event_dict[k] = "***"
    return event_dict


def setup_logging() -> None:
    """Configure structlog and bridge stdlib logging into the same pipeline.

    Idempotent: subsequent calls re-configure cleanly. **Must run before
    any log call** to take effect on early records — ``main.py`` calls
    this immediately after imports.
    """
    settings = get_settings()
    is_prod = settings.app_env.lower() == "production"
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Enrichment that runs regardless of whether the log came from
    # structlog directly or via the stdlib bridge.
    # Note: we deliberately omit ``format_exc_info`` here — modern
    # ConsoleRenderer / JSONRenderer pretty-print ``exc_info`` natively,
    # and including ``format_exc_info`` upstream stringifies the
    # exception too early, breaking that. ``StackInfoRenderer`` is kept
    # so ``log.info("...", stack_info=True)`` still works.
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _redact_secrets,
    ]

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if is_prod
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    # ---- Native structlog path ----
    # Last processor hands off to the stdlib ``ProcessorFormatter`` so
    # both paths render through one place.
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # ---- stdlib bridge ----
    # ``foreign_pre_chain`` enriches records coming from libraries that
    # use the standard ``logging`` module so they look like structlog
    # events when rendered.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processor=renderer,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace any handlers configured by uvicorn / FastAPI at import time
    # so we are the single source of truth.
    root.handlers = [handler]
    root.setLevel(level)

    # Uvicorn ships its own formatters; hand them off to the root logger
    # by clearing their handlers and letting records propagate up.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog-bound logger.

    Equivalent to ``structlog.get_logger(name)`` but typed for app code.
    """
    return structlog.get_logger(name)  # type: ignore[no-any-return]
