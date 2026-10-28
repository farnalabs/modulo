#!/bin/sh
# Generate macOS INFO.plist for the Modulo bundle
# Usage: create-macos-info-plist.sh <version> <output-path>

set -euo pipefail

VERSION="${1:-0.0.0}"
OUTPUT_PATH="${2:-INFO.plist}"

cat > "${OUTPUT_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>run.modulo.launcher</string>
    <key>CFBundleName</key>
    <string>Modulo</string>
    <key>CFBundleVersion</key>
    <string>${VERSION}</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION}</string>
    <key>CFBundleExecutable</key>
    <string>modulo</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
</dict>
</plist>
EOF

echo "Generated ${OUTPUT_PATH} for version ${VERSION}"
