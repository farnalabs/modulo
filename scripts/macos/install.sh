#!/usr/bin/env bash
# Install a signed, notarized Modulo arm64 .dmg for the current macOS user.
# The app bundle owns the service contract; this script only verifies/copies it.
set -euo pipefail

DMG=${1:?usage: install.sh <Modulo-version-macos-arm64.dmg>}
[[ -f "$DMG" ]] || { echo "ERROR: DMG not found: $DMG" >&2; exit 1; }
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || { echo 'ERROR: this installer requires macOS arm64 (Apple Silicon); x64/Rosetta is not supported' >&2; exit 1; }
MOUNT=$(mktemp -d)
MOUNT_ATTACHED=0
cleanup() {
  local status=$?
  set +e
  if [[ "$MOUNT_ATTACHED" -eq 1 ]]; then
    detach_status=1
    for _attempt in 1 2 3; do
      if hdiutil detach "$MOUNT" >/dev/null 2>&1; then
        detach_status=0
      fi
      if [[ "$detach_status" -eq 0 ]] && ! hdiutil info | grep -Fq "${MOUNT}"; then
        break
      fi
      sleep 1
    done
    if [[ "$detach_status" -ne 0 ]] || hdiutil info | grep -Fq "${MOUNT}"; then
      echo "ERROR: cleanup could not detach mounted volume ${MOUNT}" >&2
      [[ "$status" -eq 0 ]] && status=1
    fi
  fi
  rmdir "$MOUNT" 2>/dev/null || {
    echo "ERROR: cleanup could not remove temporary mount directory ${MOUNT}" >&2
    [[ "$status" -eq 0 ]] && status=1
  }
  return "$status"
}
trap cleanup EXIT
hdiutil verify "$DMG" >/dev/null
codesign --verify --verbose=2 "$DMG"
spctl --assess --type open --context context:primary-signature "$DMG"
hdiutil attach "$DMG" -mountpoint "$MOUNT" -nobrowse -readonly >/dev/null
MOUNT_ATTACHED=1
dmg_stem="$(basename "${DMG%.dmg}")"
dmg_stem="${dmg_stem%-macos-arm64}"
APP="${MOUNT}/${dmg_stem}.app"
[[ -d "$APP" && ! -L "$APP" ]] || { echo 'ERROR: DMG does not contain the expected non-symlink .app bundle' >&2; exit 1; }
app_count=0
while IFS= read -r candidate; do
  [[ "$candidate" == "$APP" ]] || { echo "ERROR: DMG contains an unexpected .app bundle: $candidate" >&2; exit 1; }
  app_count=$((app_count + 1))
done < <(for candidate in "$MOUNT"/*.app; do [[ -d "$candidate" && ! -L "$candidate" ]] && printf '%s\n' "$candidate"; done)
[[ "$app_count" -eq 1 ]] || { echo "ERROR: expected exactly one non-symlink .app bundle, found ${app_count}" >&2; exit 1; }
codesign --verify --deep --strict "$APP"
INSTALL_ROOT="${MODULO_INSTALL_ROOT:-$HOME/Library/Application Support/Modulo}"
mkdir -p "$INSTALL_ROOT"
TARGET="$INSTALL_ROOT/$(basename "$APP")"
rm -rf "$TARGET"
cp -R "$APP" "$TARGET"
mkdir -p "$HOME/Library/LaunchAgents"
PLIST="$HOME/Library/LaunchAgents/run.modulo.launcher.plist"
DATA_DIR="${MODULO_DATA_DIR:-$HOME/Library/Application Support/Modulo/data}"
PYTHONPATH="$TARGET/Contents/Resources/bundle/backend/src:$TARGET/Contents/Resources/bundle/backend/.venv/lib/python3.12/site-packages" \
  "$TARGET/Contents/Resources/bundle/runtime/bin/python3" - "$TARGET" "$PLIST" "$DATA_DIR" <<'PY'
import sys
from pathlib import Path
from modulo.launcher.service import render_launchd_plist
app = Path(sys.argv[1])
plist_path = Path(sys.argv[2])
data_dir = Path(sys.argv[3])
plist_path.write_text(render_launchd_plist(app / 'Contents/MacOS/modulo', data_dir=data_dir), encoding='utf-8')
PY
launchctl unload -w "$PLIST" >/dev/null 2>&1 || true
launchctl load -w "$PLIST"
printf 'Modulo installed at %s\n' "$TARGET"
