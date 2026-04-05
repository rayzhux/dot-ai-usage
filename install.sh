#!/bin/sh
# install.sh — parse .env, render the launchd plist from
# launchd/dot-ai-usage.plist.template, install it under ~/Library/LaunchAgents,
# and load it. Idempotent: re-run any time .env changes.

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

# Reject CRLF line endings in .env — they corrupt values with a trailing \r
# that silently breaks the API key, producing 401s that are hard to diagnose.
if LC_ALL=C grep -q $'\r' "$PROJECT_DIR/.env"; then
  echo "install.sh: $PROJECT_DIR/.env has CRLF line endings." >&2
  echo "  Fix with: tr -d '\\r' < .env > .env.unix && mv .env.unix .env" >&2
  exit 1
fi

chmod 600 "$PROJECT_DIR/.env"
mkdir -p "$(dirname "$INSTALLED")" "$(dirname "$LOG")"

# ---------- parse .env and render plist ----------
#
# Parse .env as data (no shell `. .env`, which would execute arbitrary code in
# the file), validate required keys, fill defaults for optional ones,
# XML-escape values, and render the template — all in one sandboxed
# uv-managed Python 3.11 invocation so we don't need a system `python3` on
# PATH. Values go via environment variables (not argv) to keep secrets out
# of `ps` output.

ENV_FILE="$PROJECT_DIR/.env" \
TEMPLATE_FILE="$TEMPLATE" \
UV_BIN_VAL="$UV_BIN" \
PROJECT_DIR_VAL="$PROJECT_DIR" \
HOME_VAL="$HOME" \
"$UV_BIN" run --no-project --quiet --python 3.11 python - > "$INSTALLED" <<'PY'
import os, shlex, string, sys
from xml.sax.saxutils import escape

ALLOWED = {
    "DOT_DEVICE_ID", "DOT_API_KEY", "DOT_OWNER_NAME",
    "DOT_TZ", "DOT_TZ_ABBR",
    "DOT_INTERVAL_SECONDS", "DOT_STALE_SECONDS",
}
DEFAULTS = {
    "DOT_OWNER_NAME": "",
    "DOT_TZ": "UTC",
    "DOT_TZ_ABBR": "",
    "DOT_INTERVAL_SECONDS": "600",
    "DOT_STALE_SECONDS": "900",
}
REQUIRED = ("DOT_DEVICE_ID", "DOT_API_KEY")

env_file = os.environ["ENV_FILE"]
vals = dict(DEFAULTS)

with open(env_file) as f:
    for raw in f:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        if key not in ALLOWED:
            continue
        # shlex handles "quoted strings", 'single quotes', and strips inline
        # # comments — without executing any shell metacharacters.
        try:
            parts = shlex.split(rest, comments=True, posix=True)
        except ValueError as e:
            sys.stderr.write(f"install.sh: {env_file}: bad value for {key}: {e}\n")
            sys.exit(1)
        vals[key] = parts[0] if parts else ""

missing = [k for k in REQUIRED if not vals.get(k)]
if missing:
    sys.stderr.write(
        f"install.sh: {env_file} is missing required values: {', '.join(missing)}\n"
    )
    sys.exit(1)

# Validate numeric fields so bad input fails here, not later in the script.
for key in ("DOT_INTERVAL_SECONDS", "DOT_STALE_SECONDS"):
    try:
        int(vals[key])
    except ValueError:
        sys.stderr.write(f"install.sh: {key}={vals[key]!r} is not an integer\n")
        sys.exit(1)

subs = {k: escape(v) for k, v in vals.items()}
subs["UV_BIN"] = escape(os.environ["UV_BIN_VAL"])
subs["PROJECT_DIR"] = escape(os.environ["PROJECT_DIR_VAL"])
subs["HOME"] = escape(os.environ["HOME_VAL"])

with open(os.environ["TEMPLATE_FILE"]) as f:
    sys.stdout.write(string.Template(f.read()).substitute(subs))
PY

# ---------- validate + install ----------

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$INSTALLED" >/dev/null
fi

chmod 600 "$INSTALLED"  # contains the API key

# Single source of truth for the interval: read it back from the rendered plist.
INTERVAL="$(plutil -extract StartInterval raw "$INSTALLED" 2>/dev/null || echo 600)"

# (re)load the launchd agent
UID_NUM="$(id -u)"
if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
fi
launchctl bootstrap "gui/$UID_NUM" "$INSTALLED"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

echo ""
echo "✔ installed $LABEL (every ${INTERVAL}s)"
echo "  plist: $INSTALLED"
echo "  log:   $LOG"
echo ""
echo "Tail the log to confirm it's posting:"
echo "  tail -f \"$LOG\""
