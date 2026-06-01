#!/usr/bin/env bash
# Local development helper.
#
# Usage:
#   ./scripts/dev.sh up                # start postgres (auto-builds the
#                                        schema index if it is empty)
#   ./scripts/dev.sh down              # stop postgres
#   ./scripts/dev.sh api               # run the FastAPI server with reload
#   ./scripts/dev.sh web               # run the Next.js dev server on :3000 (week 10)
#   ./scripts/dev.sh test              # unit tests (no integration)
#   ./scripts/dev.sh test-integration  # integration tests (real APIs + DB)
#   ./scripts/dev.sh index [--force]   # (re)build schema_embeddings
#   ./scripts/dev.sh ask "..."         # quick one-shot agent invocation
#   ./scripts/dev.sh ask --cid ID "...". # continue a thread (multi-turn dialogue)
#   ./scripts/dev.sh ask --cid ID --resume approve|reject
#                                      # respond to a pending HITL prompt (week 7)
#   ./scripts/dev.sh ask --show-cost "..."
#                                      # also print the cost breakdown (week 9)
#   ./scripts/dev.sh eval [--experiment NAME] [--dry-run]
#                                      # run A/B eval harness (week 6)
#   ./scripts/dev.sh mcp               # run the MCP server in stdio mode
#                                      # for Claude Desktop / Cursor / Cline
#                                      # (Phase 3.0 / ADR 0022)
#   ./scripts/dev.sh demo               # one-command end-to-end demo:
#                                      # docker compose up + open browser

set -euo pipefail

cmd="${1:-help}"

# Helper: count rows in schema_embeddings; prints "" if Postgres is down.
count_index_rows() {
  docker exec data-copilot-postgres psql -U copilot -d northwind -tA \
      -c "SELECT count(*) FROM schema_embeddings" 2>/dev/null || echo ""
}

case "$cmd" in
  up)
    docker compose up -d postgres
    echo "Postgres is starting. Tail logs with: docker compose logs -f postgres"
    # Wait for healthcheck before deciding whether to auto-index.
    for i in $(seq 1 20); do
      if [ "$(docker inspect --format='{{.State.Health.Status}}' data-copilot-postgres 2>/dev/null)" = "healthy" ]; then
        break
      fi
      sleep 1
    done
    rows=$(count_index_rows)
    if [ -z "$rows" ]; then
      echo "Postgres did not become healthy in time; skip auto-index. Run './scripts/dev.sh index' manually."
    elif [ "$rows" = "0" ]; then
      echo "schema_embeddings is empty. Building the index..."
      "$0" index || echo "WARN: indexer failed. Check SILICONFLOW_API_KEY in .env, then re-run: ./scripts/dev.sh index"
    else
      echo "schema_embeddings already has $rows rows; skipping index build (use './scripts/dev.sh index --force' to rebuild)."
    fi
    ;;
  down)
    docker compose down
    ;;
  api)
    cd apps/api
    uv run uvicorn copilot.main:app --reload --host 0.0.0.0 --port "${API_PORT:-8000}"
    ;;
  web)
    # Next.js dev server. Run ``pnpm install`` the first time; the
    # script aborts loud if it hasn't been done yet.
    cd apps/web
    if [ ! -d node_modules ]; then
      echo "node_modules missing; run: cd apps/web && pnpm install"
      exit 1
    fi
    pnpm dev
    ;;
  test)
    cd apps/api
    uv run pytest -m "not integration"
    ;;
  test-integration)
    rows=$(count_index_rows)
    if [ -z "$rows" ] || [ "$rows" = "0" ]; then
      echo "schema_embeddings is empty. Run './scripts/dev.sh up' first to build the index."
      exit 1
    fi
    cd apps/api
    uv run pytest -m integration
    ;;
  index)
    shift || true
    cd apps/api
    uv run python -m copilot.indexer "$@"
    ;;
  ask)
    shift
    # Optional --cid <id> flag to continue an existing conversation.
    # When omitted, the script generates a fresh UUID and prints it so the
    # user can pass it back on the next call (week-5 multi-turn support).
    # Optional --resume <approve|reject> answers a paused HITL prompt
    # (week 7). When --resume is set, --cid is required and any positional
    # question is ignored.
    conversation_id=""
    resume=""
    show_cost=""
    while true; do
      case "${1:-}" in
        --cid)
          conversation_id="${2:?--cid requires a thread id}"
          shift 2
          ;;
        --resume)
          resume="${2:?--resume requires approve|reject}"
          shift 2
          ;;
        --show-cost)
          show_cost="1"
          shift
          ;;
        *)
          break
          ;;
      esac
    done
    if [ -n "$resume" ] && [ -z "$conversation_id" ]; then
      echo "--resume requires --cid <thread-id>" >&2
      exit 1
    fi
    question="${*:-What can you do?}"
    cd apps/api
    # Pass the conversation id, question, resume mode, and cost flag
    # through the environment to avoid bash-vs-python quoting pitfalls
    # inside the heredoc.
    export DC_CONVERSATION_ID="$conversation_id"
    export DC_QUESTION="$question"
    export DC_RESUME="$resume"
    export DC_SHOW_COST="$show_cost"
    uv run python <<'PYEOF'
