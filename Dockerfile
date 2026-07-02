# ============================================================
# Ombre Brain Docker Build
# Docker 构建文件
#
# Build: docker build -t ombre-brain .
# Run:   docker run -e OMBRE_API_KEY=your-key -p 8000:8000 ombre-brain
# ============================================================

FROM python:3.12-slim

WORKDIR /app

# Night-Fall pip-installs from GitHub, needs git
# Night-Fall 通过 git+https 安装，需要 git
# nodejs/npm 是给 Claude Code CLI 的（ChatNest 聊天后端跑在 Agent SDK 上）
RUN apt-get update && apt-get install -y --no-install-recommends git nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI：ChatNest 的运行时，认证走 CLAUDE_CODE_OAUTH_TOKEN（fly secrets）
RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

# Install dependencies first (leverage Docker cache)
# 先装依赖（利用 Docker 缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ChatNest 依赖（精简版，不含本地向量检索那套）
COPY chatnest/requirements-fly.txt ./chatnest-requirements.txt
RUN pip install --no-cache-dir -r chatnest-requirements.txt

# Night-Fall extension: dream lifecycle + breath-gated auto-surface
# Night-Fall 扩展：梦境生命周期 + breath 自动浮梦
RUN pip install --no-cache-dir git+https://github.com/mininicole/Night-Fall.git

# Copy project files / 复制项目文件
COPY *.py .
COPY dashboard.html home.html letters.html choose.html evan.html .
COPY evan-avatar.png .
COPY play/ ./play/
# 用 example 作为 baseline；运行时关键参数（model / base_url / API key）
# 全靠 fly env vars 覆盖（dehydration / embedding / Night-Fall）。
# 本地 config.yaml 在 .gitignore 里，CI 拉不到——所以直接用 example。
COPY config.example.yaml ./config.yaml

# ChatNest（/chat 聊天页）：代码进镜像，数据（对话库/上传/资料）在卷上
COPY chatnest/ ./chatnest/
COPY start.sh .
RUN chmod +x start.sh

# Persistent mount point: bucket data
# (Night-Fall stores dreams under $OMBRE_BUCKETS_DIR/night_fall automatically)
# 持久化挂载点：记忆数据（Night-Fall 自动把梦境文件放在 $OMBRE_BUCKETS_DIR/night_fall）
VOLUME ["/app/buckets"]

# Default to streamable-http for container (remote access)
# 容器场景默认用 streamable-http
ENV OMBRE_TRANSPORT=streamable-http
ENV OMBRE_BUCKETS_DIR=/app/buckets
# Night-Fall launcher imports server.py from OMBRE_HOME
# Night-Fall launcher 通过 OMBRE_HOME 找 server.py
ENV OMBRE_HOME=/app

EXPOSE 8000

# ChatNest 数据目录/会话目录固定到卷上，重新部署不丢对话
ENV AGENT_APP_ROOT=/app/buckets/chatnest
ENV MODELS_FILE=/app/chatnest/models.json
ENV CLAUDE_CONFIG_DIR=/app/buckets/.claude
ENV CLAUDE_SESSION_DIR=/app/buckets/.claude/projects
ENV OMBRE_MCP_URL=http://127.0.0.1:8000/mcp/
ENV EVAN_PROMPT_FILE=/app/buckets/chatnest/evan-prompt.md

# start.sh 同时拉起 ChatNest（8787，仅容器内）和 Ombre/Night-Fall（8000，对外）
CMD ["./start.sh"]
