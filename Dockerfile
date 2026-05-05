FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY ai_usage_tracker.py codex_usage_observer.py /app/
COPY ai_usage_tracker /app/ai_usage_tracker
COPY docker/server.toml /app/default-server.toml
COPY docker/entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

EXPOSE 8318

CMD ["/app/entrypoint.sh"]
