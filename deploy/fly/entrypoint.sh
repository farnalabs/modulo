#!/bin/bash
set -e

# Separate web and worker process groups:
#   * FLY_PROCESS_GROUP=app (or unset): nginx + uvicorn only
#   * FLY_PROCESS_GROUP=worker: SAQ workers only
#
# Common: bootstraps the database in BOTH groups so the first machine to boot
# applies migrations regardless of group.

# python3 / .venv/bin are on PATH via the image ENV.
export PYTHONPATH="/app/src:${PYTHONPATH:-}"
FLY_PROCESS_GROUP="${FLY_PROCESS_GROUP:-app}"

PREFLIGHT_WINDOW=45
MAX_RESTARTS=5

# ============================================================================
# Common: database bootstrapping + migrations (runs in BOTH groups)
# ============================================================================
echo "=== Bootstrap: fix DATABASE_URL and create alembic_version ==="
python3 /app/deploy/fly/bootstrap_db.py

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
python3 -m modulo.db.bootstrap_role || echo "  WARNING: role bootstrap failed (non-fatal)"

echo "=== Running DB migrations ==="
alembic upgrade heads && echo "  Migrations complete" || echo "  WARNING: migrations failed (will retry in lifespan)"

# ============================================================================
# Process group dispatch
# ============================================================================
if [ "$FLY_PROCESS_GROUP" = "worker" ]; then
    # -----------------------------------------------------------------------
    # Worker process group -- SAQ workers only, no nginx, no uvicorn
    # -----------------------------------------------------------------------

    # SAQ worker fail-closed auth check (plan F1)
    echo "=== Checking SAQ system worker auth (fail-closed) ==="
    if ! python3 -c "from modulo.settings import get_settings; s = get_settings(); raise SystemExit(0 if (s.saq_auth_password and s.saq_auth_username) else 1)"; then
      echo "FATAL: SAQ_AUTH_PASSWORD / SAQ_AUTH_USERNAME must be set (fail-closed SAQ system worker web UI auth)." >&2
      exit 1
    fi

    echo "=== Celery removed (PR C cutover) -- SAQ system worker owns the scheduler ==="

    echo "=== Starting SAQ runs worker (queue: runs) ==="
    RUNS_RESTARTS=0
    SAQ_RUNS_PID=""
    start_saq_runs() {
        while true; do
            RUNS_START=$(date +%s)
            ( python3 -m saq modulo.core.saq_worker.runs_settings ) &
            SAQ_RUNS_PID=$!
            echo $SAQ_RUNS_PID > /tmp/run-worker.pid
            wait $SAQ_RUNS_PID
            RUNS_END=$(date +%s)
            RUNS_EXIT=$?
            RUNS_RESTARTS=$(( RUNS_RESTARTS + 1 ))
            if [ $((RUNS_END - RUNS_START)) -le $PREFLIGHT_WINDOW ] && [ $RUNS_RESTARTS -gt $MAX_RESTARTS ]; then
                echo "FATAL: SAQ runs worker crashed $RUNS_RESTARTS times within the preflight window -- failing container." >&2
                exit 1
            fi
            if [ $((RUNS_END - RUNS_START)) -le $PREFLIGHT_WINDOW ]; then
                echo "WARNING: SAQ runs worker exited after $((RUNS_END - RUNS_START))s (restart $RUNS_RESTARTS)"
            else
                RUNS_RESTARTS=0
            fi
            sleep 1
        done
    }
    start_saq_runs &
    SAQ_RUNS_WRAPPER_PID=$!

    echo "=== Starting SAQ system worker (queue: system, web UI 8081 on 127.0.0.1, fail-closed auth) ==="
    SYSTEM_RESTARTS=0
    SAQ_SYSTEM_PID=""
    start_saq_system() {
        while true; do
            SYSTEM_START=$(date +%s)
            ( python3 -m modulo.core.saq_worker ) &
            SAQ_SYSTEM_PID=$!
            echo $SAQ_SYSTEM_PID > /tmp/system-worker.pid
            wait $SAQ_SYSTEM_PID
            SYSTEM_END=$(date +%s)
            SYSTEM_EXIT=$?
            SYSTEM_RESTARTS=$(( SYSTEM_RESTARTS + 1 ))
            if [ $((SYSTEM_END - SYSTEM_START)) -le $PREFLIGHT_WINDOW ] && [ $SYSTEM_RESTARTS -gt $MAX_RESTARTS ]; then
                echo "FATAL: SAQ system worker crashed $SYSTEM_RESTARTS times within the preflight window -- failing container." >&2
                exit 1
            fi
            if [ $((SYSTEM_END - SYSTEM_START)) -le $PREFLIGHT_WINDOW ]; then
                echo "WARNING: SAQ system worker exited after $((SYSTEM_END - SYSTEM_START))s (restart $SYSTEM_RESTARTS)"
            else
                SYSTEM_RESTARTS=0
            fi
            sleep 1
        done
    }
    start_saq_system &
    SAQ_SYSTEM_WRAPPER_PID=$!

    trap 'kill 0; wait' SIGTERM SIGINT
    wait

else
    # -----------------------------------------------------------------------
    # Web process group -- nginx + uvicorn only, no SAQ workers
    # -----------------------------------------------------------------------

    echo "=== Writing frontend runtime configuration ==="
    python3 - <<'PY'
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

    echo "=== Admin user seeding handled by backend lifespan startup ==="

    echo "=== Starting uvicorn ==="
    uvicorn modulo.api.main:app \
        --host 0.0.0.0 --port ${PORT:-8000} \
        --proxy-headers \
        --timeout-keep-alive 30 \
        --timeout-graceful-shutdown 30 \
        --limit-concurrency 100 &
    UVICORN_PID=$!

    trap 'kill 0; wait' SIGTERM SIGINT
    wait

fi
