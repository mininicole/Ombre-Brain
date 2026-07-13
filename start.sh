#!/usr/bin/env bash
# 同容器三进程：ChatNest（127.0.0.1:8787，只对内）+ Ombre/Night-Fall（8000，对外）+ Gale（127.0.0.1:8790，只对内）。
# 任何一个进程退出都放倒整个容器，让 Fly 重启——比留半条命好排查。
set -euo pipefail

mkdir -p "${AGENT_APP_ROOT:-/app/buckets/chatnest}"

python -m uvicorn app.main:app \
  --app-dir /app/chatnest \
  --host 127.0.0.1 \
  --port "${CHATNEST_PORT:-8787}" &
CHAT_PID=$!

python /app/ombre_nightfall_launcher.py &
OMBRE_PID=$!

echo "[start] launching Gale memory process on localhost:8790"
OMBRE_BUCKETS_DIR=/app/buckets/gale \
OMBRE_PORT=8790 \
OMBRE_HOST=127.0.0.1 \
OMBRE_AUTH_TOKEN="" \
GALE_MCP_SLUG="" \
GIST_TOKEN="" \
STATE_GIST_URL="" \
EVAN_SEND_SECRET="" \
AI_NAME=Gale \
python /app/server.py &
GALE_PID=$!

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  kill "$CHAT_PID" "$OMBRE_PID" "$GALE_PID" 2>/dev/null || true
  wait "$CHAT_PID" "$OMBRE_PID" "$GALE_PID" 2>/dev/null || true
  exit "$exit_code"
}

trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

set +e
wait -n "$CHAT_PID" "$OMBRE_PID" "$GALE_PID"
EXIT_CODE=$?
set -e

echo "start.sh: 有进程退出（code=$EXIT_CODE），放倒容器等 Fly 重启" >&2
exit "$EXIT_CODE"
