"""FastAPI entry point.

This module is intentionally thin: FastAPI is just the *transport layer*
that exposes our LangGraph agent over HTTP. All actual reasoning happens
inside the agent (see ``copilot.agent``), so the same agent could later
be exposed via a CLI, a Slack bot, or a scheduled job without touching
this file.

Reading guide
-------------
* ``lifespan``           — runs once at startup / shutdown (FastAPI hook).
* ``app.state.graph``    — the compiled LangGraph agent, built once and reused.
* ``/health``            — cheap probe for monitoring & uptime checks.
* ``/ask``               — the only "real" endpoint right now; takes a
  natural-language question and returns an answer (plus, since week 2,
  the SQL that was run and the rows that came back).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from copilot.agent import build_graph
from copilot.config import get_settings
from copilot.db import dispose_engine, get_engine, get_schema_ddl

log = logging.getLogger(__name__)


def _configure_langsmith() -> None:
    """Wire LangSmith tracing via environment variables.

    LangChain reads ``LANGCHAIN_*`` env vars at module-import time inside
    its many sub-packages, so we set them as early as possible — before
    any chain or graph is invoked.

    If no LangSmith key is configured, we silently skip; tracing is an
    enhancement, never a requirement.
    """
    settings = get_settings()
    if settings.langsmith_tracing and settings.langsmith_api_key:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
        os.environ["LANGCHAIN_ENDPOINT"] = settings.langsmith_endpoint


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown hook.

    Startup:
      1. Configure LangSmith tracing.
      2. Eagerly build the SQLAlchemy engine and warm the schema cache.
         Doing this here turns "Postgres is down" into a startup error
         (visible in logs) instead of a confusing 500 on the first
         /ask request.
      3. Build the LangGraph agent once and stash it on ``app.state``.

    Shutdown:
      * Dispose the connection pool cleanly so Postgres does not log
        spurious connection-reset warnings.
    """
    _configure_langsmith()
    get_engine()
    schema = get_schema_ddl()
    log.info("schema cache warmed (%d chars)", len(schema))
    app.state.graph = build_graph()
    try:
        yield
    finally:
        dispose_engine()


app = FastAPI(
    title="Data Copilot API",
    description="Enterprise Text-to-SQL agent.",
    version="0.2.0",
    lifespan=lifespan,
)

# CORS lets the Next.js dev server (port 3000) call this API (port 8000)
# from a browser. In production we will lock this down to the real frontend
# origin. Middlewares run on every request, before the route handler.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------
# Pydantic models double as: (a) runtime validators for incoming JSON,
# (b) OpenAPI schema generators for /docs, and (c) static type hints.


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    """Response envelope for ``POST /ask``.

    Only ``answer`` is guaranteed populated. The other fields are
    introspection data — useful for debugging the agent, for the future
    Next.js UI, and for the evaluation harness. They are ``None`` when
    the question routes through the chitchat branch.
    """

    answer: str
    sql: str | None = None
    rows: list[dict[str, Any]] | None = None
    row_count: int | None = None
    error: str | None = None
    # Number of LLM attempts the agent made on the SQL pipeline.
    # 1 means first-shot success; >1 means self-healing kicked in.
    # 0 happens for chitchat questions that never hit the SQL path.
    attempts: int = 1
    # Per-attempt history (sql + error + class). Off by default to keep
    # the response small; populate when ``?debug=true`` is set.
    attempts_history: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Cheap, no external calls.

    Used by Docker / Kubernetes / load balancers to decide whether the
    container is still alive. Return 200 fast, no I/O.
    """
    return {"status": "ok", "version": app.version}


@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest, debug: bool = False) -> AskResponse:
    """Run the agent on a single user question.

    Flow:
        1. Pull the pre-compiled graph off ``app.state`` (set in ``lifespan``).
        2. Invoke the graph asynchronously — this is the call that may
           reach out to the LLM and the database.
        3. Wrap the result in a typed response model.

    Set ``?debug=true`` to also receive ``attempts_history`` — the full
    list of failed (sql, error) pairs the self-healing loop walked
    through. Off by default because it can be large.
    """
    graph = app.state.graph
    result = await graph.ainvoke({"question": req.question})

    # Each entry in ``attempts`` is a recorded FAILURE; the total number
    # of LLM SQL-generation calls is therefore ``failures + 1`` whenever
    # the SQL pipeline ran at all (i.e. ``sql`` is set). 0 for chitchat.
    failures = result.get("attempts") or []
    attempts_count = (len(failures) + 1) if result.get("sql") else 0

    return AskResponse(
        answer=result.get("answer", ""),
        sql=result.get("sql"),
        rows=result.get("sql_result"),
        row_count=result.get("row_count"),
        error=result.get("error"),
        attempts=attempts_count,
        attempts_history=list(failures) if debug else None,
    )
