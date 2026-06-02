#!/usr/bin/env bash
# Fly.io deployment helper. Reads the per-app fly.toml from
# ``apps/<api|web>/`` and runs ``fly deploy`` against the repo root
# so the Dockerfile build context can see both apps.
#
# Usage:
#
#   ./scripts/deploy.sh api          # build + deploy backend only
#   ./scripts/deploy.sh web          # build + deploy frontend only
#   ./scripts/deploy.sh all          # both, in api → web order
#   ./scripts/deploy.sh smoke        # post-deploy health probes
#
# Prereqs:
#   * ``fly auth login`` already done (one-time per machine).
#   * Secrets set via ``fly secrets set -a <app> KEY=...`` — never in
#     this script (and never committed). See ``apps/<app>/fly.toml``
#     for the list of required secret names.

set -euo pipefail

cmd="${1:-help}"

# App names are globally unique on Fly. The original
# ``data-copilot-{api,web}`` names were taken by a prior deploy on a
# different account; this project's account uses the ``-rz-`` prefix.
# If you fork, ``sed -i s/data-copilot-rz/data-copilot-<your-handle>/g``
# on this file + both ``fly.toml`` + this README before deploying.
API_APP="data-copilot-rz-api"
WEB_APP="data-copilot-rz-web"

deploy_api() {
  echo ">> deploying ${API_APP}"
  fly deploy \
    --config apps/api/fly.toml \
    --dockerfile apps/api/Dockerfile \
    --remote-only
}

deploy_web() {
  echo ">> deploying ${WEB_APP}"
  fly deploy \
    --config apps/web/fly.toml \
    --dockerfile apps/web/Dockerfile \
    --remote-only
}

smoke() {
  echo ">> smoke-checking ${API_APP} /health"
  curl -fsS "https://${API_APP}.fly.dev/health" | jq -r .
  echo ">> smoke-checking ${API_APP} /admin/stats"
  curl -fsS "https://${API_APP}.fly.dev/admin/stats" | jq -r '.embedding_cache.backend'
  echo ">> smoke-checking ${WEB_APP} /api/health"
  curl -fsS "https://${WEB_APP}.fly.dev/api/health" | jq -r .
}

case "$cmd" in
  api)   deploy_api ;;
  web)   deploy_web ;;
  all)   deploy_api && deploy_web ;;
  smoke) smoke ;;
  *)
    echo "Usage: $0 {api|web|all|smoke}" >&2
    exit 1
    ;;
esac
