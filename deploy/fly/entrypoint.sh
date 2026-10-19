#!/bin/bash
set -e

# Separate web and worker process groups:
#   * FLY_PROCESS_GROUP=app (or unset): nginx + uvicorn only
#   * FLY_PROCESS_GROUP=worker: SAQ workers only
#
# Common: bootstraps the database in BOTH groups so the first machine to boot
# applies migrations regardless of group.
#
# PR C (plan F1/F8): SAQ workers ALWAYS run (runs + system) — the system worker
# owns the scheduler (fire_due_triggers) + reconcile + system crons.
#   * Scheduler: SAQ fire_due_triggers is the ONLY scheduler.
#   * The system SAQ worker is FAIL-CLOSED: the container refuses to boot if
#     SAQ_AUTH_PASSWORD / SAQ_AUTH_USERNAME are unset (checked via the SETTINGS
#     VALUES, not raw env).
#   * Crash-loop guard: sliding window of SLIDING_CRASH_LIMIT crashes within
#     SLIDING_WINDOW_S seconds fails the container (LB moves traffic).
#     A clean/healthy exit resets the window.

# python3 / .venv/bin are on PATH via the image ENV.
export PYTHONPATH="/app/src:${PYTHONPATH:-}"
FLY_PROCESS_GROUP="${FLY_PROCESS_GROUP:-app}"

# ---------------------------------------------------------------------------
# FAR-2004 — bounded, signal-forwarding shutdown.
#
# Fly sends the kill signal to PID 1 (this script) and then waits
# `kill_timeout` seconds before SIGKILL (fly.toml; Fly's default is 5s). The
# previous handler was `trap 'kill 0; wait'`. `kill 0` signals EVERY process
# in the process group INCLUDING this shell, so bash re-enters the trap after
# each `kill 0` — observed as an infinite TRAP loop that never reaches `wait`
# and never lets the children finish draining. The worker group then died by
# SIGKILL, before SAQ's own bounded shutdown (30s job-drain grace + up to 60s
# cancellation hard deadline) could release its in-flight runs through the
# FAR-1627 shutdown handoff. Every run executing on the worker was orphaned
# as `running` until the 135-minute age gate.
#
# The handler below instead:
#   1. forwards SIGTERM to the two SAQ worker PIDs only (SAQ stops dequeuing
#      and drains — it does NOT accept new node executions),
#   2. waits a bounded window for them to exit (SAQ's own shutdown hook, which
#      releases/requeues the runs still in its in-flight registry, runs inside
#      that exit), and
#   3. exits — so Fly's stop is clean well before kill_timeout.
# It never signals the whole process group.
#
# The path overrides exist so the release-path script can be exercised by
# tests without a container (backend/tests/architecture/
# test_deploy_worker_shutdown.py).
RUN_WORKER_PID_FILE="${RUN_WORKER_PID_FILE:-/tmp/run-worker.pid}"
SYSTEM_WORKER_PID_FILE="${SYSTEM_WORKER_PID_FILE:-/tmp/system-worker.pid}"
WORKER_DRAIN_FLAG="${WORKER_DRAIN_FLAG:-/tmp/worker-draining}"
# Must be >= SAQ's shutdown ceiling (30s grace + 60s cancellation) + handback
# time, and comfortably below fly.toml's top-level kill_timeout. Enforced by
# the architecture test above.
WORKER_DRAIN_DEADLINE_S="${WORKER_DRAIN_DEADLINE_S:-140}"
APP_DRAIN_DEADLINE_S="${APP_DRAIN_DEADLINE_S:-60}"

