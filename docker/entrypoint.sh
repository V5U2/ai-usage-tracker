#!/usr/bin/env sh
set -eu

CONFIG_PATH=${AI_USAGE_SERVER_CONFIG:-/data/server.toml}
DEFAULT_CONFIG=/app/default-server.toml
SERVER_DB=${AI_USAGE_SERVER_DB:-}

if [ ! -f "$CONFIG_PATH" ]; then
  mkdir -p "$(dirname "$CONFIG_PATH")"
  cp "$DEFAULT_CONFIG" "$CONFIG_PATH"
fi

if [ -z "$SERVER_DB" ]; then
  SERVER_DB=/data/ai_usage_server.sqlite
fi

exec python ai_usage_tracker.py \
  --config "$CONFIG_PATH" \
  server serve \
  --host 0.0.0.0 \
  --port 8318 \
  --server-db "$SERVER_DB" \
  --allow-remote
