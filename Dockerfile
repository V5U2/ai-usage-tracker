FROM python:3.12-slim

WORKDIR /app

COPY codex_usage_observer.py /app/
COPY codex_usage_tracker /app/codex_usage_tracker
COPY docker/server.toml /app/server.toml

EXPOSE 8318

CMD ["python", "codex_usage_observer.py", "--config", "/app/server.toml", "server", "serve", "--host", "0.0.0.0", "--port", "8318", "--server-db", "/data/codex_usage_server.sqlite", "--allow-remote"]
