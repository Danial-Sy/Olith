#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# Use the same distro Python as setup.sh.
if [[ -x /usr/bin/python3 ]]; then
  PY=/usr/bin/python3
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  echo "python3 not found. On Ubuntu: sudo apt install -y python3 python3-venv python3-pip"
  exit 1
fi
"$PY" update.py "$@"
