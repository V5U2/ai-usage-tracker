FROM python:3.12-slim

WORKDIR /app

COPY ai_usage_tracker.py codex_usage_observer.py /app/
COPY ai_usage_tracker /app/ai_usage_tracker
COPY docker/server.toml /app/default-server.toml
COPY docker/entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

EXPOSE 8318

CMD ["/app/entrypoint.sh"]
