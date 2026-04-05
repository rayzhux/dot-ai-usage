#!/bin/sh
# uninstall.sh — stop the launchd agent and remove the installed plist.
# Leaves .env, the project checkout, and the log file untouched.

set -eu

LABEL="sh.rayzhux.dot-ai-usage"
INSTALLED="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"

if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$UID_NUM/$LABEL" || true
  echo "✔ launchd agent $LABEL stopped"
else
  echo "  (launchd agent $LABEL was not loaded)"
fi

if [ -f "$INSTALLED" ]; then
  rm "$INSTALLED"
  echo "✔ removed $INSTALLED"
else
  echo "  (no plist at $INSTALLED)"
fi

echo ""
echo "Note: .env, your clone, and ~/Library/Logs/dot-ai-usage.log are preserved."
