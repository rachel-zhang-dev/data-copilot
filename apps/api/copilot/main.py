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

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, model_validator

from copilot.agent import build_graph
from copilot.agents import build_supervisor_graph
from copilot.cache import get_embedding_cache
from copilot.checkpointer import (
    conversation_lock,
    dispose_checkpointer,
    get_checkpointer,
    setup_checkpointer,
)
from copilot.config import get_settings
from copilot.db import dispose_engine, get_engine, get_schema_ddl
from copilot.dashboards import (
    add_item as dashboard_add_item,
    create_dashboard,
    delete_dashboard,
    delete_item as dashboard_delete_item,
    get_dashboard,
    list_dashboards,
    update_dashboard,
    update_item as dashboard_update_item,
)
from copilot.saved import (
    add_previews_async,
    first_question_async,
    list_saved,
    replay_conversation_async,
    save_conversation,
    unsave_conversation,
)

log = logging.getLogger(__name__)


# Module-level boot wall clock so ``/admin/stats`` can report uptime
# without needing a heavier process-info dep.
_BOOT_TIME = time.time()


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
    # Week 12.5: the SQL Specialist is still the same week-12 graph,
    # but a Supervisor wraps it (rule-based router + Analyst worker).
    # ``app.state.graph`` is now the supervisor; the inner Specialist
    # is what ``/ask/stream`` streams from.
    sql_graph = build_graph(checkpointer=await get_checkpointer())
    app.state.sql_graph = sql_graph
    app.state.graph = build_supervisor_graph(sql_graph)
    try:
        yield
    finally:
        await dispose_checkpointer()
        dispose_engine()


app = FastAPI(
    title="Data Copilot API",
    description="Enterprise Text-to-SQL agent.",
    version="0.12.0",
    lifespan=lifespan,
)

# CORS is driven by ``CORS_ORIGINS`` (comma-separated). Default permits
# the local Next.js dev server; production deploys (Fly.io, week 11)
# override the env to the real front-end origin so the middleware never
# accidentally accepts unknown origins.
_cors_origins = get_settings().cors_origins_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
log.info("CORS allow_origins=%s", _cors_origins)


# Prometheus metrics (week 11). Instrumentator hooks into FastAPI's
# ASGI lifecycle to populate the default request/response counters
# (latency, status codes, in-flight gauges). When ``metrics_enabled``
# is False (tests, CI) we skip the registration so the default
# Prometheus registry stays clean and parallel tests don't trip the
# "metric already registered" guard.
if get_settings().metrics_enabled:
    _instrumentator = Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/metrics", "/health"],
    )
    _instrumentator.instrument(app).expose(
        app,
        endpoint="/metrics",
        include_in_schema=False,
        tags=["observability"],
    )
    log.info("/metrics exposed via prometheus_fastapi_instrumentator")


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
    # Week 9 — cumulative cost breakdown for the *conversation* up to
    # and including this turn. Always present (zero-initialised) so
    # consumers never have to ``.get(...)`` defensively.
    cost: dict[str, Any] | None = None
    # Week 12.5 — multi-agent outputs. ``analyst`` is the structured
    # envelope (``anomalies`` / ``followups`` / optional ``drill_down``);
    # ``drill_downs`` is the list of recursive Specialist invocations
    # the Analyst triggered (each entry is a full AskResponse-shaped
    # dict, in invocation order). Both are ``None`` / empty when
    # ANALYST_ENABLED is off or the supervisor short-circuited.
    analyst: dict[str, Any] | None = None
    drill_downs: list[dict[str, Any]] = []
    # Phase 1.1 — schema coverage gate + explorer (ADR 0016).
    # ``intent`` is the three-way classifier verdict; the FE uses it to
    # choose between the data answer, the schema tour, and chitchat.
    # ``coverage`` carries the structured payload for the two new
    # branches: ``verdict="refuse"`` with bullets + suggested_questions
    # for refused turns, or ``verdict="explore"`` with topics +
    # suggested_questions for the schema tour. ``None`` on chitchat or
    # plain data turns where the gate voted ``ok``.
    intent: Literal["data", "chitchat", "schema_explore", "investigate"] | None = None
    coverage: dict[str, Any] | None = None
    # Phase 1.2 — pattern detector (ADR 0017). Structured statistical
    # findings (outliers / trends) computed deterministically over the
    # SQL result. The same findings drive pattern bullets that get
    # prepended to ``insight.bullets`` so the existing InsightPanel
    # surfaces them with zero FE change; ``patterns`` is kept on the
    # response so the FE can later render badges / chart annotations
    # without re-running the stats. ``None`` / empty when the result
    # set was too small or no numeric column existed.
    patterns: list[dict[str, Any]] | None = None
    # Phase 2.3 — SQL critic verdict (ADR 0021). One additional LLM
    # call after ``execute_sql`` decides whether the SQL truly
    # answers the question. Shape:
    # ``{verdict: "ok"|"suspicious"|"wrong", reason: str, concerns: [...]}``.
    # FE renders a ⚠️ "low confidence" badge when verdict is not ``ok``;
    # ``wrong`` verdicts trigger one self-healing retry before the
    # badge is shown. ``None`` on chitchat / refused / explore / etc.
    critic: dict[str, Any] | None = None


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


