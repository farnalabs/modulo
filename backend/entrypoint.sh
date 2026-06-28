#!/bin/sh
set -e

echo "Running database migrations..."
alembic upgrade heads
echo "Migrations complete. Starting uvicorn..."

exec uvicorn modulo.api.main:app --host 0.0.0.0 --port 8000
