#!/usr/bin/env sh
set -eu

CONFIG_PATH=${AI_USAGE_SERVER_CONFIG:-/data/server.toml}
DEFAULT_CONFIG=/app/default-server.toml

if [ ! -f "$CONFIG_PATH" ]; then
  mkdir -p "$(dirname "$CONFIG_PATH")"
  cp "$DEFAULT_CONFIG" "$CONFIG_PATH"
fi

exec python codex_usage_observer.py \
  --config "$CONFIG_PATH" \
  server serve \
  --host 0.0.0.0 \
  --port 8318 \
  --server-db /data/codex_usage_server.sqlite \
  --allow-remote
