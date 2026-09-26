#!/bin/sh
# Install ChatNest runtime for Evan on Oracle (as root). Idempotent.
# Versions pinned to what the Fly ombre-brain image runs (checked 2026-09-26).
set -eu

CLI_VERSION=2.1.280

if [ ! -x /opt/evan-memory/chatnest-venv/bin/python ]; then
  python3.12 -m venv /opt/evan-memory/chatnest-venv
  /opt/evan-memory/chatnest-venv/bin/pip install -q \
    anthropic==1.8.0 claude-agent-sdk==0.2.97 fastapi==0.141.1 \
    starlette==1.7.0 pydantic==2.13.5 python-dotenv==1.2.3 \
    python-multipart==0.0.32 "uvicorn[standard]==0.53.0"
  chown -R root:evan-memory /opt/evan-memory/chatnest-venv
  chmod -R o-rwx /opt/evan-memory/chatnest-venv
fi

if [ ! -x /opt/evan-memory/claude-cli/node_modules/.bin/claude ]; then
  install -d -o root -g root -m 0755 /opt/evan-memory/claude-cli
  npm install --prefix /opt/evan-memory/claude-cli --no-fund --no-audit \
    "@anthropic-ai/claude-code@$CLI_VERSION" >/dev/null
  chown -R root:evan-memory /opt/evan-memory/claude-cli
  chmod -R o-rwx /opt/evan-memory/claude-cli
fi

# Mount points for BindPaths (same layout as the Fly image).
install -d -o root -g root -m 0755 /app /app/buckets /app/chatnest

install -o root -g root -m 0644 /opt/evan-memory/current/deploy/oracle/evan-chatnest-candidate.service /etc/systemd/system/evan-chatnest-candidate.service
systemctl daemon-reload
/opt/evan-memory/claude-cli/node_modules/.bin/claude --version
