#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=${REPO_ROOT:-"$(cd -- "$SCRIPT_DIR/../.." && pwd)"}

INSTALL_DIR=${INSTALL_DIR:-"$HOME/.local/share/ai-usage-tracker"}
CONFIG_PATH=${CONFIG_PATH:-"$INSTALL_DIR/codex_usage_observer.toml"}
SERVICE_NAME=${SERVICE_NAME:-codex-usage-collector.service}
PYTHON_BIN=${PYTHON_BIN:-python3}

if [ ! -f "$CONFIG_PATH" ]; then
  echo "Config not found: $CONFIG_PATH" >&2
  echo "Run deploy/collector/install-linux-systemd.sh first." >&2
  exit 2
fi

mkdir -p "$INSTALL_DIR"
cp "$REPO_ROOT/codex_usage_observer.py" "$INSTALL_DIR/"
rm -rf "$INSTALL_DIR/codex_usage_tracker"
cp -R "$REPO_ROOT/codex_usage_tracker" "$INSTALL_DIR/"

systemctl --user restart "$SERVICE_NAME"

echo "Updated collector code in: $INSTALL_DIR"
echo "Preserved config: $CONFIG_PATH"
systemctl --user status "$SERVICE_NAME" --no-pager | sed -n '1,14p'

if [ -f "$INSTALL_DIR/codex_usage.sqlite" ]; then
  (
    cd "$INSTALL_DIR"
    "$PYTHON_BIN" codex_usage_observer.py --config "$CONFIG_PATH" --db "$INSTALL_DIR/codex_usage.sqlite" client sync-status
  )
else
  echo "No collector database found yet at $INSTALL_DIR/codex_usage.sqlite"
fi
