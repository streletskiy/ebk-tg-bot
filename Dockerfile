FROM node:22-bookworm-slim AS codex
ARG CODEX_VERSION=0.166.0
RUN npm install --global @openai/codex@${CODEX_VERSION} && codex --version

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/streletskiy/ebk-tg-bot" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.title="ebk-tg-bot"
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && apt-get update && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home app
WORKDIR /app
COPY pyproject.toml requirements.lock LICENSE ./
RUN pip install --no-cache-dir -r requirements.lock
COPY moneybot ./moneybot
RUN pip install --no-cache-dir --no-deps . && pip check \
    && mkdir -p /data/codex /data/workspace /data/models /data/media \
    && chown -R app:app /data
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data CODEX_HOME=/data/codex HF_HOME=/data/models
USER app
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 CMD ["moneybot", "health"]
ENTRYPOINT ["moneybot"]
CMD ["run"]
