#!/usr/bin/env bash
# Local development helper.
#
# Usage:
#   ./scripts/dev.sh up                # start postgres (auto-builds the
#                                        schema index if it is empty)
#   ./scripts/dev.sh down              # stop postgres
#   ./scripts/dev.sh api               # run the FastAPI server with reload
#   ./scripts/dev.sh test              # unit tests (no integration)
#   ./scripts/dev.sh test-integration  # integration tests (real APIs + DB)
#   ./scripts/dev.sh index [--force]   # (re)build schema_embeddings
#   ./scripts/dev.sh ask "..."         # quick one-shot agent invocation
#   ./scripts/dev.sh ask --cid ID "...". # continue a thread (multi-turn dialogue)
#   ./scripts/dev.sh eval [--experiment NAME] [--dry-run]
#                                      # run A/B eval harness (week 6)

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
    conversation_id=""
    if [ "${1:-}" = "--cid" ]; then
      conversation_id="${2:?--cid requires a thread id}"
      shift 2
    fi
    question="${*:-What can you do?}"
    cd apps/api
    # Pass the conversation id and question through the environment to avoid
    # bash-vs-python quoting pitfalls inside the heredoc.
    export DC_CONVERSATION_ID="$conversation_id"
    export DC_QUESTION="$question"
    uv run python <<'PYEOF'
import asyncio
import json
import os
import uuid

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
        config = {"configurable": {"thread_id": conversation_id}}
        async with conversation_lock(conversation_id):
            result = await graph.ainvoke({"question": question}, config=config)
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
  *)
    echo "Usage: $0 {up|down|api|test|test-integration|index|ask <question>|eval}"
    exit 1
    ;;
esac