# ---------------------------------------------------------------------------
# Saved conversations (Phase 1.4 / ADR 0019)
# ---------------------------------------------------------------------------


class SaveConversationRequest(BaseModel):
    """Body for ``POST /conversations/{thread_id}/save``.

    All fields optional — when ``title`` is ``None`` and the thread
    isn't already bookmarked, the service auto-derives a title from
    the first user message (zero-friction Pin button)."""

    title: str | None = None
    tags: list[str] | None = None
    notes: str | None = None


@app.post("/conversations/{thread_id}/save")
async def save_conversation_endpoint(
    thread_id: str, req: SaveConversationRequest
) -> dict[str, Any]:
    """Pin (or update) a saved-conversation bookmark.

    Idempotent — calling again with new fields updates them and bumps
    ``updated_at`` without resetting ``pinned_at`` (so the FE sort
    order stays stable across title fixes).

    Auto-title path: when the caller didn't supply a ``title`` (the
    Pin button's zero-friction mode), we go through LangGraph's
    ``aget_state`` to read the conversation's first user question
    and derive a title from it. Reading raw rows out of the
    ``checkpoints`` table would miss the ``dialogue`` field because
    LangGraph stores reducer-driven fields in ``checkpoint_blobs``
    as msgpack — only the ``aget_state`` API reconstructs them.
    """
    first_q: str | None = None
    if req.title is None:
        try:
            first_q = await first_question_async(app.state.sql_graph, thread_id)
        except Exception as exc:  # noqa: BLE001
            # Don't fail the pin just because we can't auto-title —
            # ``derive_title`` falls back to a placeholder.
            log.warning(
                "save_conversation: first_question_async failed for %s: %s",
                thread_id,
                exc,
            )
    return save_conversation(
        thread_id,
        title=req.title,
        tags=req.tags,
        notes=req.notes,
        first_question=first_q,
    )


@app.delete("/conversations/{thread_id}/save")
async def unsave_conversation_endpoint(thread_id: str) -> dict[str, bool]:
    """Drop the bookmark. Underlying LangGraph state is left intact
    so a quick re-pin doesn't lose history."""
    removed = unsave_conversation(thread_id)
    if not removed:
        raise HTTPException(status_code=404, detail="not pinned")
    return {"unsaved": True}


@app.get("/conversations/saved")
async def list_saved_conversations_endpoint() -> dict[str, Any]:
    """Return every bookmark newest-first, with a tiny preview block
    so the FE drawer can render rich list rows without a second
    round-trip per entry. Preview fields come from ``aget_state`` —
    the only supported way to read LangGraph's reducer-driven
    ``dialogue`` field (it lives in ``checkpoint_blobs`` as msgpack
    rather than in the JSON ``channel_values``)."""
    rows = list_saved()
    items = await add_previews_async(app.state.sql_graph, rows)
    return {"items": items}


