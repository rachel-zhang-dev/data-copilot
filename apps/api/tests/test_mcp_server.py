"""Smoke tests for the Phase 3.0 MCP server.

We exercise every registered tool via FastMCP's in-memory ``Client`` —
no stdio / HTTP transport, no process boundaries, but the same
JSON-schema validation + dispatch the real server uses. Each tool gets
its DB / graph dependency mocked so the suite runs without Postgres.

Three things we want to lock down:

1. **Registration** — all 6 tools + 1 resource land in the catalog with
   the names the docs / configs reference.
2. **Schema** — type hints translate to the expected JSON Schema, which
   is what LLM clients use to decide call arguments.
3. **Behaviour** — each tool returns the documented shape; ``run_select``
   refuses unsafe SQL using the agent's own ``sql_safety`` module.

Failures here are usually one of:
   * A tool's ``@mcp.tool`` decorator was lost in a refactor.
   * A type hint became incompatible with FastMCP's JSON Schema
     inference (e.g. unbounded ``Any``).
   * The lazy graph cache got a stale entry between tests.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from copilot import mcp_server as mcp_mod

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_all_expected_tools_are_registered() -> None:
    """The six tools advertised in ADR 0022 must all show up in the
    MCP server's tool catalog with the documented names."""
    tools = await mcp_mod.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "ask_data",
        "list_tables",
        "describe_table",
        "run_select",
        "list_dashboards",
        "get_dashboard",
    }


async def test_schema_overview_resource_is_registered() -> None:
    """One resource: ``schema://overview``. Used by LLM clients that
    want to read the full DDL once instead of calling describe_table
    table-by-table."""
    resources = await mcp_mod.mcp.list_resources()
    uris = {str(r.uri) for r in resources}
    assert "schema://overview" in uris


async def test_ask_data_tool_advertises_question_parameter() -> None:
    """LLM clients pick call arguments from the JSON Schema FastMCP
    generates. ``ask_data`` MUST advertise ``question`` as a required
    string and ``conversation_id`` as an optional string."""
    tools = await mcp_mod.mcp.list_tools()
    ask = next(t for t in tools if t.name == "ask_data")
    schema = ask.parameters
    props = schema["properties"]
    assert "question" in props
    assert "conversation_id" in props
    assert "question" in schema.get("required", [])
    # conversation_id has a default of None → MUST NOT be in required
    assert "conversation_id" not in schema.get("required", [])


# ---------------------------------------------------------------------------
# In-memory client invocations
# ---------------------------------------------------------------------------


@pytest.fixture()
def reset_graph_cache() -> None:
    """Ensure each ``ask_data`` test starts from a clean slate; without
    this, the first test's stub graph would be reused by later tests."""
    mcp_mod._graph_cache.clear()


def _extract(result: Any) -> Any:
    """Return the structured payload from a CallToolResult.

    FastMCP returns ``CallToolResult`` with a ``structured_content``
    field for tools that return dicts / lists, and ``content`` (a list
    of TextContent etc.) for everything else. We unwrap whichever side
    has data so the test code stays terse."""
    sc = getattr(result, "structured_content", None) or getattr(result, "data", None)
    if sc is not None:
        # Pydantic models in newer FastMCP versions; coerce to plain.
        return sc
    parts = getattr(result, "content", None) or []
    if parts and hasattr(parts[0], "text"):
        try:
            return json.loads(parts[0].text)
        except json.JSONDecodeError:
            return parts[0].text
    return result


async def test_list_tables_returns_db_helper_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_mod, "_list_tables", lambda: ["customers", "orders"])

    from fastmcp import Client

    async with Client(mcp_mod.mcp) as client:
        result = await client.call_tool("list_tables", {})

    out = _extract(result)
    # FastMCP wraps bare list returns into ``{"result": [...]}`` for
    # JSON-Schema compatibility; accept either shape so the test
    # doesn't lock the FE / docs to one or the other.
    if isinstance(out, dict) and "result" in out:
        out = out["result"]
    assert out == ["customers", "orders"]


