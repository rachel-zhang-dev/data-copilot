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
  the SQL that was run and the rows that came back; since week 5, an
  optional ``conversation_id`` for multi-turn dialogues).
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langgraph.types import Command
from pydantic import BaseModel, model_validator

from copilot.agent import build_graph
from copilot.checkpointer import (
    conversation_lock,
    dispose_checkpointer,
    get_checkpointer,
    setup_checkpointer,
)
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
      3. Initialise the LangGraph PostgresSaver and ensure its tables
         exist (idempotent ``CREATE TABLE IF NOT EXISTS``).
      4. Build the LangGraph agent once with the checkpointer attached
         and stash it on ``app.state``.

    Shutdown:
      * Dispose the SQLAlchemy pool and the checkpointer pool cleanly.
    """
    _configure_langsmith()
    get_engine()
    schema = get_schema_ddl()
    log.info("schema cache warmed (%d chars)", len(schema))
    await setup_checkpointer()
    app.state.graph = build_graph(checkpointer=await get_checkpointer())
    try:
        yield
    finally:
        await dispose_checkpointer()
        dispose_engine()


app = FastAPI(
    title="Data Copilot API",
    description="Enterprise Text-to-SQL agent.",
    version="0.8.0",
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
    """Input envelope for ``POST /ask``.

    A request is one of two shapes:

    * **Fresh turn**: ``question`` populated, ``resume`` omitted. Starts
      a new conversation (when ``conversation_id`` is also omitted) or
      adds a follow-up to an existing one.
    * **Resume turn** (week 7): ``resume`` populated to ``"approve"``
      or ``"reject"``, ``conversation_id`` required, ``question``
      omitted. Used to answer a pending human-in-the-loop confirmation.

    The model validator below rejects any other combination so the
    server never has to guess intent from a half-filled body.
    """

    question: str | None = None
    # When omitted, the server allocates a fresh UUID and the call
    # starts a new conversation. Pass it back on subsequent calls to
    # continue the same thread.
    conversation_id: str | None = None
    # Week 7 — present only on the second leg of a HITL pause.
    resume: Literal["approve", "reject"] | None = None

    @model_validator(mode="after")
    def _check_question_or_resume(self) -> AskRequest:
        if self.resume is None and not (self.question and self.question.strip()):
            raise ValueError("question is required unless resume is provided")
        if self.resume is not None:
            if self.question is not None:
                raise ValueError("question must be omitted when resume is provided")
            if not self.conversation_id:
                raise ValueError("conversation_id is required to resume a paused thread")
        return self


class AskResponse(BaseModel):
    """Response envelope for ``POST /ask``.

    Only ``answer`` is guaranteed populated (and even that is empty
    when the agent is paused awaiting confirmation). The other fields
    are introspection data — useful for debugging the agent, for the
    Next.js UI, and for the evaluation harness. They are ``None`` when
    the question routes through the chitchat branch.

    ``status`` distinguishes the three legitimate outcomes:

    * ``"ok"``                    — turn finished; ``answer`` is final.
    * ``"pending_confirmation"``  — graph paused at the HITL gate (week 7);
                                     ``pending_risk`` is populated and the
                                     caller is expected to call ``/ask``
                                     again with ``resume="approve"|"reject"``.
    """

    answer: str
    # Always set, including when the caller did not supply one.
    # Returned so the caller can use it for the next turn.
    conversation_id: str
    # 1-based index of THIS turn within the conversation.
    turn_index: int
    sql: str | None = None
    rows: list[dict[str, Any]] | None = None
    row_count: int | None = None
    error: str | None = None
    # Number of LLM SQL-generation calls in THIS turn (1 = first-shot
    # success). 0 happens for chitchat turns that never hit SQL.
    attempts: int = 1
    # Per-attempt history (sql + error + class). Off by default to keep
    # the response small; populate when ``?debug=true`` is set.
    attempts_history: list[dict[str, Any]] | None = None
    # Week 7 — HITL surface.
    status: Literal["ok", "pending_confirmation"] = "ok"
    pending_risk: dict[str, Any] | None = None
    # Week 8 — structured insight + chart spec.
    # ``insight`` is the ``{headline, bullets, metric_highlights}``
    # envelope produced by ``summarize_result_node``; ``None`` when the
    # LLM JSON parse fell back to the legacy NL-only path or the turn
    # never reached the data success branch (chitchat / terminal error).
    insight: dict[str, Any] | None = None
    # ``chart_kind`` is one of ``"kpi"`` | ``"bar"`` | ``"line"`` |
    # ``"grouped_bar"`` | ``"table"`` on the data success path, else
    # ``None``. ``chart_spec`` is a Vega-Lite v5 spec for ``bar`` /
    # ``line`` / ``grouped_bar``; ``None`` for ``kpi`` and ``table``
    # (the UI renders those directly from ``rows``).
    chart_kind: Literal["kpi", "bar", "line", "grouped_bar", "table"] | None = None
    chart_spec: dict[str, Any] | None = None


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


def _first_interrupt_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the interrupt payload from a paused ``ainvoke`` result.

    LangGraph surfaces active interrupts via the ``__interrupt__`` key
    on the returned state — a list of ``Interrupt(value=..., id=...)``.
    We return the first one's ``value`` (only one HITL gate exists
    today; the list-of-one shape leaves room for parallel branches
    to introduce more later).
    """
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0]
    value = getattr(first, "value", None)
    return value if isinstance(value, dict) else None


