#!/bin/sh
set -eu
cd "$(dirname "$0")/.."

if [ ! -f .runtime/local.env ]; then
  echo 'Execute scripts/start-local.sh primeiro para gerar os segredos locais.' >&2
  exit 1
fi
set -a
. .runtime/local.env
set +a
if [ -z "${PDV_PRINT_BRIDGE_TOKEN:-}" ]; then
  echo 'PDV_PRINT_BRIDGE_TOKEN não está configurado em .runtime/local.env.' >&2
  exit 1
fi
launch_dir="$HOME/Library/LaunchAgents"
plist="$launch_dir/com.pdv2.print-bridge.plist"
mkdir -p "$launch_dir"
./.venv/bin/python - "$plist" "$PWD" <<'PY'
import os
import plistlib
import sys
from pathlib import Path

plist_path = Path(sys.argv[1])
root = Path(sys.argv[2])
document = {
    "Label": "com.pdv2.print-bridge",
    "ProgramArguments": [str(root / ".venv/bin/python"), str(root / "infra/print_bridge.py")],
    "WorkingDirectory": str(root),
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Interactive",
    "EnvironmentVariables": {
        "PDV_PRINT_BRIDGE_TOKEN": os.environ["PDV_PRINT_BRIDGE_TOKEN"],
        "PDV_PRINT_BRIDGE_HOST": "0.0.0.0",
        "PDV_PRINT_BRIDGE_PORT": "8020",
    },
    "StandardOutPath": str(root / ".runtime/print-bridge.log"),
    "StandardErrorPath": str(root / ".runtime/print-bridge.log"),
}
plist_path.write_bytes(plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=False))
plist_path.chmod(0o600)
PY

domain="gui/$(id -u)"
launchctl bootout "$domain/com.pdv2.print-bridge" 2>/dev/null || true
launchctl bootstrap "$domain" "$plist"
launchctl kickstart -k "$domain/com.pdv2.print-bridge"
echo "Ponte de impressão instalada em $plist"
