"""MCP server — exposes the data-copilot agent as Model Context Protocol tools.

Phase 3.0 / ADR 0022.

Two transports supported by the same module:

* **stdio** — for local LLM clients (Claude Desktop, Cursor, Cline, etc.).
  Invoked via ``uv run python -m copilot.mcp_server`` and configured by the
  client's MCP config file. Each stdio invocation is its own process and
  gets its own graph + checkpointer.

* **Streamable HTTP** — mounted at ``/mcp`` on the FastAPI app (see
  ``main.py``). Remote LLM clients (Databricks Genie, hosted Claude, web
  apps that ship an MCP client) connect to a single URL. The graph is
  shared with the rest of the FastAPI process via the module-level
  ``_get_graph`` cache, so we don't pay for two LangGraph instances when
  the same Python process serves both ``/ask`` and ``/mcp``.

Tools (six of them, mirroring the six things this agent does well):

  * ``ask_data``         — end-to-end NL question → answer.
  * ``list_tables``      — schema discovery for an unfamiliar DB.
  * ``describe_table``   — DDL + sample row for one table.
  * ``run_select``       — escape hatch for already-written SQL, gated by
                           the same ``sql_safety`` AST validator the agent
                           uses internally.
  * ``list_dashboards``  — saved dashboards index.
  * ``get_dashboard``    — one dashboard + all its cards (snapshot model).

Plus one resource:

  * ``schema://overview`` — full DDL blob, suitable for stuffing into an
                            LLM's context as a single read.

Design choices (full rationale in ADR 0022):

* We use FastMCP v3.x (the standard wrapper around the MCP Python SDK)
  rather than the low-level SDK. The decorator-based API generates
  JSON-schema-typed tool descriptors from Python type hints; we don't
  duplicate that metadata anywhere.
* ``ask_data`` returns a COMPACT payload (first 10 rows, no chart spec)
  so the LLM client's context stays small. Callers that want the full
  shape can hit ``/ask`` over HTTP instead.
* All tools that touch the DB are async-safe by virtue of running the
  sync SQLAlchemy calls from inside an async function — FastMCP runs
  sync tools in a thread pool automatically.
* Graph construction is lazy + cached. Both stdio and HTTP-mount paths
  share the same singleton; ``_get_graph`` is idempotent.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastmcp import FastMCP

from copilot.agent import build_graph
from copilot.agent.sql_safety import SqlSafetyError, validate_and_rewrite
from copilot.checkpointer import get_checkpointer, setup_checkpointer
from copilot.dashboards import get_dashboard as _get_dashboard
from copilot.dashboards import list_dashboards as _list_dashboards
from copilot.db import get_engine, get_schema_ddl, get_table_ddl
from copilot.db import list_tables as _list_tables
from copilot.db import run_select as _run_select

log = logging.getLogger(__name__)


mcp: FastMCP = FastMCP(name="data-copilot")
"""The single MCP server instance.

Exported at module level so both ``mcp.run()`` (stdio entry point) and
``mcp.http_app()`` (HTTP mount in ``main.py``) operate on the same set
of registered tools.
"""


# ---------------------------------------------------------------------------
# Lazy graph cache
# ---------------------------------------------------------------------------


_graph_cache: dict[str, Any] = {}
"""Module-level cache for the compiled LangGraph + checkpointer.

Keyed by the literal string ``"sql_graph"`` so a typo in a future
extension fails loudly rather than silently caching the wrong thing.
``main.lifespan`` and the stdio entry point both end up calling
``_ensure_graph`` on first use — neither pays the cost twice."""


async def _ensure_graph() -> Any:
    """Return the compiled SQL Specialist graph, building it on first call.

    Idempotent. Safe to call from any code path (FastAPI tool handler,
    stdio entry point, test). The checkpointer's own singleton means a
    second ``setup_checkpointer()`` is a no-op once the pool is wired.
    """
    if "sql_graph" not in _graph_cache:
        # Warm the SQLAlchemy pool first so a missing ``DATABASE_URL``
        # fails immediately (with a clear message) rather than buried
        # inside the first tool call.
        get_engine()
        await setup_checkpointer()
        cp = await get_checkpointer()
        _graph_cache["sql_graph"] = build_graph(checkpointer=cp)
        log.info("mcp_server: SQL Specialist graph compiled + cached")
    return _graph_cache["sql_graph"]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool
async def ask_data(
    question: str,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Ask a natural-language question about the database and get a structured answer.

    Runs the full agent pipeline: intent classification → schema RAG →
    coverage gate → SQL generation → static safety check → planner-cost
    risk gate → execution → SQL critic review → summarisation. Each
    layer can refuse or retry; the response shape never changes.

    Returns a COMPACT payload suitable for embedding in an LLM client's
    context: the natural-language answer, the SQL that ran, the first
    10 rows, the structured insight headline + bullets, and the critic's
    verdict (so the caller can decide how much to trust the number).
    Full row sets and chart specs are deliberately omitted — the LLM
    client doesn't need them, and if a human wants them they can hit
    ``/ask`` over HTTP.

    Pass ``conversation_id`` from a previous response to continue the
    same thread (multi-turn dialogue). Omit it on the first call; a
    fresh UUID is allocated and returned.
    """
    graph = await _ensure_graph()
    cid = conversation_id or str(uuid.uuid4())
    config: dict[str, Any] = {"configurable": {"thread_id": cid}}
    result = await graph.ainvoke({"question": question}, config=config)

    critic = result.get("critic") or {}
    insight = result.get("insight") or {}
    rows = result.get("sql_result") or []

    return {
        "conversation_id": cid,
        "turn_index": result.get("turn_index"),
        "answer": result.get("answer", ""),
        "sql": result.get("sql"),
        "row_count": result.get("row_count"),
        # Cap at 10 rows so the response stays under ~10 KB even for
        # wide tables. Callers wanting the full set can use ``run_select``
        # with the same SQL.
        "rows_preview": rows[:10],
        "insight_headline": insight.get("headline"),
        "insight_bullets": insight.get("bullets") or [],
        "critic_verdict": critic.get("verdict"),
        "critic_reason": critic.get("reason"),
        "critic_concerns": critic.get("concerns") or [],
        "intent": result.get("intent"),
        "error": result.get("error"),
    }


