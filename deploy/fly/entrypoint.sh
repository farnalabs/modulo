#!/bin/sh
set -e

echo "=== Starting nginx ==="
nginx -g "daemon off;" &
NGINX_PID=$!

# Fly attaches Postgres with DATABASE_URL=postgres://... but SQLAlchemy
# async drivers need the "postgresql+asyncpg://" scheme prefix.
export DATABASE_URL="${DATABASE_URL:-}"
case "$DATABASE_URL" in
  postgres://*)
    export DATABASE_URL="postgresql+asyncpg://${DATABASE_URL#postgres://}"
    echo "Fixed DATABASE_URL scheme for async driver"
    ;;
esac

echo "=== Running DB migrations ==="
uv run alembic upgrade head || echo "WARNING: Migration failed — continuing anyway"

echo "=== Starting uvicorn ==="
exec uv run uvicorn modulo.api.main:app --host 0.0.0.0 --port 8000 --proxy-headers
