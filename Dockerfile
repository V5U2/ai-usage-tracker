FROM python:3.12-slim

ARG AI_USAGE_VERSION=
ARG AI_USAGE_COMMIT=
ENV AI_USAGE_VERSION=$AI_USAGE_VERSION
ENV AI_USAGE_COMMIT=$AI_USAGE_COMMIT

RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY ai_usage_tracker.py /app/
COPY ai_usage_tracker /app/ai_usage_tracker
COPY docker/server.toml /app/default-server.toml
COPY docker/entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

EXPOSE 8318

CMD ["/app/entrypoint.sh"]
