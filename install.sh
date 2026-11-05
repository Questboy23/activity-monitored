#!/bin/bash
# Steady installer for macOS.
# One line:  curl -fsSL https://raw.githubusercontent.com/Questboy23/mac-steady/main/install.sh | bash
# Options (after "bash -s --" when piped): --mode on|watch|off   --ai on|off
set -e
RAW="${STEADY_RAW:-https://raw.githubusercontent.com/Questboy23/mac-steady/main}"
H="${STEADY_DIR:-$HOME/.steady}"
if [ "$(uname)" != "Darwin" ]; then echo "Steady works on macOS only."; exit 1; fi
PY=""
for p in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -x "$p" ] && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  echo "Steady needs Python 3.8 or newer, which comes with Apple's Command Line Tools."
  echo "Run:  xcode-select --install   (a window opens: click Install, wait a few minutes), then run this installer again."
  exit 1
fi
mkdir -p "$H"; chmod 700 "$H"
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)"
if [ -n "$HERE" ] && [ -f "$HERE/steady.py" ]; then cp "$HERE/steady.py" "$H/steady.py"
else curl -fsSL "$RAW/steady.py" -o "$H/steady.py"; fi
"$PY" "$H/steady.py" install "$@"
