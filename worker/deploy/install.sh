#!/bin/bash
# Install and start the photo2print worker as a launchd agent for this user.
#   worker/deploy/install.sh [--no-load]
# Fills the plist template with this checkout's path and uv, writes it to
# ~/Library/LaunchAgents/com.photo2print.worker.plist, then loads it (unless --no-load).
# The worker reads secrets from <repo>/.env itself; nothing secret goes into the plist.
set -euo pipefail

LABEL=com.photo2print.worker
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
UV="$(command -v uv || true)"
[ -n "$UV" ] || { echo "uv not found on PATH"; exit 1; }
[ -f "$REPO/.env" ] || { echo "missing $REPO/.env (run scripts/secrets.sh)"; exit 1; }
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
PATH_VALUE="$HOME/.local/bin:$(dirname "$UV"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

mkdir -p "$REPO/logs" "$HOME/Library/LaunchAgents"
[ -f "$REPO/logs/.gitignore" ] || printf '*\n' > "$REPO/logs/.gitignore"

sed -e "s#__REPO__#$REPO#g" -e "s#__UV__#$UV#g" -e "s#__PATH__#$PATH_VALUE#g" \
  "$HERE/$LABEL.plist" > "$DEST.tmp"
plutil -lint "$DEST.tmp" >/dev/null
mv "$DEST.tmp" "$DEST"
echo "wrote $DEST"

(cd "$REPO" && "$UV" sync --quiet)

if [ "${1:-}" = "--no-load" ]; then
  echo "not loaded (--no-load). Start with: launchctl bootstrap gui/$(id -u) $DEST"
  exit 0
fi
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
launchctl enable "gui/$(id -u)/$LABEL"
echo "started $LABEL; logs in $REPO/logs/worker.jsonl"
echo "check: curl -s http://127.0.0.1:8787/healthz"
