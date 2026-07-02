#!/usr/bin/env bash
# 同容器双进程：ChatNest（127.0.0.1:8787，只对内）+ Ombre/Night-Fall（8000，对外）。
# 任何一个进程退出都放倒整个容器，让 Fly 重启——比留半条命好排查。
set -euo pipefail

mkdir -p "${AGENT_APP_ROOT:-/app/buckets/chatnest}"

python -m uvicorn app.main:app \
  --app-dir /app/chatnest \
  --host 127.0.0.1 \
  --port "${CHATNEST_PORT:-8787}" &
CHAT_PID=$!

python -m night_fall.launcher &
OMBRE_PID=$!

wait -n "$CHAT_PID" "$OMBRE_PID"
EXIT_CODE=$?
echo "start.sh: 有进程退出（code=$EXIT_CODE），放倒容器等 Fly 重启" >&2
kill "$CHAT_PID" "$OMBRE_PID" 2>/dev/null || true
exit "$EXIT_CODE"
