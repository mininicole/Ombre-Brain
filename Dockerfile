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
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (leverage Docker cache)
# 先装依赖（利用 Docker 缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Night-Fall extension: dream lifecycle + breath-gated auto-surface
# Night-Fall 扩展：梦境生命周期 + breath 自动浮梦
RUN pip install --no-cache-dir git+https://github.com/ysuu525/Night-Fall.git

# Copy project files / 复制项目文件
COPY *.py .
COPY dashboard.html .
# 优先用本地 config.yaml（自定义），否则回退到 example
COPY config.yaml ./config.yaml

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

# Launch via Night-Fall: loads Ombre, registers night_fall tool, runs transport
# Night-Fall launcher 启动：加载 Ombre + 注册 night_fall 工具 + 跑 transport
CMD ["python", "-m", "night_fall.launcher"]
