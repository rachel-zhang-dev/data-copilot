#!/usr/bin/env bash
#
# Interactive .env bootstrap.
#
# Copies .env.example to .env and prompts for the three keys most
# people don't remember off the top of their head. Every other
# default is left as-is — you can edit .env afterwards if needed.
#
# Idempotent: re-run any time. If .env already exists, we ask before
# overwriting; values you've already set are kept by default.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE="${REPO_ROOT}/.env.example"
TARGET="${REPO_ROOT}/.env"

if [ ! -f "${EXAMPLE}" ]; then
  echo "ERROR: ${EXAMPLE} not found. Did you clone the full repo?" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Copy / merge logic
# ---------------------------------------------------------------------------

if [ -f "${TARGET}" ]; then
  echo ""
  echo ".env already exists. What do you want to do?"
  echo "  [k]eep it (we'll only fill in missing required keys)   <- default"
  echo "  [o]verwrite it from .env.example, then prompt for keys"
  echo "  [c]ancel"
  read -rp "> " choice
  choice="${choice:-k}"
  case "${choice}" in
    o|O)
      cp "${EXAMPLE}" "${TARGET}"
      echo "  -> overwritten."
      ;;
    c|C)
      echo "  -> cancelled."
      exit 0
      ;;
    *)
      echo "  -> keeping existing .env; only filling in missing required keys."
      ;;
  esac
else
  cp "${EXAMPLE}" "${TARGET}"
  echo "  -> created .env from .env.example"
fi

# ---------------------------------------------------------------------------
# Prompt helper
# ---------------------------------------------------------------------------

# prompt_for_key NAME PROMPT_TEXT [REQUIRED]
#   Reads the current value from .env. If it's the placeholder, asks
#   the user (silently if it looks like a secret). REQUIRED keys keep
#   re-asking until non-empty; optional keys accept blank input as
#   "leave the placeholder, the app will skip the feature".
prompt_for_key() {
  local key="$1"
  local prompt="$2"
  local required="${3:-no}"

  local current
  current="$(grep -E "^${key}=" "${TARGET}" | head -1 | sed -E "s/^${key}=//")"

  if [[ "${current}" != *"your_"* && -n "${current}" ]]; then
    echo "  ${key}: already set (skipping)."
    return
  fi

  echo ""
  echo "${prompt}"
  while :; do
    read -rsp "  ${key}: " value
    echo ""
    if [ -n "${value}" ]; then
      break
    fi
    if [ "${required}" != "yes" ]; then
      value=""
      echo "  -> left as placeholder; the feature will silently no-op."
      break
    fi
    echo "  -> required, please enter a value (or Ctrl-C to abort)."
  done

  if [ -n "${value}" ]; then
    # macOS / GNU sed compatibility: write to a temp file and move.
    local tmp
    tmp="$(mktemp)"
    awk -v k="${key}" -v v="${value}" '
      $0 ~ "^"k"=" { print k "=" v; next }
      { print }
    ' "${TARGET}" > "${tmp}"
    mv "${tmp}" "${TARGET}"
    echo "  -> ${key} set."
  fi
}

# ---------------------------------------------------------------------------
# The interactive bit
# ---------------------------------------------------------------------------

cat <<'EOF'

I'll prompt you for the three keys the agent needs. The values you
type are NOT echoed; nothing leaves your machine. Press Enter to
skip a key (the app will degrade gracefully on the optional one).

EOF

prompt_for_key DEEPSEEK_API_KEY \
  "1/3  DeepSeek API key (REQUIRED). Get one at platform.deepseek.com" \
  yes

prompt_for_key SILICONFLOW_API_KEY \
  "2/3  SiliconFlow API key (REQUIRED). Get one at cloud.siliconflow.cn" \
  yes

prompt_for_key LANGSMITH_API_KEY \
  "3/3  LangSmith API key (optional). Get one at smith.langchain.com" \
  no

cat <<'EOF'

.env is ready. Next steps:

  docker compose up           # full stack (api + web + postgres)
  ./scripts/dev.sh ask "..."  # CLI smoke test (postgres only)

Edit .env directly if you want to change any other setting; this
script only handles the required keys.
EOF