# True when the PID names a live process. Deliberately a helper so callers use
# `if _pid_alive ...` and `set -e` is never tripped by the kill probe itself.
_pid_alive() {
    local pid="$1"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

# Sliding window crash guard: track crash timestamps in a temp file so a
# worker that periodically dies does not cycle forever without triggering the
# limit. Reset the window on a successful run (clean exit or ran >= 300s).
#
# The guard constants (window/limit/restart-sleep) are SINGLE-SOURCED from
# the shared supervision policy (modulo.launcher.policy — shipped in the
# wheel; the all-in-one image installs the project, so `python3 -m
# modulo.launcher.policy` works with PYTHONPATH=/app/src). Behaviour is
# identical to the former inline values (300s window, 5 crashes, 1s sleep);
# a broken generator is FATAL (silent default-drift is forbidden).
POLICY_ENV_FILE="${POLICY_ENV_FILE:-/tmp/modulo-supervision-policy.env}"
if ! python3 -m modulo.launcher.policy > "$POLICY_ENV_FILE" 2>/dev/null || [[ ! -s "$POLICY_ENV_FILE" ]]; then
    echo "FATAL: could not generate supervision policy constants (python3 -m modulo.launcher.policy)." >&2
    exit 1
fi
# shellcheck source=/dev/null
source "$POLICY_ENV_FILE"

_log_crash() {
    local exit_code=$1
    local signal_name=""
    local crash_reason="unknown"
    # exit code 128+N means killed by signal N
    if [[ $exit_code -gt 128 ]]; then
        local sig=$((exit_code - 128))
        case $sig in
            9)  signal_name="SIGKILL";   crash_reason="OOM/killed";;
            15) signal_name="SIGTERM";   crash_reason="shutdown";;
            2)  signal_name="SIGINT";    crash_reason="interrupt";;
            6)  signal_name="SIGABRT";   crash_reason="abort";;
            11) signal_name="SIGSEGV";   crash_reason="segfault";;
            *)  signal_name="SIG_$sig";  crash_reason="signal";;
        esac
    elif [[ $exit_code -ne 0 ]]; then
        crash_reason="python_exception"
    fi
    echo "WORKER_EXIT: code=$exit_code reason=$crash_reason signal=$signal_name"
}

_check_sliding_window() {
    local crash_log="$1"
    local now
    now=$(date +%s)
    local recent=0
    local ts
    if [[ -f "$crash_log" ]]; then
        while IFS= read -r ts; do
            [[ -z "$ts" ]] && continue
            if [[ $((now - ts)) -le $SLIDING_WINDOW_S ]]; then
                recent=$((recent + 1))
            fi
        done < "$crash_log"
    fi
    echo "$recent"
}

_record_crash() {
    local crash_log="$1"
    date +%s >> "$crash_log"
    # Trim entries older than the sliding window
    local now
    now=$(date +%s)
    local tmp_file
    tmp_file="${crash_log}.tmp"
    if [[ -f "$crash_log" ]]; then
        while IFS= read -r ts; do
            [[ -z "$ts" ]] && continue
            if [[ $((now - ts)) -le $SLIDING_WINDOW_S ]]; then
                echo "$ts"
            fi
        done < "$crash_log" > "$tmp_file"
        mv "$tmp_file" "$crash_log"
    fi
}

# ============================================================================
# Common: database bootstrapping + migrations (runs in BOTH groups)
# ============================================================================
echo "=== Bootstrap: fix DATABASE_URL and create alembic_version ==="
python3 /app/deploy/fly/bootstrap_db.py

if [[ -f /tmp/database_url.env ]]; then
  FIXED_URL=$(cat /tmp/database_url.env)
  export DATABASE_URL="$FIXED_URL"
  echo "DATABASE_URL fixed: $(echo $DATABASE_URL | cut -c1-80)..."
fi

if [[ -f /tmp/database_admin_url.env ]]; then
  ADMIN_URL=$(cat /tmp/database_admin_url.env)
  export DATABASE_ADMIN_URL="$ADMIN_URL"
  echo "DATABASE_ADMIN_URL fixed: $(echo $DATABASE_ADMIN_URL | cut -c1-80)..."
fi

if [[ -f /tmp/system_database_url.env ]]; then
  SYSTEM_URL=$(cat /tmp/system_database_url.env)
  export MODULO_SYSTEM_DATABASE_URL="$SYSTEM_URL"
  echo "MODULO_SYSTEM_DATABASE_URL fixed: $(echo $SYSTEM_URL | cut -c1-80)..."