@mcp.tool
def list_tables() -> list[str]:
    """List every business table in the database, alphabetically sorted.

    Excludes internal tables the agent owns (``schema_embeddings``,
    ``schema_profiles``, ``saved_conversations``, ``dashboards``,
    ``dashboard_items``, LangGraph checkpoints) so the LLM never
    accidentally composes SQL against them.

    Useful as a first call when exploring an unfamiliar schema —
    combine with ``describe_table`` to learn what each one holds.
    """
    return _list_tables()


@mcp.tool
def describe_table(name: str) -> str:
    """Return the DDL + column types + a sample row for one table.

    The output is the same LLM-friendly format the SQL agent itself
    sees during schema retrieval, so an MCP client can reproduce the
    agent's reasoning step-by-step if it wants to.
    """
    return get_table_ddl([name])


@mcp.tool
def run_select(sql: str, max_rows: int = 100) -> dict[str, Any]:
    """Execute one read-only SELECT against the database.

    The agent's static safety layer gates this — exactly the same
    ``sqlglot`` AST validator that ``validate_sql_node`` uses inside
    the LangGraph pipeline. Stacked statements, non-SELECT roots,
    ``SELECT … INTO``, and ``FOR UPDATE / SHARE`` locks all reject
    with ``error: "unsafe_sql: …"``. A missing ``LIMIT`` is auto-
    injected at ``max_rows``.

    Use this as an escape hatch when you already know the SQL you
    want and would rather skip the LLM round-trip ``ask_data`` does.
    Returns ``{sql, rows, row_count, error}`` — ``error`` is non-null
    on any failure (validation OR execution); ``rows`` is non-null on
    success.
    """
    try:
        rewritten = validate_and_rewrite(sql, max_rows=max_rows)
    except SqlSafetyError as exc:
        return {
            "sql": sql,
            "rows": None,
            "row_count": None,
            "error": f"unsafe_sql: {exc}",
        }
    try:
        rows = _run_select(rewritten)
    except Exception as exc:
        return {
            "sql": rewritten,
            "rows": None,
            "row_count": None,
            "error": f"execution_failed: {exc}",
        }
    return {
        "sql": rewritten,
        "rows": rows,
        "row_count": len(rows),
        "error": None,
    }


@mcp.tool
def list_dashboards() -> list[dict[str, Any]]:
    """List every saved dashboard, newest-touched-first.

    Each entry has ``id``, ``title``, ``description``, ``item_count``,
    ``created_at``, ``updated_at``. Use ``get_dashboard`` with the id
    to read the full grid + every card on it.
    """
    return _list_dashboards()


@mcp.tool
def get_dashboard(dashboard_id: str) -> dict[str, Any]:
    """Read one dashboard + every card on it (Phase 2.1 snapshot model).

    Each card is a frozen snapshot of a chat turn — ``sql``, ``answer``,
    ``rows``, ``insight``, ``critic`` verdict, plus grid coordinates
    (``position_x``, ``position_y``, ``width``, ``height``) following
    react-grid-layout 12-column conventions.

    The ``source_thread_id`` + ``source_turn_index`` on each card point
    back to the conversation that produced it — useful for opening
    ``ask_data`` with that ``conversation_id`` to continue from where
    the card was extracted.

    Returns ``{"error": "..."}`` when the id doesn't exist.
    """
    try:
        return _get_dashboard(dashboard_id)
    except KeyError:
        return {"error": f"dashboard not found: {dashboard_id}"}


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("schema://overview")
def schema_overview() -> str:
    """Full database DDL — every business table, every column, every FK relationship.

    Returned as a single text blob suitable for stuffing into an LLM
    context as a one-shot read. Use this when you want to compose SQL
    by hand via ``run_select`` or audit what's available before
    ``ask_data``. The same DDL the agent's RAG fallback uses when
    vector search misses.
    """
    return get_schema_ddl()


# ---------------------------------------------------------------------------
# stdio entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Stdio transport entry point.

    Used by Claude Desktop, Cursor, Cline, and other LLM clients that
    spawn MCP servers as child processes. Configure your client to
    invoke::

        uv --directory /path/to/data-copilot/apps/api \\
           run python -m copilot.mcp_server

    See ``docs/mcp-setup.md`` for full Claude Desktop / Cursor configs.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    # ``mcp.run()`` defaults to stdio transport. For HTTP transport in
    # the FastAPI-mounted path, ``main.py`` calls ``mcp.http_app()``
    # instead — same registered tools, different wire format.
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
