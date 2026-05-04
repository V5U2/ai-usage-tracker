#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 user@host [user@host ...]" >&2
  exit 2
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
REMOTE_STAGING_DIR=${REMOTE_STAGING_DIR:-"/tmp/ai-usage-tracker-update"}
REMOTE_INSTALL_DIR=${REMOTE_INSTALL_DIR:-'$HOME/.local/share/ai-usage-tracker'}
REMOTE_SERVICE_NAME=${REMOTE_SERVICE_NAME:-codex-usage-collector.service}
REMOTE_PYTHON_BIN=${REMOTE_PYTHON_BIN:-python3}

for host in "$@"; do
  echo "==> $host"
  ssh "$host" "rm -rf '$REMOTE_STAGING_DIR' && mkdir -p '$REMOTE_STAGING_DIR'"
  rsync -a \
    "$REPO_ROOT/codex_usage_observer.py" \
    "$REPO_ROOT/codex_usage_tracker" \
    "$REPO_ROOT/deploy/collector/update-linux-systemd.sh" \
    "$host:$REMOTE_STAGING_DIR/"
  ssh "$host" \
    "cd '$REMOTE_STAGING_DIR' && REPO_ROOT='$REMOTE_STAGING_DIR' INSTALL_DIR=\"$REMOTE_INSTALL_DIR\" SERVICE_NAME='$REMOTE_SERVICE_NAME' PYTHON_BIN='$REMOTE_PYTHON_BIN' ./update-linux-systemd.sh"
done