fi

echo "=== Bootstrapping modulo_app role ==="
python3 -m modulo.db.bootstrap_role || echo "  WARNING: role bootstrap failed (non-fatal)"

echo "=== Running DB migrations ==="
# The Fly [release] command (release.sh) now owns migrations (ONCE per deploy BEFORE machines
# roll out). This boot path is a FAST-PATH SKIP + FALLBACK: when the DB is already at head,
# skip the advisory-lock migration loop entirely (no lock, no alembic run) — machines previously
# queued on the lock for up to 240s before FATALing even when the schema was current.
# The check is fail-safe: any error falls through to the 10-attempt loop below. The worker group
# has no app lifespan to retry later, so it FAILS CLOSED rather than start SAQ workers on a half-migrated schema.
MIGRATIONS_OK=0
if python3 - <<'PY'
import os

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from modulo.db.migrations.env import _to_sync_url

cfg = Config("/app/alembic.ini")
cfg.set_main_option("script_location", "/app/src/modulo/db/migrations")
try:
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if not head:
        raise SystemExit(1)
    url = _to_sync_url(os.environ.get("DATABASE_ADMIN_URL") or os.environ.get("DATABASE_URL") or "")
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT version_num FROM alembic_version"))
            versions = {row[0] for row in rows.fetchall()}
    finally:
        engine.dispose()
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if versions == {head} else 1)
PY
then
    echo "  Migrations already at head (release command handled) -- skipping migration loop"
    MIGRATIONS_OK=1
else
    echo "  Migrations NOT at head (or check failed) -- running migration loop"
    for attempt in $(seq 1 10); do
        if alembic upgrade heads; then
            echo "  Migrations complete (attempt $attempt)"
            MIGRATIONS_OK=1
            break
        fi
        echo "  WARNING: migrations failed (attempt $attempt/10) -- retrying in 5s"
        sleep 5
    done
fi
if [[ "$MIGRATIONS_OK" -ne 1 ]]; then
    if [[ "$FLY_PROCESS_GROUP" = "worker" ]]; then
        echo "FATAL: DB migrations failed after 10 attempts -- not starting SAQ workers." >&2
        exit 1
    fi
    echo "  WARNING: migrations failed (will retry in lifespan)"
fi