@app.get("/conversations/{thread_id}/messages")
async def replay_conversation_endpoint(thread_id: str) -> dict[str, Any]:
    """Return the user-visible dialogue for ``thread_id``.

    The FE calls this when the user clicks a saved-conversation row
    to restore history. 404 when the thread has no checkpoint
    (e.g. it was never pinned, or LangGraph state was wiped)."""
    sql_graph = app.state.sql_graph
    try:
        messages = await replay_conversation_async(sql_graph, thread_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"thread_id": thread_id, "messages": messages}


# ---------------------------------------------------------------------------
# Dashboards (Phase 2.1 / ADR 0020)
# ---------------------------------------------------------------------------


class DashboardRequest(BaseModel):
    """Body for ``POST /dashboards`` and ``PATCH /dashboards/{id}``.

    Both fields optional on PATCH; ``title`` required on POST (we
    validate that explicitly in the handler so PATCH stays partial-
    update friendly)."""

    title: str | None = None
    description: str | None = None


class DashboardItemRequest(BaseModel):
    """Body for ``POST /dashboards/{id}/items``.

    The FE sends the full assistant-turn snapshot directly — chart
    spec / insight / rows live on the live ``AskResponse`` and never
    re-enter LangGraph's persisted ``dialogue``, so the only place
    that has the complete payload is the browser at extract time.
    Carrying the snapshot over the wire matches that reality.

    Fields outside the snapshot (``title`` / position / size) are
    optional with sensible defaults; the backend never re-runs the
    SQL to populate them.
    """

    title: str
    sql: str | None = None
    answer: str | None = None
    chart_kind: str | None = None
    chart_spec: dict[str, Any] | None = None
    rows: list[dict[str, Any]] | None = None
    row_count: int | None = None
    insight: dict[str, Any] | None = None
    # Phase 2.3.1 — critic verdict (ADR 0021) preserved on the card
    # so a "suspicious" / "wrong" turn pinned to a dashboard keeps
    # its low-confidence badge. None for cards extracted from ``ok``
    # turns or from chats where the critic flag was disabled.
    critic: dict[str, Any] | None = None
    source_thread_id: str | None = None
    source_turn_index: int | None = None
    position_x: int = 0
    position_y: int = 0
    width: int = 4
    height: int = 3


class DashboardItemPatch(BaseModel):
    """Body for ``PATCH /dashboards/{did}/items/{iid}``.

    Snapshot columns are deliberately absent — the FE can rename a
    card and drag it around, but it can't accidentally rewrite the
    card's data. Re-extracting from a fresh turn is the supported
    "change the underlying snapshot" path.
    """

    title: str | None = None
    position_x: int | None = None
    position_y: int | None = None
    width: int | None = None
    height: int | None = None


@app.post("/dashboards")
async def create_dashboard_endpoint(req: DashboardRequest) -> dict[str, Any]:
    """Create a new (empty) dashboard."""
    if not req.title or not req.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    return create_dashboard(title=req.title.strip(), description=req.description)


@app.get("/dashboards")
async def list_dashboards_endpoint() -> dict[str, Any]:
    """List dashboards newest-touched-first, with per-row item_count
    so the sidebar / index page can render rich tiles without a
    second round-trip."""
    return {"items": list_dashboards()}


