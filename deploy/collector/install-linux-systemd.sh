#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)

INSTALL_DIR=${INSTALL_DIR:-"$HOME/.local/share/ai-usage-tracker"}
CONFIG_PATH=${CONFIG_PATH:-"$INSTALL_DIR/ai_usage_tracker.toml"}
SERVICE_NAME=${SERVICE_NAME:-ai-usage-collector.service}
SERVICE_DIR=${SERVICE_DIR:-"$HOME/.config/systemd/user"}
CLIENT_NAME=${CLIENT_NAME:-"$(hostname)"}
COLLECTOR_PORT=${COLLECTOR_PORT:-4318}
COLLECTOR_HOST=${COLLECTOR_HOST:-127.0.0.1}
AGGREGATION_ENDPOINT=${AGGREGATION_ENDPOINT:-}
COLLECTOR_API_KEY=${COLLECTOR_API_KEY:-}
CF_ACCESS_CLIENT_ID=${CF_ACCESS_CLIENT_ID:-}
CF_ACCESS_CLIENT_SECRET=${CF_ACCESS_CLIENT_SECRET:-}
PYTHON_BIN=${PYTHON_BIN:-/usr/bin/python3}

if [ -z "$AGGREGATION_ENDPOINT" ] || [ -z "$COLLECTOR_API_KEY" ]; then
  echo "AGGREGATION_ENDPOINT and COLLECTOR_API_KEY are required." >&2
  echo "Create a collector token from the aggregation server /admin page first." >&2
  exit 2
fi

mkdir -p "$INSTALL_DIR" "$SERVICE_DIR"
cp "$REPO_ROOT/codex_usage_observer.py" "$INSTALL_DIR/"
cp "$REPO_ROOT/ai_usage_tracker.py" "$INSTALL_DIR/"
rm -rf "$INSTALL_DIR/codex_usage_tracker" "$INSTALL_DIR/ai_usage_tracker"
cp -R "$REPO_ROOT/ai_usage_tracker" "$INSTALL_DIR/"

{
  cat <<EOF
client_name = "$CLIENT_NAME"

[collector]
EOF
  if [ -n "$AGGREGATION_ENDPOINT" ]; then
    printf 'endpoint = "%s"\n' "$AGGREGATION_ENDPOINT"
  fi
  if [ -n "$COLLECTOR_API_KEY" ]; then
    printf 'api_key = "%s"\n' "$COLLECTOR_API_KEY"
  fi
  if [ -n "$CF_ACCESS_CLIENT_ID" ]; then
    printf 'cloudflare_access_client_id = "%s"\n' "$CF_ACCESS_CLIENT_ID"
  fi
  if [ -n "$CF_ACCESS_CLIENT_SECRET" ]; then
    printf 'cloudflare_access_client_secret = "%s"\n' "$CF_ACCESS_CLIENT_SECRET"
  fi
  cat <<EOF
batch_size = 100
timeout_seconds = 10

[storage]
raw_payload_body = false
extracted_attributes = "redacted"
model = true
session_id = true
thread_id = true
max_body_bytes = 52428800
EOF
} > "$CONFIG_PATH"

cat > "$SERVICE_DIR/$SERVICE_NAME" <<EOF
[Unit]
Description=AI usage tracker local collector
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYTHON_BIN $INSTALL_DIR/ai_usage_tracker.py --config $CONFIG_PATH client serve --host $COLLECTOR_HOST --port $COLLECTOR_PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"

echo "Installed collector service: $SERVICE_NAME"
echo "Config: $CONFIG_PATH"
systemctl --user status "$SERVICE_NAME" --no-pager
