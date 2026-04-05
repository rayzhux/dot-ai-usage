#!/bin/sh
# install.sh — render the launchd plist from launchd/dot-ai-usage.plist.template
# using values from ./.env, install it under ~/Library/LaunchAgents/, and load
# it. Idempotent: re-run any time .env changes.

set -eu

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="sh.rayzhux.dot-ai-usage"
TEMPLATE="$PROJECT_DIR/launchd/dot-ai-usage.plist.template"
INSTALLED="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/dot-ai-usage.log"

# ---------- prerequisite checks ----------

if [ "$(uname -s)" != "Darwin" ]; then
  echo "install.sh: this tool targets macOS (launchd)." >&2
  exit 1
fi

UV_BIN="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV_BIN" ]; then
  echo "install.sh: \`uv\` not found on PATH. Install it from https://github.com/astral-sh/uv" >&2
  exit 1
fi

if ! curl -fsS --max-time 3 http://localhost:6736/v1/usage >/dev/null 2>&1; then
  echo "install.sh: WARNING — OpenUsage does not appear to be running on :6736." >&2
  echo "  The script will install fine but will push '--' placeholders until OpenUsage is up." >&2
  echo "  Start it from https://www.openusage.ai/ and enable 'Launch at Login' in its settings." >&2
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo "install.sh: missing $PROJECT_DIR/.env — copy .env.example first and fill it in." >&2
  exit 1
fi

# Reject CRLF line endings in .env — sourcing them assigns values with a
# trailing \r and quietly corrupts the API key, causing 401s that are hard
# to diagnose from the log alone.
if LC_ALL=C grep -q $'\r' "$PROJECT_DIR/.env"; then
  echo "install.sh: $PROJECT_DIR/.env has CRLF line endings." >&2
  echo "  Fix with: tr -d '\\r' < .env > .env.unix && mv .env.unix .env" >&2
  exit 1
fi

chmod 600 "$PROJECT_DIR/.env"

# shellcheck disable=SC1091
. "$PROJECT_DIR/.env"

: "${DOT_API_KEY:?install.sh: DOT_API_KEY is empty in .env}"
: "${DOT_DEVICE_ID:?install.sh: DOT_DEVICE_ID is empty in .env}"
DOT_OWNER_NAME="${DOT_OWNER_NAME:-}"
DOT_TZ="${DOT_TZ:-UTC}"
DOT_TZ_ABBR="${DOT_TZ_ABBR:-}"
DOT_INTERVAL_SECONDS="${DOT_INTERVAL_SECONDS:-600}"

# ---------- render plist ----------

mkdir -p "$(dirname "$INSTALLED")" "$(dirname "$LOG")"

# Render the template via Python's string.Template. Values are XML-escaped
# before substitution so that &, <, > in secrets or names can't produce
# invalid plist XML. string.Template also fails loudly on unknown ${…}
# tokens (catches typos in the template).
UV_BIN="$UV_BIN" PROJECT_DIR="$PROJECT_DIR" \
  DOT_DEVICE_ID="$DOT_DEVICE_ID" DOT_API_KEY="$DOT_API_KEY" \
  DOT_OWNER_NAME="$DOT_OWNER_NAME" DOT_TZ="$DOT_TZ" DOT_TZ_ABBR="$DOT_TZ_ABBR" \
  DOT_INTERVAL_SECONDS="$DOT_INTERVAL_SECONDS" HOME="$HOME" \
  python3 -c '
import os, string, sys
from xml.sax.saxutils import escape
env = {k: escape(v) for k, v in os.environ.items()}
with open(sys.argv[1]) as f:
    sys.stdout.write(string.Template(f.read()).substitute(env))
' "$TEMPLATE" > "$INSTALLED"

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$INSTALLED" >/dev/null
fi

chmod 600 "$INSTALLED"  # contains the API key

# ---------- (re)load launchd agent ----------

UID_NUM="$(id -u)"
if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
fi
launchctl bootstrap "gui/$UID_NUM" "$INSTALLED"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

echo ""
echo "✔ installed $LABEL (every ${DOT_INTERVAL_SECONDS}s)"
echo "  plist: $INSTALLED"
echo "  log:   $LOG"
echo ""
echo "Tail the log to confirm it's posting:"
echo "  tail -f \"$LOG\""
