#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)

UNRAID_HOST=${UNRAID_HOST:-}
TEMPLATE_NAME=${TEMPLATE_NAME:-my-ai-usage-tracker.xml}
DEST_DIR=${DEST_DIR:-/boot/config/plugins/dockerMan/templates-user}
SOURCE_TEMPLATE=${SOURCE_TEMPLATE:-$REPO_ROOT/deploy/aggregation-server/unraid/ai-usage-tracker.xml}

if [ ! -f "$SOURCE_TEMPLATE" ]; then
  echo "Template not found: $SOURCE_TEMPLATE" >&2
  exit 1
fi

if [ -n "$UNRAID_HOST" ]; then
  ssh "$UNRAID_HOST" "mkdir -p '$DEST_DIR'"
  scp "$SOURCE_TEMPLATE" "$UNRAID_HOST:$DEST_DIR/$TEMPLATE_NAME"
  ssh "$UNRAID_HOST" "ls -l '$DEST_DIR/$TEMPLATE_NAME'"
else
  mkdir -p "$DEST_DIR"
  cp "$SOURCE_TEMPLATE" "$DEST_DIR/$TEMPLATE_NAME"
  ls -l "$DEST_DIR/$TEMPLATE_NAME"
fi

echo "Installed Unraid template: $DEST_DIR/$TEMPLATE_NAME"
