#!/bin/bash
# Double-click installer (right-click, Open, the first time). Runs install.sh from this folder.
cd "$(dirname "$0")" || exit 1
bash ./install.sh
echo
read -n 1 -s -r -p "Done. Press any key to close this window."
