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
# S6505: do not execute arbitrary install lifecycle scripts from deps.
cd "$ROOT/frontend"
pnpm install --ignore-scripts
# Run only the project's own known, required postinstall step (no third-party scripts).
node scripts/internationalized-date-patch.cjs

# Docker (optional)
if [ "$1" = "--full" ]; then
    docker compose -f "$ROOT/docker-compose.yml" up -d
fi

echo "Done!"
