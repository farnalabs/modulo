#!/bin/sh
set -e

echo "=== Starting nginx ==="
nginx -g "daemon off;" &
NGINX_PID=$!

# Fly attaches Postgres with DATABASE_URL=postgres://...?sslmode=disable
# but SQLAlchemy async drivers need:
#   1. "postgresql+asyncpg://" scheme prefix
#   2. No ?sslmode=disable (asyncpg uses ?ssl= instead)
export DATABASE_URL="${DATABASE_URL:-}"
case "$DATABASE_URL" in
  postgres://*)
    export DATABASE_URL="postgresql+asyncpg://${DATABASE_URL#postgres://}"
    echo "Fixed DATABASE_URL scheme for async driver"
    ;;
esac
# Strip sslmode query params — asyncpg doesn't accept sslmode
export DATABASE_URL="$(echo "$DATABASE_URL" | sed 's/\?sslmode=disable//g; s/&sslmode=disable//g')"

echo "=== Running DB migrations ==="
.venv/bin/alembic upgrade head || echo "WARNING: Migration failed — continuing anyway"

echo "=== Starting uvicorn ==="
exec .venv/bin/uvicorn modulo.api.main:app --host 0.0.0.0 --port 8000 --proxy-headers
