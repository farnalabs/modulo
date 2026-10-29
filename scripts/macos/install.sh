#!/usr/bin/env bash
# Install a signed Modulo .dmg for the current macOS user.
set -euo pipefail

DMG=${1:?usage: install.sh <Modulo-version-macos-arm64.dmg>}
[[ -f "$DMG" ]] || { echo "ERROR: DMG not found: $DMG" >&2; exit 1; }
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || { echo 'ERROR: this installer requires macOS arm64' >&2; exit 1; }
MOUNT=$(mktemp -d)
cleanup() { hdiutil detach "$MOUNT" >/dev/null 2>&1 || true; rmdir "$MOUNT" 2>/dev/null || true; }
trap cleanup EXIT
hdiutil attach "$DMG" -mountpoint "$MOUNT" -nobrowse -readonly >/dev/null
APP=$(find "$MOUNT" -maxdepth 1 -name '*.app' -print -quit)
[[ -n "$APP" ]] || { echo 'ERROR: DMG contains no .app bundle' >&2; exit 1; }
codesign --verify --deep --strict "$APP"
INSTALL_ROOT="${MODULO_INSTALL_ROOT:-$HOME/Library/Application Support/Modulo}"
mkdir -p "$INSTALL_ROOT"
TARGET="$INSTALL_ROOT/$(basename "$APP")"
rm -rf "$TARGET"
cp -R "$APP" "$TARGET"
mkdir -p "$HOME/Library/LaunchAgents"
PLIST="$HOME/Library/LaunchAgents/run.modulo.launcher.plist"
python3 - "$TARGET" "$PLIST" <<'PY'
import plistlib, sys
from pathlib import Path
app = Path(sys.argv[1])
plist = {
    'Label': 'run.modulo.launcher',
    'ProgramArguments': [str(app / 'Contents/MacOS/modulo'), 'start'],
    'RunAtLoad': True,
    'KeepAlive': {'SuccessfulExit': False},
    'ProcessType': 'Interactive',
}
Path(sys.argv[2]).write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=False))
PY
launchctl unload -w "$PLIST" >/dev/null 2>&1 || true
launchctl load -w "$PLIST"
printf 'Modulo installed at %s\n' "$TARGET"
