# MCP setup — Claude Desktop, Cursor, Cline, remote clients

> Phase 3.0 / ADR 0022. Copy-paste recipes for every supported client
> + a smoke-test curl.

## Quick smoke test (no client needed)

Verify the stdio entry point works on your machine:

```bash
cd ~/Documents/data-copilot
./scripts/dev.sh mcp
# (no output — stdio mode waits for JSON-RPC on stdin)
# Ctrl-C to exit
```

If you see "ImportError" / "module not found", run `uv sync --extra dev`
in the repo root first.

Verify the HTTP transport is reachable (only after `dev.sh api`):

```bash
curl -i -X POST http://localhost:8000/mcp/ \
     -H 'content-type: application/json' \
     -H 'accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

The trailing slash on `/mcp/` matters (FastAPI mount + Starlette
sub-router). A 200 + a JSON-RPC payload listing 6 tools = working.

## Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` on
macOS:

```jsonc
{
  "mcpServers": {
    "data-copilot": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/zhangruiping/Documents/data-copilot/apps/api",
        "run",
        "python",
        "-m",
        "copilot.mcp_server"
      ],
      "env": {
        "DATABASE_URL": "postgresql://copilot:copilot_dev_pwd@localhost:5432/northwind",
        "DEEPSEEK_API_KEY": "sk-…",
        "SILICONFLOW_API_KEY": "sk-…"
      }
    }
  }
}
```

Restart Claude Desktop (full quit, not just close window). The
"Search & tools" menu in any chat will show a new `data-copilot` entry
with 6 tools.

Try: *"Use data-copilot to tell me how many customers we have in
Germany"* — Claude should call `ask_data` once and read back the
structured answer.

## Cursor

`~/.cursor/mcp.json` (or per-workspace `<repo>/.cursor/mcp.json`):

```jsonc
{
  "mcpServers": {
    "data-copilot": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/zhangruiping/Documents/data-copilot/apps/api",
        "run",
        "python",
        "-m",
        "copilot.mcp_server"
      ],
      "env": {
        "DATABASE_URL": "postgresql://copilot:copilot_dev_pwd@localhost:5432/northwind",
        "DEEPSEEK_API_KEY": "sk-…",
        "SILICONFLOW_API_KEY": "sk-…"
      }
    }
  }
}
```

Reload Cursor (`Cmd+Shift+P` → "Reload Window"). In Composer / Agent
mode, the model can call the 6 tools directly. Per-workspace config
is preferred if you're working in this repo — keeps the MCP server
auto-available when you open the project.

## Cline (VS Code extension)

Cline reads the same MCP config schema. Find the settings JSON via
`Cmd+Shift+P` → "Cline: Open MCP Settings", paste the same
`mcpServers` block above.

## Remote MCP (Databricks Genie, hosted Claude, web apps)

After `./scripts/deploy.sh api` (or any HTTP-reachable deploy of the
API container), the MCP server is at:

```
https://<your-api-host>/mcp/
```

Databricks Genie (2026 H1+) — under workspace settings → MCP servers,
add a custom MCP server with the URL above.

Hosted Claude with MCP support (CLI / API) — pass `--mcp-server-url`
or the equivalent SDK parameter.

Custom apps using `@modelcontextprotocol/sdk` (Node / Python) —
connect via the Streamable HTTP transport pointing at that URL.

### Auth note

`/mcp` inherits whatever middleware is on the FastAPI app. Today
that's `CORS` + nothing else — anyone with the URL can call the
tools. Phase 3.1 / ADR 0006 will add JWT / API-key gating;
production deploys should keep the URL private (Fly.io's
`internal_port` + Tailscale, or a Cloudflare Access policy) until
then.

## What you get

Once any client is connected, the LLM sees these tools:

| Tool | When the model will call it |
|---|---|
| `ask_data` | "What's our top customer in Germany?" — the headline tool |
| `list_tables` | "What tables do you have?" — schema discovery |
| `describe_table` | "What columns does `orders` have?" |
| `run_select` | When the user pasted SQL or asked for something the model thinks it can compose itself |
| `list_dashboards` | "Show me my saved dashboards" |
| `get_dashboard` | "Open the Q3 brief dashboard" |

Plus one resource — `schema://overview` — that smarter clients read
once at session start to load the full DDL into context. Claude
Desktop does this automatically when you add the server.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Claude Desktop shows the server as "failed" | `DATABASE_URL` is set to a host the Claude Desktop process can't reach | Use `localhost`, not container service names. If you're inside a devcontainer, switch to the HTTP transport instead. |
| `ImportError: No module named 'fastmcp'` | dev dependencies not installed | `cd ~/Documents/data-copilot && uv sync --extra dev` |
| `/mcp/` returns 404 | trailing slash missing OR the FastAPI process started before the MCP mount landed | check the URL ends with `/`, restart the API process |
| `/mcp/` returns 500 with "session_manager.run() needs to be executed" | The sub-app lifespan wasn't combined in the parent lifespan | look at `apps/api/copilot/main.py` — the `async with _mcp_app.lifespan(app):` block must wrap the yield |
| Tool calls take 20+ seconds | Self-healing retried + critic retried | This is expected on hard questions. Look at `app/api/copilot/agent/critic.py` for the retry policy. |
| `run_select` returns `{"error":"unsafe_sql: …"}` for a SELECT | sqlglot couldn't parse it (PG-specific syntax variant) | rewrite to standard SQL, or use `ask_data` instead which has self-healing |
