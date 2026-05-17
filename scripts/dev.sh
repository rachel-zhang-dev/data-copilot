#!/usr/bin/env bash
# Local development helper.
#
# Usage:
#   ./scripts/dev.sh up        # start postgres
#   ./scripts/dev.sh down      # stop postgres
#   ./scripts/dev.sh api       # run the FastAPI server with reload
#   ./scripts/dev.sh test      # run unit tests (excluding integration)
#   ./scripts/dev.sh ask "..." # quick one-shot agent invocation

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
  ask)
    shift
    question="${*:-What can you do?}"
    cd apps/api
    uv run python -c "
import asyncio
from copilot.agent import build_graph

async def main():
    graph = build_graph()
    result = await graph.ainvoke({'question': '''$question'''})
    print('---')
    print(result['answer'])

asyncio.run(main())
"
    ;;
  *)
    echo "Usage: $0 {up|down|api|test|ask <question>}"
    exit 1
    ;;
esac
