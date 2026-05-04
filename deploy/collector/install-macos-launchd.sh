#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)

INSTALL_DIR=${INSTALL_DIR:-"$HOME/Library/Application Support/ai-usage-tracker"}
CONFIG_PATH=${CONFIG_PATH:-"$INSTALL_DIR/codex_usage_observer.toml"}
LOG_DIR=${LOG_DIR:-"$HOME/Library/Logs/ai-usage-tracker"}
LABEL=${LABEL:-"com.$(id -un).ai-usage-tracker.collector"}
PLIST_PATH=${PLIST_PATH:-"$HOME/Library/LaunchAgents/$LABEL.plist"}
CLIENT_NAME=${CLIENT_NAME:-"$(scutil --get LocalHostName 2>/dev/null || hostname)"}
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

mkdir -p "$INSTALL_DIR" "$LOG_DIR" "$(dirname "$PLIST_PATH")"
cp "$REPO_ROOT/codex_usage_observer.py" "$INSTALL_DIR/"
rm -rf "$INSTALL_DIR/codex_usage_tracker"
cp -R "$REPO_ROOT/codex_usage_tracker" "$INSTALL_DIR/"

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

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$INSTALL_DIR/codex_usage_observer.py</string>
    <string>--config</string>
    <string>$CONFIG_PATH</string>
    <string>client</string>
    <string>serve</string>
    <string>--host</string>
    <string>$COLLECTOR_HOST</string>
    <string>--port</string>
    <string>$COLLECTOR_PORT</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$INSTALL_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/collector.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/collector.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Installed collector LaunchAgent: $LABEL"
echo "Config: $CONFIG_PATH"
launchctl print "gui/$(id -u)/$LABEL" | grep -E "state =|pid =|runs =" || true
