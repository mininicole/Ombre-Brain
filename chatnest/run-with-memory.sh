#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi
export AGENT_APP_ROOT="${AGENT_APP_ROOT:-$(pwd)}"
export MEMORY_SEARCH_PORT="${MEMORY_SEARCH_PORT:-3900}"
export MEMORY_SEARCH_URL="${MEMORY_SEARCH_URL:-http://127.0.0.1:${MEMORY_SEARCH_PORT}/search}"

if [ "${MEMORY_AUTO_VECTORIZE:-1}" = "1" ]; then
  "${PYTHON:-python3}" -m memory_search_service.vectorize
fi

"${PYTHON:-python3}" -m memory_search_service &
MEMORY_PID=$!
trap 'kill "$MEMORY_PID" 2>/dev/null || true' EXIT

./run.sh
