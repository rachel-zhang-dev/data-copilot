#!/usr/bin/env bash
# Local development helper.
#
# Usage:
#   ./scripts/dev.sh up                # start postgres
#   ./scripts/dev.sh down              # stop postgres
#   ./scripts/dev.sh api               # run the FastAPI server with reload
#   ./scripts/dev.sh test              # run unit tests (excluding integration)
#   ./scripts/dev.sh test-integration  # run integration tests (needs real .env + DB)
#   ./scripts/dev.sh ask "..."         # quick one-shot agent invocation

set -euo pipefail

cmd="${1:-help}"

case "$cmd" in
  up)
    docker compose up -d postgres
    echo "Postgres is starting. Tail logs with: docker compose logs -f postgres"
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
    cd apps/api
    uv run pytest -m integration
    ;;
  ask)
    shift
    question="${*:-What can you do?}"
    cd apps/api
    uv run python -c "
import asyncio
import json
from copilot.agent import build_graph

async def main():
    graph = build_graph()
    result = await graph.ainvoke({'question': '''$question'''})
    # Show what the agent actually did, not just the final answer.
    if result.get('sql'):
        print('--- SQL ---')
        print(result['sql'])
    if result.get('row_count') is not None:
        print(f'--- ROWS ({result[\"row_count\"]}) ---')
        rows = result.get('sql_result') or []
        print(json.dumps(rows[:5], default=str, ensure_ascii=False, indent=2))
        if len(rows) > 5:
            print(f'... and {len(rows) - 5} more')
    if result.get('error'):
        print('--- ERROR ---')
        print(result['error'])
    print('--- ANSWER ---')
    print(result.get('answer', ''))

asyncio.run(main())
"
    ;;
  *)
    echo "Usage: $0 {up|down|api|test|test-integration|ask <question>}"
    exit 1
    ;;
esac
