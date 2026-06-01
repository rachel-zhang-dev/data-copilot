# ADR 0022: MCP server — expose the agent as Model Context Protocol tools (Phase 3.0)

> Status: Accepted · Date: 2026-06 (Phase 3.0) · Supersedes: none

## Context

Through 2025 the **Model Context Protocol** (MCP) — the open standard
Anthropic published in late 2024 — went from "interesting RFC" to the
de-facto interop standard between LLM clients and external systems.
By 2026 H1 every major data platform exposes an MCP server:

* Databricks Genie added MCP support in early 2026, with built-in
  connectors for Google Drive, SharePoint, GitHub, Glean, Atlassian
  ([blog](https://www.databricks.com/blog/next-generation-databricks-genie)).
* dbt shipped a `dbt-mcp` server exposing Semantic Layer, Discovery,
  CLI, Fusion, and Admin tools.
* Cube added an MCP server so AI agents call `get_metric()` rather
  than write SQL.
* Microsoft Fabric / Power BI shipped a Modeling MCP Server (Nov 2025).
* Snowflake's Cortex Agents (Apr 2026) use semantic views as
  MCP-style tools internally.

ThoughtWorks' Tech Radar (early 2026) explicitly moved raw text-to-SQL
to **"Hold"** for unsupervised workflows, recommending instead:

> *"For agentic business intelligence, avoid direct database access and
> instead use a governed data abstraction semantic layer — such as Cube
> or dbt's semantic layer — or a semantically rich access layer like
> GraphQL or MCP."*

Without an MCP server, this project's only consumer is its own
Next.js UI. Adding one makes it accessible from **Claude Desktop**,
**Cursor**, **Cline**, **Databricks Genie** (via remote MCP), the
**Claude API** with MCP support, and any future LLM client that
adopts the protocol. The barrier between "I built a text-to-SQL agent"
and "I built a tool that Claude Desktop can call as a first-class
citizen" is one small server module.

## Decision

### 1. FastMCP v3 over the low-level SDK

We use [FastMCP v3.x](https://gofastmcp.com/) (the standard wrapper
around the official `mcp` Python SDK). FastMCP 1.0 was upstreamed
into the SDK in 2024; v2/v3 is the actively-maintained higher-level
project, downloaded ~1M times a day and powering 70% of MCP servers
across all languages.

Why not the low-level `mcp` SDK directly?

* The decorator-based API (`@mcp.tool`) generates JSON-schema-typed
  tool descriptors from Python type hints. No duplicate metadata.
* Built-in support for both stdio and Streamable HTTP transports;
  switching is one method call.
* Built-in in-memory `Client` for testing — we use it in
  `tests/test_mcp_server.py` to call every tool without spinning up a
  process boundary.

### 2. Dual transport: stdio + Streamable HTTP (mounted on FastAPI)

| Transport | Entry point | Used by |
|---|---|---|
| **stdio** | `python -m copilot.mcp_server` (or `./scripts/dev.sh mcp`) | Claude Desktop, Cursor, Cline — clients that spawn MCP servers as child processes |
| **Streamable HTTP** | `POST /mcp` on the FastAPI app | Remote clients — Databricks Genie, hosted Claude with MCP support, custom apps |

Both transports register the **same** set of tools (one module-level
`mcp: FastMCP` instance). Different wire format, identical behaviour.

Why both:

* stdio is the local-dev path 99% of LLM-client tutorials assume.
  Skipping it would make this project second-class for solo
  developers running Claude Desktop on their laptop.
* HTTP is the deploy path. Mounting on the existing FastAPI app
  reuses the Postgres pool + checkpointer that `/ask` already warms
  — no second process, no second connection pool.

### 3. Six tools + one resource

Tools (the LLM client can call them):

| Tool | Wraps | Why |
|---|---|---|
| `ask_data(question, conversation_id?)` | The full SQL Specialist graph | Headline tool. "Ask anything about my data." Returns compact payload (first 10 rows, no chart spec). |
| `list_tables()` | `db.list_tables()` | Schema discovery on an unfamiliar DB. |
| `describe_table(name)` | `db.get_table_ddl([name])` | Detail-on-demand for one table; same format the agent itself sees. |
| `run_select(sql, max_rows=100)` | `sql_safety.validate_and_rewrite` + `db.run_select` | Escape hatch for clients that want to compose SQL themselves. Same static safety layer the agent uses internally — no DROP TABLE backdoor. |
| `list_dashboards()` | `dashboards.list_dashboards` | Surface the saved-dashboards index. |
| `get_dashboard(id)` | `dashboards.get_dashboard` | Full snapshot of one dashboard's grid + cards. |

Resources (the LLM client reads them):

| URI | Returns | Why a resource not a tool |
|---|---|---|
| `schema://overview` | Full database DDL as a single text blob | Resources are read once and treated as ambient context; tools are called per-question. Schema is the textbook "ambient context" pattern. |

### 4. `ask_data` returns a COMPACT payload

The internal `AskResponse` has 18+ fields including chart_spec
(can be tens of KB of Vega-Lite JSON), full rows, drill_downs,
analyst output, etc. We project that to a smaller shape for MCP:

```jsonc
{
  "conversation_id": "abc-123",
  "turn_index": 1,
  "answer": "There are 91 customers.",
  "sql": "SELECT count(*) FROM customers LIMIT 100",
  "row_count": 1,
  "rows_preview": [{"count": 91}],        // first 10 rows only
  "insight_headline": "91 customers",     // no metric_highlights — they're chart-context
  "insight_bullets": ["Mostly USA"],
  "critic_verdict": "ok",                  // ⭐ from ADR 0021
  "critic_reason": "matches the question",
  "critic_concerns": [],
  "intent": "data",
  "error": null
}
```

Reasoning:
* LLM clients have limited context windows; a 50 KB chart spec
  per tool call would burn budget on data the client doesn't render.
* Critic verdict travels — an LLM client that gets `suspicious`
  back can decide whether to trust the answer or ask a follow-up.
* `conversation_id` round-trips so a client can do multi-turn
  ("…and only Germany?" with the same ID).

A future caller wanting the full shape can hit `POST /ask` directly
over HTTP — same backend, different surface.

### 5. Lazy + cached graph construction

Both transports lazy-build the SQL Specialist graph on first tool
invocation via a module-level `_graph_cache`. This means:

* **stdio mode**: graph builds on first `ask_data` call (single
  invocation cost, ~200ms).
* **HTTP-mount mode**: graph builds when the FastAPI lifespan calls
  `_ensure_graph` (or implicitly when `/mcp` first sees an
  `ask_data` tool call). The same instance serves all subsequent
  tool calls within the process.

The checkpointer (`copilot.checkpointer.setup_checkpointer`) is also
a singleton, so calling `setup_checkpointer()` from both lifespans is
idempotent. We don't pay for two pools when both `/ask` and `/mcp`
are mounted on the same FastAPI process.

### 6. Mount lifespan via `combine_lifespans` pattern

FastAPI does NOT auto-propagate sub-app lifespans into mounted
routes. Without this, every `/mcp` request 500s with
`"session_manager.run() needs to be executed"`. The fix is in `main.py`:

```python
_mcp_app = _mcp_server.http_app(path="/")   # path="/" because we mount at /mcp

@asynccontextmanager
async def lifespan(app):
    ...  # existing setup
    async with _mcp_app.lifespan(app):     # ⭐ wrap the MCP lifespan
        try:
            yield
        finally:
            await dispose_checkpointer()
            dispose_engine()

app.mount("/mcp", _mcp_app)
```

The nested `async with _mcp_app.lifespan(app)` block delegates to
FastMCP's own session-manager lifespan, then exits in LIFO order
during shutdown. This is the upstream-recommended pattern after
the [`PR #2962`](https://github.com/PrefectHQ/fastmcp/pull/2962)
documentation fix.

We use the manual `async with` rather than `combine_lifespans` for
clarity — our own lifespan has substantive setup (checkpointer,
graph build) that needs to interleave specifically AFTER the
schema warmup but BEFORE yielding. `combine_lifespans` would
collapse the ordering.

The `path="/"` on `http_app(path="/")` plus `app.mount("/mcp", …)`
is **deliberate** — `http_app()` defaults to `path="/mcp"`, which
combined with the mount produces `/mcp/mcp` (a real footgun, see
[FastMCP PR #2962](https://github.com/PrefectHQ/fastmcp/pull/2962)).

## Alternatives explicitly rejected

### One bloated `ask` tool that returns everything

Return the full `AskResponse` including chart_spec, every row,
analyst output. Rejected because:

* MCP clients have token-limited context; a 50 KB JSON blob per
  call would chew through it.
* The chart spec is meaningful only to a renderer that knows
  Vega-Lite — the LLM client can't display it.
* Callers wanting the full shape have `/ask` over HTTP already.

### Stdio-only

Skip the HTTP transport. Simpler implementation. Rejected because:

* The whole point of the deploy story (Fly.io, Phase 11) is that
  the agent is accessible **without** running anything locally.
  Stdio-only means every consumer needs the codebase checked out.
* Remote MCP clients (Databricks Genie's MCP connection, hosted
  Claude with `--mcp-server-url`) need HTTP. Skipping it now would
  mean a second migration later.

### A separate `mcp-server` process

Run the MCP server as its own Docker container alongside `api` and
`web`. Rejected because:

* Two graphs in two processes = two checkpointer pools, two LangSmith
  trace contexts, two Postgres connection pools, two of everything.
  Operational complexity without any isolation benefit (they'd
  share the same DB anyway).
* Mounting in the existing FastAPI app means `/mcp` and `/ask`
  share state — a conversation started via the chat UI can continue
  via the MCP `ask_data` tool with the same `conversation_id`.
  That cross-surface continuity is a real feature, not an accident.

### Expose the agent via Function Calling / OpenAI Tools format

Skip MCP and ship plain OpenAI Function Calling JSON spec. Rejected
because:

* MCP is the protocol the ecosystem is consolidating on. Function
  Calling is OpenAI-specific; MCP is multi-vendor (Anthropic,
  Google, etc.).
* MCP servers compose — an LLM client can connect to ours plus
  five others and use them all in one conversation. Function
  Calling specs don't compose this way.

### `run_arbitrary_sql` without the safety layer

A power-user tool that just runs whatever SQL is given. Rejected
because:

* The whole point of `sql_safety` (ADR 0002) is that we don't
  trust SQL coming from outside the safe path. An MCP client is
  outside the safe path.
* Removing safety from one tool means writing a "but this one is
  safe" exception forever. Better to never have the exception.

## Risks and known limitations

* **No auth on `/mcp` yet.** Same posture as the rest of the API —
  ADR 0006 tracks the Phase 3.1 (formerly "Phase 3") multi-tenancy
  work where `/mcp` will inherit JWT / API-key gating from the
  shared middleware. Today, mounting `/mcp` on a public Fly.io URL
  exposes the same surface anyone with that URL could already hit.
  Acceptable for the project's portfolio / single-operator posture.
* **`ask_data` is bounded by the agent's existing retry budgets**
  (ADR 0004 + 0021). A pathologically wrong question that triggers
  3 self-heal retries + 1 critic retry can take 30+ seconds.
  Clients with their own timeouts (Claude Desktop ~120s) won't
  break, but visible latency is real.
* **`run_select` returns 100 rows by default.** A power-user MCP
  client wanting a different cap has to pass `max_rows=…`. We
  deliberately don't accept `max_rows=null` to mean "all" — that's
  how OOM happens.
* **No streaming.** Each tool call is a single request/response. The
  SSE phase events you see in the chat UI are not surfaced via MCP.
  MCP's protocol supports notifications but FastMCP doesn't yet
  cleanly expose them for tool progress; we'll revisit when it
  does.
* **stdio + HTTP both register the same `mcp` singleton.** If you
  somehow run BOTH (e.g. start the API process AND spawn the stdio
  entry point), you'll have two graphs in two processes pointing at
  the same Postgres. That's fine, just not free.

## Compatibility / migration

* No migration on Postgres — the MCP server is purely additive code.
* `/ask` and `/ask/stream` are unchanged. Existing Next.js UI,
  existing eval harness, existing CLI calls all keep working.
* The new dependency is `fastmcp>=3.3.0`, ~12 transitive packages.
  Disk: +20 MB to the Docker image; first-time `uv sync` adds
  ~3 s. Acceptable.
* `docker-compose.yml` and `fly.toml` need no changes — `/mcp` is
  just another route on the API container. To verify after deploy:

  ```bash
  curl -X POST https://data-copilot-api.fly.dev/mcp \
       -H 'content-type: application/json' \
       -H 'accept: application/json,text/event-stream' \
       -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
  ```

See [`../mcp-setup.md`](../mcp-setup.md) for Claude Desktop / Cursor /
Cline config snippets.