async def test_describe_table_passes_name_through(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_ddl(names: list[str]) -> str:
        captured["names"] = names
        return "Table: customers\n  id INTEGER\n  country TEXT"

    monkeypatch.setattr(mcp_mod, "get_table_ddl", _fake_ddl)

    from fastmcp import Client

    async with Client(mcp_mod.mcp) as client:
        result = await client.call_tool("describe_table", {"name": "customers"})

    assert captured["names"] == ["customers"]
    out = _extract(result)
    if isinstance(out, dict) and "result" in out:
        out = out["result"]
    assert "Table: customers" in str(out)


async def test_run_select_rejects_unsafe_sql_via_static_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same ``validate_and_rewrite`` that gates the agent's
    ``validate_sql_node`` MUST gate this tool — without it, an LLM
    client could DROP TABLE through the MCP escape hatch and bypass
    every protection the agent surface has."""
    # Critical: don't mock ``run_select`` — we want to confirm the
    # safety layer rejects BEFORE the DB call is even attempted.

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("run_select must not be called when SQL is unsafe")

    monkeypatch.setattr(mcp_mod, "_run_select", _boom)

    from fastmcp import Client

    async with Client(mcp_mod.mcp) as client:
        result = await client.call_tool(
            "run_select", {"sql": "DROP TABLE customers"}
        )

    out = _extract(result)
    assert isinstance(out, dict)
    assert out.get("rows") is None
    assert out.get("error", "").startswith("unsafe_sql:")


async def test_run_select_executes_safe_select(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_mod, "_run_select", lambda _sql: [{"count": 91}]
    )

    from fastmcp import Client

    async with Client(mcp_mod.mcp) as client:
        result = await client.call_tool(
            "run_select", {"sql": "SELECT count(*) FROM customers"}
        )

    out = _extract(result)
    assert isinstance(out, dict)
    assert out["error"] is None
    assert out["rows"] == [{"count": 91}]
    assert out["row_count"] == 1
    # validate_and_rewrite injects a LIMIT when the user omitted one.
    assert "LIMIT" in out["sql"].upper()


async def test_list_dashboards_returns_service_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_mod,
        "_list_dashboards",
        lambda: [
            {
                "id": "d1",
                "title": "Q3 brief",
                "description": None,
                "item_count": 2,
                "created_at": "2026-06-01",
                "updated_at": "2026-06-01",
            }
        ],
    )

    from fastmcp import Client

    async with Client(mcp_mod.mcp) as client:
        result = await client.call_tool("list_dashboards", {})

    out = _extract(result)
    if isinstance(out, dict) and "result" in out:
        out = out["result"]
    assert isinstance(out, list)
    assert out[0]["title"] == "Q3 brief"
    assert out[0]["item_count"] == 2


async def test_get_dashboard_returns_envelope_with_error_on_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_id: str) -> Any:
        raise KeyError(_id)

    monkeypatch.setattr(mcp_mod, "_get_dashboard", _raise)

    from fastmcp import Client

    async with Client(mcp_mod.mcp) as client:
        result = await client.call_tool("get_dashboard", {"dashboard_id": "nope"})

    out = _extract(result)
    assert isinstance(out, dict)
    assert "error" in out


async def test_ask_data_wraps_graph_and_returns_compact_payload(
    monkeypatch: pytest.MonkeyPatch,
    reset_graph_cache: None,
) -> None:
    """The end-to-end tool: stub the graph, call ``ask_data``, assert
    the projected payload contains the headline fields LLM clients
    need (answer / sql / rows_preview / critic verdict). Full
    AskResponse shape is tested elsewhere; here we lock the projection."""

    class _FakeGraph:
        async def ainvoke(
            self, payload: Any, *, config: Any
        ) -> dict[str, Any]:
            return {
                "answer": "There are 91 customers.",
                "sql": "SELECT count(*) FROM customers LIMIT 100",
                "row_count": 1,
                "sql_result": [{"count": 91}],
                "turn_index": 1,
                "intent": "data",
                "insight": {
                    "headline": "91 customers in the database",
                    "bullets": ["Mostly in the USA"],
                    "metric_highlights": [],
                },
                "critic": {
                    "verdict": "ok",
                    "reason": "matches the question",
                    "concerns": [],
                },
                "error": None,
            }

    async def _fake_ensure_graph() -> Any:
        return _FakeGraph()

    monkeypatch.setattr(mcp_mod, "_ensure_graph", _fake_ensure_graph)

    from fastmcp import Client

    async with Client(mcp_mod.mcp) as client:
        result = await client.call_tool(
            "ask_data", {"question": "How many customers are there?"}
        )

    out = _extract(result)
    assert isinstance(out, dict)
    # conversation_id is auto-generated when omitted.
    assert isinstance(out["conversation_id"], str) and len(out["conversation_id"]) >= 8
    assert out["answer"] == "There are 91 customers."
    assert "LIMIT" in (out["sql"] or "").upper()
    assert out["row_count"] == 1
    assert out["rows_preview"] == [{"count": 91}]
    assert out["insight_headline"] == "91 customers in the database"
    assert out["insight_bullets"] == ["Mostly in the USA"]
    assert out["critic_verdict"] == "ok"
    assert out["intent"] == "data"


async def test_ask_data_respects_supplied_conversation_id(
    monkeypatch: pytest.MonkeyPatch,
    reset_graph_cache: None,
) -> None:
    captured: dict[str, Any] = {}

    class _CaptureGraph:
        async def ainvoke(self, payload: Any, *, config: Any) -> dict[str, Any]:
            captured["thread_id"] = config["configurable"]["thread_id"]
            return {"answer": "x", "turn_index": 5, "sql": None, "sql_result": []}

    monkeypatch.setattr(
        mcp_mod, "_ensure_graph", lambda: _async_return(_CaptureGraph())
    )

    from fastmcp import Client

    async with Client(mcp_mod.mcp) as client:
        result = await client.call_tool(
            "ask_data",
            {"question": "follow-up", "conversation_id": "thr-existing-42"},
        )

    out = _extract(result)
    assert captured["thread_id"] == "thr-existing-42"
    assert out["conversation_id"] == "thr-existing-42"


def _async_return(value: Any) -> Any:
    """Tiny helper: produce a coroutine that resolves to ``value`` so a
    sync ``lambda`` can stand in for an async function in monkeypatches."""

    async def _f() -> Any:
        return value

    return _f()
