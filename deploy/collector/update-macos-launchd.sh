#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=${REPO_ROOT:-"$(cd -- "$SCRIPT_DIR/../.." && pwd)"}

INSTALL_DIR=${INSTALL_DIR:-"$HOME/Library/Application Support/ai-usage-tracker"}
if [ -z "${CONFIG_PATH:-}" ]; then
  if [ -f "$INSTALL_DIR/codex_usage_observer.toml" ] && [ ! -f "$INSTALL_DIR/ai_usage_tracker.toml" ]; then
    CONFIG_PATH="$INSTALL_DIR/codex_usage_observer.toml"
  else
    CONFIG_PATH="$INSTALL_DIR/ai_usage_tracker.toml"
  fi
fi
LABEL=${LABEL:-"com.$(id -un).ai-usage-tracker.collector"}
PYTHON_BIN=${PYTHON_BIN:-python3}

if [ ! -f "$CONFIG_PATH" ]; then
  echo "Config not found: $CONFIG_PATH" >&2
  echo "Run deploy/collector/install-macos-launchd.sh first." >&2
  exit 2
fi

mkdir -p "$INSTALL_DIR"
cp "$REPO_ROOT/codex_usage_observer.py" "$INSTALL_DIR/"
cp "$REPO_ROOT/ai_usage_tracker.py" "$INSTALL_DIR/"
rm -rf "$INSTALL_DIR/codex_usage_tracker" "$INSTALL_DIR/ai_usage_tracker"
cp -R "$REPO_ROOT/ai_usage_tracker" "$INSTALL_DIR/"

launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Updated collector code in: $INSTALL_DIR"
echo "Preserved config: $CONFIG_PATH"
launchctl print "gui/$(id -u)/$LABEL" | grep -E "state =|pid =|runs =" || true

if [ -f "$INSTALL_DIR/ai_usage.sqlite" ]; then
  DB_PATH="$INSTALL_DIR/ai_usage.sqlite"
elif [ -f "$INSTALL_DIR/codex_usage.sqlite" ]; then
  DB_PATH="$INSTALL_DIR/codex_usage.sqlite"
else
  DB_PATH=
fi

if [ -n "$DB_PATH" ]; then
  (
    cd "$INSTALL_DIR"
    "$PYTHON_BIN" ai_usage_tracker.py --config "$CONFIG_PATH" --db "$DB_PATH" client sync-status
  )
else
  echo "No collector database found yet at $INSTALL_DIR/ai_usage.sqlite"
fi