# ============================================================================
# Process group dispatch
# ============================================================================
if [[ "$FLY_PROCESS_GROUP" = "worker" ]]; then
    # -----------------------------------------------------------------------
    # Worker process group -- SAQ workers only, no nginx, no uvicorn
    # -----------------------------------------------------------------------

    # SAQ worker fail-closed auth check (plan F1) — reads the SETTINGS VALUES so
    # a defaulted/empty secret fails just like an unset one.
    echo "=== Checking SAQ system worker auth (fail-closed) ==="
    if ! python3 -c "from modulo.settings import get_settings; s = get_settings(); raise SystemExit(0 if (s.saq_auth_password and s.saq_auth_username) else 1)"; then
      echo "FATAL: SAQ_AUTH_PASSWORD / SAQ_AUTH_USERNAME must be set (fail-closed SAQ system worker web UI auth)." >&2
      exit 1
    fi

    # -----------------------------------------------------------------------
    # The SAQ system worker owns the scheduler (fire_due_triggers) + reconcile +
    # system crons. Single scheduler invariant: SAQ fire_due_triggers is the
    # ONLY scheduler.
    # -----------------------------------------------------------------------
    echo "=== SAQ system worker owns the scheduler ==="

    # -----------------------------------------------------------------------
    # SAQ workers — restart/backoff wrapper + sliding-window crash guard + PID
    # files. The `( ... )` subshell lets the wrapper survive to restart. DO NOT
    # add `exec -a <marker>` here: rewriting argv[0] makes Python 3.12's getpath
    # unable to resolve its executable (sys.executable becomes empty), so the
    # interpreter falls back to the system prefix and loses the venv
    # site-packages — the worker then dies at import with `No module named
    # 'saq'` / `No module named 'redis'`. Launch with a plain `python3` so the
    # venv prefix resolves. (Regression found on the 2026-08-04 staging deploy.)
    # -----------------------------------------------------------------------
    echo "=== Starting SAQ runs worker (queue: runs) ==="
    SAQ_RUNS_PID=""
    RUNS_CRASH_LOG="/tmp/run-worker-crashes.log"
    start_saq_runs() {
        while true; do
            RUNS_START=$(date +%s)
            ( python3 -m saq modulo.core.saq_worker.runs_settings ) &
            SAQ_RUNS_PID=$!
            echo $SAQ_RUNS_PID > "$RUN_WORKER_PID_FILE"
            # `wait || EXIT=$?` both survives `set -e` (a nonzero wait would
            # otherwise abort this wrapper on the FIRST crash, bypassing the
            # sliding-window tolerance) AND captures the worker's real exit code
            # — plain `wait` then `RUNS_END=$(date +%s)` would leave `$?` as
            # date's status (always 0).
            RUNS_EXIT=0
            wait $SAQ_RUNS_PID || RUNS_EXIT=$?
            RUNS_END=$(date +%s)
            _log_crash $RUNS_EXIT
            # FAR-2004: a drain was requested — this exit is the worker's
            # graceful shutdown, not a crash. Stop restarting so the entrypoint
            # can observe the worker gone and complete its bounded drain.
            if [[ -f "$WORKER_DRAIN_FLAG" ]]; then
                echo "SAQ runs worker drained — not restarting (worker shutdown requested)"
                return 0
            fi
            RUNS_ELAPSED=$((RUNS_END - RUNS_START))
            if [[ $RUNS_ELAPSED -le $SLIDING_WINDOW_S ]] && [[ $RUNS_EXIT -ne 0 ]]; then
                _record_crash "$RUNS_CRASH_LOG"
                recent=$(_check_sliding_window "$RUNS_CRASH_LOG")
                echo "WARNING: SAQ runs worker exited after ${RUNS_ELAPSED}s (exit=$RUNS_EXIT, recent_crashes=$recent)"
                if [[ $recent -gt $SLIDING_CRASH_LIMIT ]]; then
                    echo "FATAL: SAQ runs worker: $recent crashes in the last ${SLIDING_WINDOW_S}s — failing container." >&2
                    exit 1
                fi
            else
                # Successful run (ran long enough or clean exit) — reset crash log
                rm -f "$RUNS_CRASH_LOG"
            fi
            sleep "${SLIDING_RESTART_SLEEP_S}"
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
            # Custom runner (modulo.core.saq_worker.run_system_web): binds the
            # web UI to 127.0.0.1 (fly ssh only) AND maps
            # SAQ_AUTH_USERNAME/PASSWORD to the AUTH_USER/AUTH_PASSWORD env vars
            # saq.web.aiohttp.create_app reads for BasicAuth. The plain
            # `python -m saq ... --web` CLI binds 0.0.0.0 and applies NO auth —
            # never use it. Runs the system worker (crons + functions) and the
            # web app in the same process.
            ( python3 -m modulo.core.saq_worker ) &
            SAQ_SYSTEM_PID=$!
            echo $SAQ_SYSTEM_PID > "$SYSTEM_WORKER_PID_FILE"
            # `wait || EXIT=$?` both survives `set -e` (a nonzero wait would
            # otherwise abort this wrapper on the FIRST crash, bypassing the
            # sliding-window tolerance) AND captures the worker's real exit code
            # — plain `wait` then `SYSTEM_END=$(date +%s)` would leave `$?` as
            # date's status (always 0).
            SYSTEM_EXIT=0
            wait $SAQ_SYSTEM_PID || SYSTEM_EXIT=$?
            SYSTEM_END=$(date +%s)
            _log_crash $SYSTEM_EXIT
            # FAR-2004: drain requested — stop restarting (see the runs worker).
            if [[ -f "$WORKER_DRAIN_FLAG" ]]; then
                echo "SAQ system worker drained — not restarting (worker shutdown requested)"
                return 0
            fi
            SYSTEM_ELAPSED=$((SYSTEM_END - SYSTEM_START))
            if [[ $SYSTEM_ELAPSED -le $SLIDING_WINDOW_S ]] && [[ $SYSTEM_EXIT -ne 0 ]]; then
                _record_crash "$SYSTEM_CRASH_LOG"
                recent=$(_check_sliding_window "$SYSTEM_CRASH_LOG")
                echo "WARNING: SAQ system worker exited after ${SYSTEM_ELAPSED}s (exit=$SYSTEM_EXIT, recent_crashes=$recent)"
                if [[ $recent -gt $SLIDING_CRASH_LIMIT ]]; then
                    echo "FATAL: SAQ system worker: $recent crashes in the last ${SLIDING_WINDOW_S}s — failing container." >&2
                    exit 1
                fi
            else
                # Successful run (ran long enough or clean exit) — reset crash log
                rm -f "$SYSTEM_CRASH_LOG"
            fi
            sleep "${SLIDING_RESTART_SLEEP_S}"
        done
    }
    start_saq_system &
    SAQ_SYSTEM_WRAPPER_PID=$!

    # -----------------------------------------------------------------------
    # Worker health liveness server (ADR 021): binds 0.0.0.0:8082 and returns
    # 200 only while BOTH SAQ worker subprocesses are alive (tracked via the PID
    # files the wrappers above write). Backs the top-level [checks.worker_health]
    # in fly.toml — the worker process group has no service/health check today,
    # so a machine that is "up" but running no SAQ workers is invisible to
    # `fly checks list` and to rolling-deploy readiness.
    # The subshell exits 0 on failure (|| true) and is `disown`ed so a health-
    # server crash can never fail the container through `set -e`/`wait`, and so
    # the main `wait` below still returns the SAQ wrappers' exit status alone
    # (fail-closed on crash-limit preserved). The bounded drain handler tears
    # it down on deploy.
    # -----------------------------------------------------------------------
    echo "=== Starting worker health liveness server (0.0.0.0:${WORKER_HEALTH_PORT:-8082}) ==="
    (
        python3 - <<'PY' || echo "worker health server exited (non-fatal)"
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PID_FILES = (
    os.environ.get("RUN_WORKER_PID_FILE", "/tmp/run-worker.pid"),
    os.environ.get("SYSTEM_WORKER_PID_FILE", "/tmp/system-worker.pid"),
)
_PORT = int(os.environ.get("WORKER_HEALTH_PORT", "8082"))


def _saq_workers_alive():
    pids = []
    for path in _PID_FILES:
        try:
            with open(path, encoding="utf-8") as fh:
                pids.append(int(fh.read().strip()))
        except (OSError, ValueError):
            return False
    for pid in pids:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
    return True


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        alive = _saq_workers_alive()
        body = b"worker-health ok" if alive else b"worker-health degraded"
        self.send_response(200 if alive else 503)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("health:%s - %s\n" % (self.address_string(), fmt % args))


ThreadingHTTPServer(("0.0.0.0", _PORT), _Handler).serve_forever()
PY
    ) &
    WORKER_HEALTH_PID=$!
    disown "$WORKER_HEALTH_PID" || true
    echo "Worker health server PID: $WORKER_HEALTH_PID"

    # -----------------------------------------------------------------------
    # FAR-2004: bounded drain on SIGTERM/SIGINT (see the top-of-file comment).
    # The handler is installed only after both workers and the health server
    # are up; a signal before this point has nothing to drain.
    # -----------------------------------------------------------------------
    drain_workers() {
        local deadline alive pid f
        echo "WORKER_DRAIN: begin (deadline=${WORKER_DRAIN_DEADLINE_S}s)"
        # Tell the restart wrappers to stop restarting once their worker exits.
        : > "$WORKER_DRAIN_FLAG"
        # Forward the signal to the SAQ workers only — never to this shell and
        # never to the whole process group. SAQ stops dequeuing on SIGTERM and
        # starts its bounded shutdown; its shutdown hook (FAR-1627) releases or
        # truthfully fails the runs still in its in-flight registry.
        for f in "$RUN_WORKER_PID_FILE" "$SYSTEM_WORKER_PID_FILE"; do
            pid=$(cat "$f" 2>/dev/null || true)
            if _pid_alive "$pid"; then
                kill -TERM "$pid" 2>/dev/null || true
            fi
        done
        deadline=$(( $(date +%s) + WORKER_DRAIN_DEADLINE_S ))
        while :; do
            alive=0
            for f in "$RUN_WORKER_PID_FILE" "$SYSTEM_WORKER_PID_FILE"; do
                pid=$(cat "$f" 2>/dev/null || true)
                if _pid_alive "$pid"; then
                    alive=1
                fi
            done
            if [[ $alive -eq 0 ]]; then
                echo "WORKER_DRAIN: complete — in-flight workers exited"
                break
            fi
            if [[ $(date +%s) -ge $deadline ]]; then
                echo "WORKER_DRAIN: deadline (${WORKER_DRAIN_DEADLINE_S}s) reached with workers still alive — stopping container" >&2
                break
            fi
            sleep 1
        done
        # Stop the restart wrappers and the health server. The wrappers normally
        # exit on their own once the drain flag is set; this is the bounded
        # backstop. Fly's kill_timeout then bounds anything still alive.
        for pid in "$SAQ_RUNS_WRAPPER_PID" "$SAQ_SYSTEM_WRAPPER_PID" "$WORKER_HEALTH_PID"; do
            if _pid_alive "$pid"; then
                kill -TERM "$pid" 2>/dev/null || true
            fi
        done
        exit 0
    }
    trap 'drain_workers' SIGTERM SIGINT
    echo "WORKER_READY: drain trap installed (kill_timeout must exceed ${WORKER_DRAIN_DEADLINE_S}s)"
    wait

