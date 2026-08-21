#!/bin/sh
set -e

echo "=== Bootstrapping modulo_app role ==="
python -m modulo.db.bootstrap_role

echo "Running database migrations..."

# Phase 1: apply the initial schema (creates version table with VARCHAR(32))
alembic upgrade 0001_v2_identity_org

# Widen the version_num column to VARCHAR(64) — long revision IDs like
# 0005_library_community_visibility (34 chars) exceed the default 32.
PGPASSWORD=modulo psql -h db -U modulo -d modulo -c \
  "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64);" 2>/dev/null || true

# Phase 2: apply remaining migrations
alembic upgrade heads

echo "Migrations complete. Starting uvicorn..."

exec uvicorn modulo.api.main:app \
    --host 0.0.0.0 --port 8000 \
    --timeout-keep-alive 30 \
    --timeout-graceful-shutdown 30 \
    --limit-concurrency 100
