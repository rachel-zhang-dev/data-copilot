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
  natural-language question and returns an answer.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from copilot.agent import build_graph
from copilot.config import get_settings


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

    Anything before ``yield`` runs once at startup; anything after runs
    at shutdown. We use this to:

    1. Configure tracing.
    2. Build (compile) the LangGraph agent **once** and stash it on
       ``app.state``. Compiling on every request would waste milliseconds
       and prevent LangGraph from caching internal structure.
    """
    _configure_langsmith()
    app.state.graph = build_graph()
    yield
    # Nothing to clean up yet. When we add a DB pool, close it here.


app = FastAPI(
    title="Data Copilot API",
    description="Enterprise Text-to-SQL agent.",
    version="0.1.0",
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
# Defining them as classes — not loose dicts — is what unlocks FastAPI's
# auto-validation and auto-documentation.

class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    """Liveness probe. Cheap, no external calls.

    Used by Docker / Kubernetes / load balancers to decide whether the
    container is still alive. Return 200 fast, no I/O.
    """
    return {"status": "ok", "version": app.version}


@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest) -> AskResponse:
    """Run the agent on a single user question.

    Flow:
        1. Pull the pre-compiled graph off ``app.state`` (set in ``lifespan``).
        2. Invoke the graph asynchronously — this is the call that may
           reach out to the LLM, vector store, and database.
        3. Wrap the answer in a typed response model.
    """
    graph = app.state.graph
    result = await graph.ainvoke({"question": req.question})
    return AskResponse(answer=result["answer"])