@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest, debug: bool = False) -> AskResponse:
    """Run the agent on a single user question, or resume a paused turn.

    Multi-turn dialogue is opt-in via ``conversation_id``. Same id =
    continuation of the same thread (history loaded from Postgres);
    omitted or new id = fresh conversation.

    Set ``?debug=true`` to also receive ``attempts_history`` — the full
    list of failed (sql, error) pairs the self-healing loop walked
    through.

    Week 7: if the response carries ``status="pending_confirmation"``,
    the agent has paused at a HITL gate. Call ``/ask`` again with the
    same ``conversation_id`` and ``resume="approve"|"reject"`` to
    continue.
    """
    graph = app.state.graph
    conversation_id = req.conversation_id or str(uuid.uuid4())

    # LangGraph keys persistence on ``thread_id``; we treat
    # conversation_id and thread_id as synonyms.
    config: dict[str, Any] = {"configurable": {"thread_id": conversation_id}}

    # Resume requests must target a thread that is actually paused.
    # We probe the checkpointer up front so a stray resume call gets a
    # crisp 400 instead of a confusing no-op run.
    if req.resume is not None:
        snapshot = await graph.aget_state(config)
        if not getattr(snapshot, "interrupts", None):
            raise HTTPException(
                status_code=400,
                detail=(
                    "no pending confirmation on this conversation; pass a "
                    "question instead of resume to start a new turn"
                ),
            )

    # Serialise concurrent writes to the same conversation_id. Without
    # this guard, two near-simultaneous /ask calls on the same thread
    # both read the same baseline and the later commit silently
    # overwrites the earlier turn's diff. Different conversation_ids
    # use different lock keys and stay fully parallel.
    async with conversation_lock(conversation_id):
        if req.resume is not None:
            result = await graph.ainvoke(Command(resume=req.resume), config=config)
        else:
            result = await graph.ainvoke({"question": req.question}, config=config)

    pending = _first_interrupt_payload(result)
    failures = result.get("attempts") or []
    turn_idx = result.get("turn_index") or 1
    # Count failures recorded during THIS turn only.
    this_turn_failures = [f for f in failures if f.get("turn_idx", 0) == turn_idx]
    # Count total SQL-generation calls in this turn:
    #   * chitchat path never generated SQL                     -> 0
    #   * budget exhausted (error set, all attempts failed)     -> len(failures)
    #   * happy path / self-healed (final attempt succeeded)    -> len(failures) + 1
    # validate_sql_node deliberately leaves ``state.sql`` set on failure so
    # the retry prompt can reference it, which means ``result.sql`` alone
    # cannot disambiguate success from terminal failure — we have to check
    # ``error`` too.
    if not result.get("sql"):
        attempts_count = 0
    elif result.get("error"):
        attempts_count = len(this_turn_failures)
    else:
        attempts_count = len(this_turn_failures) + 1

    if pending is not None:
        # The graph is paused at ``await_confirmation``. We surface the
        # full diagnostic payload to the caller and an empty answer —
        # the user has not actually been answered yet.
        return AskResponse(
            answer="",
            conversation_id=conversation_id,
            turn_index=turn_idx,
            sql=result.get("sql"),
            rows=None,
            row_count=None,
            error=None,
            attempts=attempts_count,
            attempts_history=list(this_turn_failures) if debug else None,
            status="pending_confirmation",
            pending_risk=pending,
        )

    return AskResponse(
        answer=result.get("answer", ""),
        conversation_id=conversation_id,
        turn_index=turn_idx,
        sql=result.get("sql"),
        rows=result.get("sql_result"),
        row_count=result.get("row_count"),
        error=result.get("error"),
        attempts=attempts_count,
        attempts_history=list(this_turn_failures) if debug else None,
        status="ok",
        pending_risk=None,
        insight=result.get("insight"),
        chart_kind=result.get("chart_kind"),
        chart_spec=result.get("chart_spec"),
    )
