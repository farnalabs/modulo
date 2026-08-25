#!/usr/bin/env bash
set -euo pipefail

# Modulo installer
# Usage: curl -fsSL https://modulo.run/install.sh | bash

REPO="farnalabs/modulo"
VERSION="${VERSION:-latest}"
INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"

echo "Installing Modulo CLI v$VERSION..."

# Detect OS and arch
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)
case "$ARCH" in
    x86_64) ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
esac

# Download from GitHub releases
URL="https://github.com/$REPO/releases/$VERSION/download/modulo-$OS-$ARCH.tar.gz"
echo "Downloading from $URL..."

if command -v curl &>/dev/null; then
    curl -fsSL --proto '=https' --tlsv1.2 "$URL" | tar xz -C "$INSTALL_DIR" modulo
elif command -v wget &>/dev/null; then
    wget -q --https-only -O- "$URL" | tar xz -C "$INSTALL_DIR" modulo
else
    echo "Error: need curl or wget"
    exit 1
fi

echo "Installed to $INSTALL_DIR/modulo"
echo "Run 'modulo --help' to get started"

# Quick start instructions
cat <<EOF

Quick start:
  1. Set up a Postgres database and Redis
  2. Run: modulo server --db-url postgresql://... --redis-url redis://...
  3. Open http://localhost:8000

Or use Docker:
  docker run -p 8000:80 ghcr.io/$REPO:latest
EOF