else
    # -----------------------------------------------------------------------
    # Web process group -- nginx + uvicorn only, no SAQ workers
    # -----------------------------------------------------------------------

    echo "=== Writing frontend runtime configuration ==="
    python3 - <<'PY'
import os

from modulo.launcher.runtime_config import build_runtime_config_payload, write_runtime_config_js

payload = build_runtime_config_payload(os.environ)
write_runtime_config_js(payload, "/usr/share/nginx/html/runtime-config.js")
PY

    echo "=== Starting nginx ==="
    nginx -g "daemon off;" &
    NGINX_PID=$!

    echo "=== Admin user seeding handled by backend lifespan startup ==="

    echo "=== Starting uvicorn ==="
    uvicorn modulo.api.main:app \
        --host 0.0.0.0 --port ${PORT:-8000} \
        --proxy-headers \
        --timeout-keep-alive 30 \
        --timeout-graceful-shutdown 30 \
        --limit-concurrency 100 &
    UVICORN_PID=$!

    # FAR-2004: same self-signal defect as the worker branch — `kill 0`
    # re-entered this shell's own trap forever instead of waiting for the
    # children. Forward to the two real children and wait, bounded.
    drain_app() {
        local deadline
        echo "APP_DRAIN: begin"
        if _pid_alive "$UVICORN_PID"; then
            kill -TERM "$UVICORN_PID" 2>/dev/null || true
        fi
        if _pid_alive "$NGINX_PID"; then
            kill -TERM "$NGINX_PID" 2>/dev/null || true
        fi
        deadline=$(( $(date +%s) + APP_DRAIN_DEADLINE_S ))
        while _pid_alive "$UVICORN_PID" || _pid_alive "$NGINX_PID"; do
            if [[ $(date +%s) -ge $deadline ]]; then
                echo "APP_DRAIN: deadline (${APP_DRAIN_DEADLINE_S}s) reached — stopping container" >&2
                break
            fi
            sleep 1
        done
        exit 0
    }
    trap 'drain_app' SIGTERM SIGINT
    wait

fi
