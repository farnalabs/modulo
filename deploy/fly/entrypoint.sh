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
#   * Crash-loop guard: sliding window of SLIDING_CRASH_LIMIT crashes within
#     SLIDING_WINDOW_S seconds fails the container (LB moves traffic).
#     A clean/healthy exit resets the window.

# python3 / .venv/bin are on PATH via the image ENV.
export PYTHONPATH="/app/src:${PYTHONPATH:-}"

# Sliding window crash guard: track crash timestamps in a temp file so a
# worker that periodically dies does not cycle forever without triggering the
# limit. Reset the window on a successful run (clean exit or ran >= 300s).
SLIDING_WINDOW_S=300
SLIDING_CRASH_LIMIT=5

_log_crash() {
    local EXIT_CODE=$1
    local SIGNAL_NAME=""
    local CRASH_REASON="unknown"
    # exit code 128+N means killed by signal N
    if [ $EXIT_CODE -gt 128 ]; then
        local SIG=$((EXIT_CODE - 128))
        case $SIG in
            9)  SIGNAL_NAME="SIGKILL";   CRASH_REASON="OOM/killed";;
            15) SIGNAL_NAME="SIGTERM";   CRASH_REASON="shutdown";;
            2)  SIGNAL_NAME="SIGINT";    CRASH_REASON="interrupt";;
            6)  SIGNAL_NAME="SIGABRT";   CRASH_REASON="abort";;
            11) SIGNAL_NAME="SIGSEGV";   CRASH_REASON="segfault";;
            *)  SIGNAL_NAME="SIG_$SIG";  CRASH_REASON="signal";;
        esac
    elif [ $EXIT_CODE -ne 0 ]; then
        CRASH_REASON="python_exception"
    fi
    echo "WORKER_EXIT: code=$EXIT_CODE reason=$CRASH_REASON signal=$SIGNAL_NAME"
}

_check_sliding_window() {
    local CRASH_LOG="$1"
    local NOW
    NOW=$(date +%s)
    local RECENT=0
    local TS
    if [ -f "$CRASH_LOG" ]; then
        while IFS= read -r TS; do
            [ -z "$TS" ] && continue
            if [ $((NOW - TS)) -le $SLIDING_WINDOW_S ]; then
                RECENT=$((RECENT + 1))
            fi
        done < "$CRASH_LOG"
    fi
    echo "$RECENT"
}

_record_crash() {
    local CRASH_LOG="$1"
    date +%s >> "$CRASH_LOG"
    # Trim entries older than the sliding window
    local NOW
    NOW=$(date +%s)
    local TMP_FILE
    TMP_FILE="${CRASH_LOG}.tmp"
    if [ -f "$CRASH_LOG" ]; then
        while IFS= read -r TS; do
            [ -z "$TS" ] && continue
            if [ $((NOW - TS)) -le $SLIDING_WINDOW_S ]; then
                echo "$TS"
            fi
        done < "$CRASH_LOG" > "$TMP_FILE"
        mv "$TMP_FILE" "$CRASH_LOG"
    fi
}

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
# The `( ... )` subshell lets the wrapper survive to restart. DO NOT add
# `exec -a <marker>` here: rewriting argv[0] makes Python 3.12's getpath unable
# to resolve its executable (sys.executable becomes empty), so the interpreter
# falls back to the system prefix and loses the venv site-packages — the worker
# then dies at import with `No module named 'saq'` / `No module named 'redis'`.
# Launch with a plain `python3` so the venv prefix resolves. (Regression found
# on the 2026-08-04 staging deploy.)
# ---------------------------------------------------------------------------
echo "=== Starting SAQ runs worker (queue: runs) ==="
SAQ_RUNS_PID=""
RUNS_CRASH_LOG="/tmp/run-worker-crashes.log"
start_saq_runs() {
    while true; do
        RUNS_START=$(date +%s)
        ( python3 -m saq modulo.core.saq_worker.runs_settings ) &
        SAQ_RUNS_PID=$!
        echo $SAQ_RUNS_PID > /tmp/run-worker.pid
        wait $SAQ_RUNS_PID
        RUNS_END=$(date +%s)
        RUNS_EXIT=$?
        _log_crash $RUNS_EXIT
        RUNS_ELAPSED=$((RUNS_END - RUNS_START))
        if [ $RUNS_ELAPSED -le $SLIDING_WINDOW_S ] && [ $RUNS_EXIT -ne 0 ]; then
            _record_crash "$RUNS_CRASH_LOG"
            RECENT=$(_check_sliding_window "$RUNS_CRASH_LOG")
            echo "WARNING: SAQ runs worker exited after ${RUNS_ELAPSED}s (exit=$RUNS_EXIT, recent_crashes=$RECENT)"
            if [ $RECENT -gt $SLIDING_CRASH_LIMIT ]; then
                echo "FATAL: SAQ runs worker: $RECENT crashes in the last ${SLIDING_WINDOW_S}s — failing container." >&2
                exit 1
            fi
        else
            # Successful run (ran long enough or clean exit) — reset crash log
            rm -f "$RUNS_CRASH_LOG"
        fi
        sleep 1
    done
}
start_saq_runs &
SAQ_RUNS_WRAPPER_PID=$!

echo "=== Starting SAQ system worker (queue: system, web UI 8081 on 127.0.0.1, fail-closed auth) ==="
SAQ_SYSTEM_PID=""
SYSTEM_CRASH_LOG="/tmp/system-worker-crashes.log"
start_saq_system() {
    while true; do
        SYSTEM_START=$(date +%s)
        # Custom runner (modulo.core.saq_worker.run_system_web): binds the web
        # UI to 127.0.0.1 (fly ssh only) AND maps SAQ_AUTH_USERNAME/PASSWORD to
        # the AUTH_USER/AUTH_PASSWORD env vars saq.web.aiohttp.create_app reads
        # for BasicAuth. The plain `python -m saq ... --web` CLI binds 0.0.0.0
        # and applies NO auth — never use it. Runs the system worker (crons +
        # functions) and the web app in the same process.
        ( python3 -m modulo.core.saq_worker ) &
        SAQ_SYSTEM_PID=$!
        echo $SAQ_SYSTEM_PID > /tmp/system-worker.pid
        wait $SAQ_SYSTEM_PID
        SYSTEM_END=$(date +%s)
        SYSTEM_EXIT=$?
        _log_crash $SYSTEM_EXIT
        SYSTEM_ELAPSED=$((SYSTEM_END - SYSTEM_START))
        if [ $SYSTEM_ELAPSED -le $SLIDING_WINDOW_S ] && [ $SYSTEM_EXIT -ne 0 ]; then
            _record_crash "$SYSTEM_CRASH_LOG"
            RECENT=$(_check_sliding_window "$SYSTEM_CRASH_LOG")
            echo "WARNING: SAQ system worker exited after ${SYSTEM_ELAPSED}s (exit=$SYSTEM_EXIT, recent_crashes=$RECENT)"
            if [ $RECENT -gt $SLIDING_CRASH_LIMIT ]; then
                echo "FATAL: SAQ system worker: $RECENT crashes in the last ${SLIDING_WINDOW_S}s — failing container." >&2
                exit 1
            fi
        else
            # Successful run (ran long enough or clean exit) — reset crash log
            rm -f "$SYSTEM_CRASH_LOG"
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
