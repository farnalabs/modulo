#!/bin/sh
# Write the native macOS app bundle metadata used by the arm64 installer.
# This helper only writes metadata; signing and notarization remain workflow steps.

set -eu

if [ "$#" -ne 2 ]; then
    printf 'usage: %s <version> <output-path>\n' "$0" >&2
    exit 2
fi

VERSION=$1
OUTPUT_PATH=$2

case "$VERSION" in
    ''|*[!0-9.]*|*.*.*.*)
        printf 'invalid macOS bundle version: %s\n' "$VERSION" >&2
        exit 1
        ;;
    *) ;;
esac

case "$OUTPUT_PATH" in
    ''|*..*|/*)
        printf 'unsafe macOS metadata output path: %s\n' "$OUTPUT_PATH" >&2
        exit 1
        ;;
    *) ;;
esac

mkdir -p "$(dirname "$OUTPUT_PATH")"
cat > "$OUTPUT_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>run.modulo.app</string>
<key>CFBundleName</key><string>Modulo</string>
<key>CFBundleExecutable</key><string>modulo</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>${VERSION}</string>
<key>CFBundleVersion</key><string>${VERSION}</string>
</dict></plist>
EOF
printf 'macOS arm64 metadata written to %s for version %s\n' "$OUTPUT_PATH" "$VERSION"
