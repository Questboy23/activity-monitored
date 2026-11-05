#!/bin/bash
# Activity Monitored installer for macOS.
# One line:  curl -fsSL https://raw.githubusercontent.com/Questboy23/activity-monitored/main/install.sh | bash
# Options (after "bash -s --" when piped): --mode on|watch|off   --ai on|off
set -e
RAW="${MONITORED_RAW:-https://raw.githubusercontent.com/Questboy23/activity-monitored/main}"
H="${MONITORED_DIR:-$HOME/.monitored}"
if [ "$(uname)" != "Darwin" ]; then echo "Activity Monitored works on macOS only."; exit 1; fi
PY=""
for p in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -x "$p" ] && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  echo "Activity Monitored needs Python 3.8 or newer, which comes with Apple's Command Line Tools."
  echo "Run:  xcode-select --install   (a window opens: click Install, wait a few minutes), then run this installer again."
  exit 1
fi
mkdir -p "$H"; chmod 700 "$H"
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)"
if [ -n "$HERE" ] && [ -f "$HERE/monitored.py" ]; then cp "$HERE/monitored.py" "$H/monitored.py"
else curl -fsSL "$RAW/monitored.py" -o "$H/monitored.py"; fi
"$PY" "$H/monitored.py" install "$@"
