FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md config.yaml ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    SHIGOTO_CONFIG=/app/config.yaml \
    SHIGOTO_DB=/data/shigoto.db \
    GOOGLE_APPLICATION_CREDENTIALS=/secrets/google.json
VOLUME /data
CMD ["shigoto", "serve"]