import asyncio
import json
import os
import uuid

from langgraph.types import Command

from copilot.agent import build_graph
from copilot.checkpointer import (
    conversation_lock,
    dispose_checkpointer,
    get_checkpointer,
    setup_checkpointer,
)


async def main() -> None:
    await setup_checkpointer()
    try:
        graph = build_graph(checkpointer=await get_checkpointer())
        conversation_id = os.environ.get("DC_CONVERSATION_ID") or str(uuid.uuid4())
        question = os.environ["DC_QUESTION"]
        resume = os.environ.get("DC_RESUME") or ""
        config = {"configurable": {"thread_id": conversation_id}}
        if resume:
            payload = Command(resume=resume)
        else:
            payload = {"question": question}
        async with conversation_lock(conversation_id):
            result = await graph.ainvoke(payload, config=config)

        interrupts = result.get("__interrupt__") or []
        pending = None
        if interrupts:
            value = getattr(interrupts[0], "value", None)
            pending = value if isinstance(value, dict) else None

        if pending is not None:
            print("--- PENDING CONFIRMATION ---")
            print(f"reason:     {pending.get('reason')}")
            print(f"total_cost: {pending.get('total_cost')}")
            print(f"threshold:  {pending.get('threshold')}")
            if pending.get("sql"):
                print("--- SQL ---")
                print(pending["sql"])
            print("--- THREAD ---")
            print(f"conversation_id: {conversation_id}")
            print(f"turn_index:      {result.get('turn_index')}")
            print(
                "(approve with: ./scripts/dev.sh ask --cid "
                f"{conversation_id} --resume approve)"
            )
            print(
                "(reject  with: ./scripts/dev.sh ask --cid "
                f"{conversation_id} --resume reject)"
            )
            return

        if result.get("sql"):
            print("--- SQL ---")
            print(result["sql"])
        if result.get("row_count") is not None:
            print(f"--- ROWS ({result['row_count']}) ---")
            rows = result.get("sql_result") or []
            print(json.dumps(rows[:5], default=str, ensure_ascii=False, indent=2))
            if len(rows) > 5:
                print(f"... and {len(rows) - 5} more")
        if result.get("error"):
            print("--- ERROR ---")
            print(result["error"])
        # Week 8: structured insight + chart kind / spec. Both are
        # optional — chitchat / errored turns don't populate them.
        insight = result.get("insight") or {}
        if insight:
            print("--- INSIGHT ---")
            if insight.get("bullets"):
                for b in insight["bullets"]:
                    print(f"  - {b}")
            highlights = insight.get("metric_highlights") or []
            if highlights:
                print("  metrics:")
                for h in highlights:
                    print(f"    {h.get('label')}: {h.get('value')}")
        if result.get("chart_kind"):
            print(f"--- CHART ({result['chart_kind']}) ---")
            if result.get("chart_spec"):
                spec_preview = json.dumps(
                    result["chart_spec"], default=str, ensure_ascii=False
                )
                if len(spec_preview) > 400:
                    spec_preview = spec_preview[:400] + "..."
                print(spec_preview)
        # Week 9: cumulative cost across the conversation. Only print
        # when --show-cost was passed so the default output stays clean.
        if os.environ.get("DC_SHOW_COST") and result.get("cost"):
            c = result["cost"]
            print("--- COST (cumulative) ---")
            print(
                f"  llm_calls={c.get('llm_calls', 0)} "
                f"embedding_calls={c.get('embedding_calls', 0)} "
                f"db_explain={c.get('db_explain_calls', 0)} "
                f"db_select={c.get('db_select_calls', 0)}"
            )
            print(
                f"  tokens: in={c.get('est_tokens_in', 0)} "
                f"out={c.get('est_tokens_out', 0)}"
            )
            print(f"  est_usd=${c.get('est_usd', 0.0):.6f}")
        print("--- ANSWER ---")
        print(result.get("answer", ""))
        print("--- THREAD ---")
        print(f"conversation_id: {conversation_id}")
        print(f"turn_index:      {result.get('turn_index')}")
        print(f"(continue with: ./scripts/dev.sh ask --cid {conversation_id} '...')")
    finally:
        await dispose_checkpointer()