@app.get("/dashboards/{dashboard_id}")
async def get_dashboard_endpoint(dashboard_id: str) -> dict[str, Any]:
    """Return one dashboard + its items in render order. 404 when
    the dashboard does not exist."""
    try:
        return get_dashboard(dashboard_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/dashboards/{dashboard_id}")
async def update_dashboard_endpoint(
    dashboard_id: str, req: DashboardRequest
) -> dict[str, Any]:
    """Edit title / description. Fields left as ``None`` are
    preserved; ``updated_at`` is bumped so the list reflects the
    recent edit."""
    try:
        return update_dashboard(
            dashboard_id,
            title=req.title,
            description=req.description,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/dashboards/{dashboard_id}")
async def delete_dashboard_endpoint(dashboard_id: str) -> dict[str, bool]:
    """Cascade-delete dashboard + every item on it."""
    removed = delete_dashboard(dashboard_id)
    if not removed:
        raise HTTPException(status_code=404, detail="not found")
    return {"deleted": True}


@app.post("/dashboards/{dashboard_id}/items")
async def add_dashboard_item_endpoint(
    dashboard_id: str, req: DashboardItemRequest
) -> dict[str, Any]:
    """Add a card to a dashboard from a FE-supplied snapshot.

    Why FE-supplied: the assistant turn's full payload (chart_spec /
    insight / rows) only ever exists on the live ``AskResponse`` —
    LangGraph's persisted ``dialogue`` keeps only ``role / content /
    sql / row_count`` per Turn. So at extract time, the FE has the
    only complete copy. Posting the snapshot over the wire matches
    that reality and keeps the backend completely free of any
    LangGraph state read in the dashboard path.
    """
    try:
        return dashboard_add_item(
            dashboard_id,
            snapshot=req.model_dump(),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/dashboards/{dashboard_id}/items/{item_id}")
async def update_dashboard_item_endpoint(
    dashboard_id: str,  # noqa: ARG001 — kept for URL shape; lookup is by item_id
    item_id: str,
    req: DashboardItemPatch,
) -> dict[str, Any]:
    """Rename + reposition + resize a card. Snapshot data is
    immutable through this surface — re-extract from a fresh turn
    to update the underlying answer."""
    try:
        return dashboard_update_item(
            item_id,
            title=req.title,
            position_x=req.position_x,
            position_y=req.position_y,
            width=req.width,
            height=req.height,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/dashboards/{dashboard_id}/items/{item_id}")
async def delete_dashboard_item_endpoint(
    dashboard_id: str,  # noqa: ARG001 — URL shape only
    item_id: str,
) -> dict[str, bool]:
    """Remove a card. 404 when the item doesn't exist."""
    removed = dashboard_delete_item(item_id)
    if not removed:
        raise HTTPException(status_code=404, detail="not found")
    return {"deleted": True}


@app.get("/admin/stats", tags=["observability"])
async def admin_stats() -> dict[str, Any]:
    """Operator dashboard endpoint (week 11).

    Surfaces:
      * Embedding cache stats (``hits`` / ``misses`` / ``hit_rate`` /
        ``evictions`` / ``size`` / ``max_size`` / ``ttl_seconds``).
      * Process uptime in seconds.
      * Settings snapshot of the knobs ops cares about (model, cache
        backend, retry budgets) — no secrets.

    Deliberately public for now; ADR 0006 puts proper admin auth on
    the Week 13 roadmap. The endpoint reads no PII and the cache
    counters are only useful with the live URL.
    """
    settings = get_settings()
    cache = get_embedding_cache()
    s = cache.stats()
    return {
        "version": app.version,
        "uptime_seconds": int(time.time() - _BOOT_TIME),
        "embedding_cache": {
            "hits": s.hits,
            "misses": s.misses,
            "size": s.size,
            "evictions": s.evictions,
            "max_size": s.max_size,
            "ttl_seconds": s.ttl_seconds,
            "hit_rate": round(s.hit_rate, 3),
            "backend": (
                "redis" if settings.redis_url else "in-memory"
            ),
        },
        "settings": {
            "deepseek_model": settings.deepseek_model,
            "embedding_model": settings.embedding_model,
            "embedding_cache_enabled": settings.embedding_cache_enabled,
            "embedding_cache_max_size": settings.embedding_cache_max_size,
            "embedding_cache_ttl_seconds": settings.embedding_cache_ttl_seconds,
            "llm_max_retries": settings.llm_max_retries,
            "risk_explain_cost_threshold": settings.risk_explain_cost_threshold,
            "app_env": settings.app_env,
        },
    }


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


def _build_ask_response(
    result: dict[str, Any],
    *,
    conversation_id: str,
    debug: bool,
    analyst: dict[str, Any] | None = None,
    drill_downs: list[dict[str, Any]] | None = None,
) -> AskResponse:
    """Project a LangGraph ``ainvoke`` (or fully-consumed ``astream``)
    result into the public ``AskResponse`` shape.

    Centralised here so the streaming endpoint, the legacy ``/ask``
    endpoint, and the week-12.5 supervisor endpoint emit identical
    payloads; any future field added to the response only has to be
    wired in one place. ``analyst`` and ``drill_downs`` are week-12.5
    additions surfaced by the supervisor wrapper below.
    """
    pending = _first_interrupt_payload(result)
    failures = result.get("attempts") or []
    turn_idx = result.get("turn_index") or 1
    this_turn_failures = [f for f in failures if f.get("turn_idx", 0) == turn_idx]
    if not result.get("sql"):
        attempts_count = 0
    elif result.get("error"):
        attempts_count = len(this_turn_failures)
    else:
        attempts_count = len(this_turn_failures) + 1
    cost = result.get("cost")

    if pending is not None:
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
            cost=cost,
            analyst=None,
            drill_downs=[],
            intent=result.get("intent"),
            coverage=result.get("coverage"),
            patterns=result.get("patterns"),
            critic=result.get("critic"),
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
        cost=cost,
        analyst=analyst,
        drill_downs=drill_downs or [],
        intent=result.get("intent"),
        coverage=result.get("coverage"),
        patterns=result.get("patterns"),
        critic=result.get("critic"),
    )


def _build_response_from_supervisor(
    supervisor_state: dict[str, Any],
    *,
    conversation_id: str,
    debug: bool,
) -> AskResponse:
    """Unpack a ``SupervisorState`` into ``AskResponse``.

    Layout assumed:
      * ``supervisor_state['sql_result']`` — the FINAL Specialist
        state (either the original answer, or the drill-down's
        result if Analyst recursed).
      * ``supervisor_state['analyst']`` — typed ``AnalystResponse``
        or ``None``.
      * ``supervisor_state['drill_downs']`` — list of *prior*
        Specialist states (one per recursive invocation). We project
        each into a nested ``AskResponse`` dict so the UI can render
        "drill-down 1 was about Germany customers, top-level answer
        is about the whole world" with one payload.
    """
    sql_result = supervisor_state.get("sql_result") or {}
    analyst_obj = supervisor_state.get("analyst")
    analyst_dump = (
        analyst_obj.model_dump() if analyst_obj is not None and hasattr(analyst_obj, "model_dump")
        else None
    )
    raw_drills = supervisor_state.get("drill_downs") or []
    drill_dumps = [
        _build_ask_response(d, conversation_id=conversation_id, debug=debug).model_dump()
        for d in raw_drills
    ]

    # Phase 1.3 — ``intent`` is the user-visible label for the
    # ORIGINAL question, not whatever the last drill-down's question
    # happened to classify as. The first specialist invocation (the
    # one that processed the user's literal input) ends up in
    # ``drill_downs[0]`` once any drill happens; ``sql_result`` only
    # carries the original intent if no drill happened. Promote the
    # right one here so the AskResponse field is stable.
    final_state = dict(sql_result)
    if raw_drills:
        original_intent = raw_drills[0].get("intent")
        if original_intent is not None:
            final_state["intent"] = original_intent

    return _build_ask_response(
        final_state,
        conversation_id=conversation_id,
        debug=debug,
        analyst=analyst_dump,
        drill_downs=drill_dumps,
    )


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
    # Week 12.5: ``app.state.graph`` is the *supervisor*; the inner
    # SQL Specialist (used by the streaming endpoint) lives at
    # ``app.state.sql_graph``. The supervisor's checkpoint probe goes
    # through the Specialist because that's where ``aget_state`` lives.
    supervisor = app.state.graph
    sql_graph = app.state.sql_graph
    conversation_id = req.conversation_id or str(uuid.uuid4())

    # LangGraph keys persistence on ``thread_id``; we treat
    # conversation_id and thread_id as synonyms.
    config: dict[str, Any] = {"configurable": {"thread_id": conversation_id}}

    # Resume requests must target a thread that is actually paused.
    # We probe the SQL Specialist's checkpointer (the only one with
    # persisted state) up front so a stray resume call gets a crisp
    # 400 instead of a confusing no-op run.
    if req.resume is not None:
        snapshot = await sql_graph.aget_state(config)
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
        supervisor_state = await supervisor.ainvoke(
            {
                "question": req.question,
                "conversation_id": conversation_id,
                "resume": req.resume,
                "debug": debug,
                "hop_count": 0,
                "drill_downs": [],
            },
            config=config,
        )

    return _build_response_from_supervisor(
        supervisor_state, conversation_id=conversation_id, debug=debug
    )


# ---------------------------------------------------------------------------
# Streaming endpoint (week 10)
# ---------------------------------------------------------------------------


# SSE field separator — kept as a module constant so a future tweak to the
# heartbeat shape only touches one place.
_SSE_SEP = "\n\n"

# Heartbeat cadence (week 11). Reverse proxies (Cloudflare ≈100 s, AWS
# ALB 60 s, Fly.io 60 s by default) drop idle SSE connections. We emit
# a comment line every ``_HEARTBEAT_INTERVAL_S`` seconds when no real
# event is in flight; clients ignore comment lines but the bytes keep
# the socket alive.
_HEARTBEAT_INTERVAL_S = 15.0


def _sse_event(event: str, data: Any) -> str:
    """Format one SSE event. ``data`` is JSON-serialised so the client's
    EventSource always sees a single ``data:`` line per event — no need
    to worry about newlines in nested strings."""
    payload = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}{_SSE_SEP}"


def _sse_heartbeat() -> str:
    """SSE comment line. Per the spec, any line starting with ``:`` is a
    comment and is ignored by EventSource implementations. We use it as
    a no-op keepalive."""
    return f": heartbeat {_HEARTBEAT_INTERVAL_S:.0f}s\n\n"


# Node names that are pure plumbing and add visual noise without telling
# the user anything new. Streamed but tagged "internal" so the front-end
# can hide them by default.
_INTERNAL_NODES = frozenset(
    {
        "reset_per_turn",
        "append_to_dialogue",
        "compact_history",
        # Phase 1.1 — ``coverage_check`` is a pre-flight check that runs
        # silently when the gate votes ``ok`` (the common case). The
        # only diff it ever surfaces is the coverage envelope itself,
        # which the front-end picks up via the AskResponse anyway. On
        # ``refuse``, the user sees the downstream ``explain_uncovered``
        # phase explicitly, which is NOT internal.
        "coverage_check",
    }
)


def _phase_payload(node: str, diff: Any) -> dict[str, Any]:
    """Reduce a node's full state diff into a phase payload that's safe
    to ship over the wire.

    Filters out internal bookkeeping fields (``messages``, big blobs
    like ``relevant_schema``) so the SSE stream stays small and the
    client doesn't have to know about LangGraph internals.

    ``diff`` is typed as ``Any`` because LangGraph 1.x occasionally
    emits non-dict update values for terminal / interrupt-adjacent
    chunks (e.g. ``None`` once the graph hits ``END``). Treat anything
    that isn't a mapping as an empty diff rather than crashing the
    whole stream.
    """
    keep = {"intent", "sql", "row_count", "error", "answer", "chart_kind",
            "risk_decision", "turn_index", "coverage", "patterns"}
    if isinstance(diff, dict):
        safe_diff: dict[str, Any] = {k: v for k, v in diff.items() if k in keep}
    else:
        safe_diff = {}
    return {
        "node": node,
        "diff": safe_diff,
        "internal": node in _INTERNAL_NODES,
    }


async def _stream_ask(
    graph: Any,
    payload: Any,
    config: dict[str, Any],
    conversation_id: str,
    *,
    debug: bool,
) -> AsyncIterator[str]:
    """Async generator that yields SSE events for one turn.

    Three event types:
      * ``phase``                — one per node activation.
      * ``pending_confirmation`` — graph paused at the HITL gate.
      * ``done``                 — full ``AskResponse`` JSON.

    On any unhandled exception we emit ``error`` and end the stream
    so the client gets a deterministic signal instead of a half-open
    socket.
    """
    interrupted = False
    try:
        async with conversation_lock(conversation_id):
            stream = graph.astream(
                payload, config=config, stream_mode="updates"
            ).__aiter__()

            # Pull chunks with a heartbeat cadence: any quiet period
            # longer than ``_HEARTBEAT_INTERVAL_S`` yields a comment
            # line to keep reverse-proxy idle timers honest.
            while True:
                try:
                    update = await asyncio.wait_for(
                        stream.__anext__(), timeout=_HEARTBEAT_INTERVAL_S
                    )
                except TimeoutError:
                    yield _sse_heartbeat()
                    continue
                except StopAsyncIteration:
                    break

                # LangGraph 1.2 surfaces interrupts inline as a chunk
                # with key ``__interrupt__`` whose value is a tuple of
                # ``Interrupt`` objects.
                if "__interrupt__" in update:
                    interrupts = update["__interrupt__"]
                    first = interrupts[0] if interrupts else None
                    risk = getattr(first, "value", None) if first is not None else None
                    yield _sse_event(
                        "pending_confirmation",
                        {
                            "conversation_id": conversation_id,
                            "pending_risk": risk,
                        },
                    )
                    interrupted = True
                    break

                for node, diff in update.items():
                    yield _sse_event("phase", _phase_payload(node, diff))

            if not interrupted:
                # Fetch the final cumulative state and project to AskResponse.
                snapshot = await graph.aget_state(config)
                final = dict(snapshot.values)
                response = _build_ask_response(
                    final, conversation_id=conversation_id, debug=debug
                )
                yield _sse_event("done", response.model_dump())
    except Exception as exc:
        log.exception("/ask/stream failed: %s", exc)
        yield _sse_event(
            "error",
            {"detail": str(exc), "type": type(exc).__name__},
        )


@app.post("/ask/stream")
async def ask_stream(req: AskRequest, debug: bool = False) -> StreamingResponse:
    """Stream the agent's per-node progress over Server-Sent Events.

    Wire format and event taxonomy are documented in ADR 0011. The
    request body matches ``/ask``; on HITL pause the stream ends with
    a ``pending_confirmation`` event and the client is expected to
    call ``/ask`` (non-streaming) with ``resume="approve"|"reject"``
    to continue.

    Note on caching headers: we set ``Cache-Control: no-cache`` and
    ``X-Accel-Buffering: no`` so reverse proxies (Nginx, Cloudflare)
    do not buffer the response — without this, SSE would only flush
    when the connection closed, defeating the point of streaming.

    Week 12.5: the streaming endpoint deliberately streams from the
    SQL Specialist (``app.state.sql_graph``), NOT the supervisor.
    Reason: the supervisor sees the Specialist's whole run as a
    single sub-graph chunk, which would collapse all the per-node
    phase events the UI relies on. Multi-agent Analyst output stays
    on the non-streaming ``/ask`` path; surfacing it over SSE is
    tracked in ADR 0014 future work.
    """
    graph = app.state.sql_graph
    conversation_id = req.conversation_id or str(uuid.uuid4())
    config: dict[str, Any] = {"configurable": {"thread_id": conversation_id}}

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
        payload: Any = Command(resume=req.resume)
    else:
        payload = {"question": req.question}

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _stream_ask(graph, payload, config, conversation_id, debug=debug),
        media_type="text/event-stream",
        headers=headers,
    )
