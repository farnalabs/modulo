#!/bin/sh
set -e

echo "=== Starting nginx ==="
nginx -g "daemon off;" &

echo "=== Bootstrap: fix DATABASE_URL and create alembic_version ==="
.venv/bin/python3 /app/deploy/fly/bootstrap_db.py

echo "=== Running DB migrations ==="
.venv/bin/alembic upgrade head || echo "WARNING: Migration failed — continuing anyway"

echo "=== Starting uvicorn ==="
exec .venv/bin/uvicorn modulo.api.main:app --host 0.0.0.0 --port 8000 --proxy-headers
