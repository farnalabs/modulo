#!/bin/sh
set -e

echo "=== Writing frontend runtime configuration ==="
.venv/bin/python3 - <<'PY'
import json
import os
from pathlib import Path

config = {}

monitor_config = os.environ.get("MODULO_MONITOR_CONFIG")
if monitor_config:
    try:
        config["monitor"] = json.loads(monitor_config)
    except json.JSONDecodeError as exc:
        print(f"Ignoring invalid MODULO_MONITOR_CONFIG: {exc}")

username = os.environ.get("MODULO_AUTO_LOGIN_USERNAME")
password = os.environ.get("MODULO_AUTO_LOGIN_PASSWORD")
if username and password:
    config["autoLogin"] = {"username": username, "password": password}

payload = json.dumps(config, separators=(",", ":"), ensure_ascii=True)
Path("/usr/share/nginx/html/runtime-config.js").write_text(
    "window.__MODULO_CONFIG__ = Object.assign(window.__MODULO_CONFIG__ || {}, " + payload + ");\n",
    encoding="utf-8",
)
PY

echo "=== Starting nginx ==="
nginx -g "daemon off;" &

echo "=== Bootstrap: fix DATABASE_URL and create alembic_version ==="
.venv/bin/python3 /app/deploy/fly/bootstrap_db.py

if [ -f /tmp/database_url.env ]; then
  FIXED_URL=$(cat /tmp/database_url.env)
  export DATABASE_URL="$FIXED_URL"
  echo "DATABASE_URL fixed: $(echo $DATABASE_URL | cut -c1-80)..."
fi

if [ -f /tmp/database_admin_url.env ]; then
  ADMIN_URL=$(cat /tmp/database_admin_url.env)
  export DATABASE_ADMIN_URL="$ADMIN_URL"
  echo "DATABASE_ADMIN_URL fixed: $(echo $DATABASE_ADMIN_URL | cut -c1-80)..."
fi

echo "=== Bootstrapping modulo_app role ==="
.venv/bin/python3 -m modulo.db.bootstrap_role || echo "  WARNING: role bootstrap failed (non-fatal)"

echo "=== Running DB migrations ==="
.venv/bin/alembic upgrade heads && echo "  Migrations complete" || echo "  WARNING: migrations failed (will retry in lifespan)"

echo "=== Starting Celery beat (cron/polling/report scheduler) ==="
.venv/bin/celery -A modulo.cli_celery beat --loglevel=info --pidfile=/tmp/celery-beat.pid &
CELERY_BEAT_PID=$!

echo "=== Starting Celery worker (pipeline execution, concurrency=2) ==="
.venv/bin/celery -A modulo.cli_celery worker --loglevel=info --concurrency=2 --pidfile=/tmp/celery-worker.pid &
CELERY_WORKER_PID=$!

echo "=== Admin user seeding handled by backend lifespan startup ==="

echo "=== Starting uvicorn ==="
exec .venv/bin/uvicorn modulo.api.main:app \
    --host 0.0.0.0 --port ${PORT:-8000} \
    --proxy-headers \
    --timeout-keep-alive 30 \
    --timeout-graceful-shutdown 30 \
    --limit-concurrency 100
