FROM node:22-bookworm-slim AS codex
# Reject base images missing the October 2026 security release.
RUN node -e "const [major, minor, patch] = process.versions.node.split('.').map(Number); if (major !== 22 || minor < 23 || (minor === 23 && patch < 4)) process.exit(1)"
ARG CODEX_VERSION=0.160.0
RUN npm install -g @openai/codex@${CODEX_VERSION} && codex --version

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# npm selects the matching amd64/arm64 Codex binary during each image build.
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules/@openai /usr/local/lib/node_modules/@openai
RUN ln -s ../lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && codex --version

RUN useradd --uid 1000 --create-home shigoto && install -d -o shigoto -g shigoto /data

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
    CODEX_HOME=/data/codex \
    GOOGLE_APPLICATION_CREDENTIALS=/secrets/google.json
USER shigoto
VOLUME /data
CMD ["shigoto", "serve"]
