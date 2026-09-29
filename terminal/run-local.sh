#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$PROJECT_DIR"
export PDV_TERMINAL_DATA="${PDV_TERMINAL_DATA:-${XDG_DATA_HOME:-$HOME/.local/share}/pdv2}"
export PDV_TERMINAL_DB="${PDV_TERMINAL_DB:-$PDV_TERMINAL_DATA/terminal.sqlite3}"
if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  exec "$PROJECT_DIR/.venv/bin/python" -m terminal
fi
exec python3 -m terminal
