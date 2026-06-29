#!/bin/sh
# Cross-platform dev setup helper (Unix/macOS)
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "Setting up dev environment..."

# Backend
cd "$ROOT/backend"
if [ ! -d ".venv" ]; then
    uv venv
fi
uv sync

# Frontend
cd "$ROOT/frontend"
npm install

# Docker (optional)
if [ "$1" = "--full" ]; then
    docker compose -f "$ROOT/docker-compose.yml" up -d
fi

echo "Done!"