asyncio.run(main())
PYEOF
    ;;
  eval)
    shift || true
    cd apps/api
    uv run python -m copilot.eval "$@"
    ;;
  mcp)
    # Stdio MCP server (Phase 3.0 / ADR 0022). Used by LLM clients
    # that spawn MCP servers as child processes (Claude Desktop, Cursor,
    # Cline). Reads NL questions via the MCP wire protocol on stdin and
    # writes responses on stdout — DO NOT add ``echo`` / ``print``
    # statements above this line, they'd corrupt the JSON-RPC stream.
    cd apps/api
    exec uv run python -m copilot.mcp_server
    ;;
  demo)
    # One-shot end-to-end demo:
    # 1. start the full stack (postgres + api + web)
    # 2. wait until the web container is healthy
    # 3. open the Next.js page in the default browser
    # 4. tail logs so the operator can watch the agent work
    if [ ! -f .env ]; then
      echo "No .env yet. Running make-env.sh first..."
      ./scripts/make-env.sh
    fi
    echo "Starting the full stack (postgres + api + web)..."
    # ``--profile app`` is required because the api/web services are
    # gated behind it in docker-compose.yml (so plain ``dev.sh up`` keeps
    # bringing only Postgres, like it always did). Without this flag,
    # ``docker compose up -d`` would skip api+web entirely and the wait
    # loop below would time out on a container that was never started.
    docker compose --profile app up -d --build
    echo "Waiting for the web container to come up (up to 90s)..."
    for i in $(seq 1 30); do
      if [ "$(docker inspect --format='{{.State.Health.Status}}' data-copilot-web 2>/dev/null)" = "healthy" ]; then
        break
      fi
      sleep 3
    done
    url="http://localhost:${WEB_PORT:-3000}"
    echo "Web app should be live at ${url}"
    case "$(uname -s)" in
      Darwin)  open "${url}" ;;
      Linux)   command -v xdg-open >/dev/null && xdg-open "${url}" ;;
      *)       echo "  (open ${url} in your browser)" ;;
    esac
    echo ""
    echo "Tailing combined logs. Hit Ctrl-C to stop."
    docker compose logs -f --tail=20 api web
    ;;
  *)
    echo "Usage: $0 {up|down|api|web|test|test-integration|index|ask <question>|eval|mcp|demo}"
    exit 1
    ;;
esac
