#!/bin/bash
set -e

# PR C (plan F1/F8): this entrypoint runs ONLY SAQ workers (Celery removed).
#   * SAQ workers ALWAYS run (runs + system) — the system worker owns the
#     scheduler (fire_due_triggers) + reconcile + system crons.
#   * Scheduler: SAQ fire_due_triggers is the ONLY scheduler; Celery beat is
#     gone (removed in PR C).
#   * The system SAQ worker is FAIL-CLOSED: the container refuses to boot if
#     SAQ_AUTH_PASSWORD / SAQ_AUTH_USERNAME are unset (checked via the SETTINGS
#     VALUES, not raw env).
#   * Crash-loop guard: any SAQ worker exiting within PREFLIGHT_WINDOW seconds
#     is counted; after MAX_RESTARTS the container fails (LB moves traffic).

# python3 / .venv/bin are on PATH via the image ENV.
export PYTHONPATH="/app/src:${PYTHONPATH:-}"

PREFLIGHT_WINDOW=45
MAX_RESTARTS=5

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

# ---------------------------------------------------------------------------
# SAQ worker fail-closed auth check (plan F1) — reads the SETTINGS VALUES so a
# defaulted/empty secret fails just like an unset one.
# ---------------------------------------------------------------------------
echo "=== Checking SAQ system worker auth (fail-closed) ==="
if ! python3 -c "from modulo.settings import get_settings; s = get_settings(); raise SystemExit(0 if (s.saq_auth_password and s.saq_auth_username) else 1)"; then
  echo "FATAL: SAQ_AUTH_PASSWORD / SAQ_AUTH_USERNAME must be set (fail-closed SAQ system worker web UI auth)." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Celery — REMOVED in PR C. The SAQ system worker owns the scheduler
# (fire_due_triggers) + reconcile + system crons; there is no Celery worker or
# beat to start. Single scheduler invariant: SAQ fire_due_triggers is the ONLY
# scheduler.
# ---------------------------------------------------------------------------
echo "=== Celery removed (PR C cutover) — SAQ system worker owns the scheduler ==="

# ---------------------------------------------------------------------------
# SAQ workers — restart/backoff wrapper + max-restart guard + PID files.
# argv markers via `exec -a` (kernel argv immutable; python -m only overwrites
# sys.argv[0], not /proc/<pid>/cmdline). The `( exec -a ... )` subshell lets
# the wrapper survive to restart while argv[0] is still the marker.
# ---------------------------------------------------------------------------
echo "=== Starting SAQ runs worker (queue: runs) ==="
RUNS_RESTARTS=0
SAQ_RUNS_PID=""
start_saq_runs() {
    while true; do
        RUNS_START=$(date +%s)
        ( exec -a runs-worker python3 -m saq modulo.core.saq_worker.runs_settings ) &
        SAQ_RUNS_PID=$!
        echo $SAQ_RUNS_PID > /tmp/run-worker.pid
        wait $SAQ_RUNS_PID
        RUNS_END=$(date +%s)
        RUNS_EXIT=$?
        RUNS_RESTARTS=$(( RUNS_RESTARTS + 1 ))
        if [ $((RUNS_END - RUNS_START)) -le $PREFLIGHT_WINDOW ] && [ $RUNS_RESTARTS -gt $MAX_RESTARTS ]; then
            echo "FATAL: SAQ runs worker crashed $RUNS_RESTARTS times within the preflight window — failing container." >&2
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
        # Custom runner (modulo.core.saq_worker.run_system_web): binds the web
        # UI to 127.0.0.1 (fly ssh only) AND maps SAQ_AUTH_USERNAME/PASSWORD to
        # the AUTH_USER/AUTH_PASSWORD env vars saq.web.aiohttp.create_app reads
        # for BasicAuth. The plain `python -m saq ... --web` CLI binds 0.0.0.0
        # and applies NO auth — never use it. Runs the system worker (crons +
        # functions) and the web app in the same process.
        ( exec -a system-worker python3 -m modulo.core.saq_worker ) &
        SAQ_SYSTEM_PID=$!
        echo $SAQ_SYSTEM_PID > /tmp/system-worker.pid
        wait $SAQ_SYSTEM_PID
        SYSTEM_END=$(date +%s)
        SYSTEM_EXIT=$?
        SYSTEM_RESTARTS=$(( SYSTEM_RESTARTS + 1 ))
        if [ $((SYSTEM_END - SYSTEM_START)) -le $PREFLIGHT_WINDOW ] && [ $SYSTEM_RESTARTS -gt $MAX_RESTARTS ]; then
            echo "FATAL: SAQ system worker crashed $SYSTEM_RESTARTS times within the preflight window — failing container." >&2
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
