#!/bin/bash
# Stop the photo2print worker launchd agent and remove its plist. Logs and jobs stay.
set -euo pipefail

LABEL=com.photo2print.worker
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "stopped $LABEL" \
  || echo "$LABEL was not running"
if [ -f "$DEST" ]; then
  rm "$DEST"
  echo "removed $DEST"
fi
