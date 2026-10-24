#!/usr/bin/env bash
# Restart a WEDGED app-process machine between `fly deploy` attempts.
#
# Why this exists (2026-10-23 staging outage)
# -------------------------------------------
# A Fly app machine can be `started` while its HTTP server is unresponsive
# (the wedge documented in .github/workflows/web-watchdog.yml). A rolling
# deploy cannot heal that state: `fly deploy` updates the machine in place and
# then waits for the NEW health check on that SAME machine, so every retry
# times out against the same wedge:
#
#   Unrecoverable error: timeout reached waiting for health checks to pass
#   for machine <id>: failed to get VM <id>: ...: net/http: request canceled
#
# The out-of-band web-watchdog is not a recovery path here either: its
# deploy guard refuses to restart any machine while a deploy run is in
# progress or queued, and its freshness guard refuses while the machine's
# `updated_at` is younger than 15 minutes - both of which a failing deploy
# retry loop keeps re-arming. That deadlock left staging down and every deploy
# failing from 12:03 to 13:36 UTC.
#
# The deploy retry loop is the one place that KNOWS the previous attempt
# failed, so it is the right place to break the deadlock: before retrying,
# restart the app machine whose process-local liveness check is critical. The
# machine then boots the config/image the failed attempt already wrote, and
# the retry can make progress.
#
# 2026-10-23 follow-up (staging still down after the restart fix shipped):
# a restart is not always enough. When the VM itself is unreachable, the
# restart waits its own ~2 minute health window, fails with `watching machine
# checks: context deadline exceeded`, and leaves the SAME wedged machine for
# the next `fly deploy` to update in place - which times out identically
# (observed on e29d961d0: restart failed, then attempt 3's own 5 minute health
# wait also failed). A machine that cannot be restarted cannot be deployed
# onto, so the fallback is to DESTROY it: the next `fly deploy` sees the app
# below its `min_machines_running` count and creates a fresh machine from the
# image/config instead of updating a corpse in place. The machine is already
# unserviceable at that point (its process-local liveness probe is critical),
# so removing it costs no availability.
#
# Contract
# --------
# - Usage: restart-wedged-app-machines.sh <app>
# - Restarts only machines that are `started`, in the `app` process group,
#   whose `app_liveness` check is `critical` (the process-local probe - never
#   servicecheck-*, which is deployment-scoped; restarting on it would storm
#   every app machine for a dead queue. Same selection as web-watchdog.yml).
# - If the restart fails, destroys the same machine so the next deploy attempt
#   can create a replacement. A successful restart is never followed by a
#   destroy.
# - Captures the wedged machine's boot log to
#   ${WEDGED_LOG_DIR:-/tmp/app-machine-logs}/<machine-id>.log before touching
#   it (bounded, best-effort, credentials redacted) so deploy.yml can upload
#   the evidence; a destroy otherwise discards it.
# - Best-effort by design: a missing flyctl, an unreadable machine list, or a
#   failed restart/destroy emits a GitHub warning and exits 0. The deploy
#   attempt that follows is still the authoritative gate and fails loudly on
#   its own; this step must never mask or pre-empt it (2026-08-27
#   masked-deploy incident).
# - Exits 0 when there is nothing to do (the common case).
set -euo pipefail

APP="${1:?usage: restart-wedged-app-machines.sh <app>}"

if ! command -v fly >/dev/null 2>&1; then
  echo "::warning::flyctl not on PATH - cannot check ${APP} for a wedged app machine; continuing to the next deploy attempt."
  exit 0
fi

MACHINES_JSON="$(fly machines list --app "${APP}" --json 2>/dev/null || true)"
if [ -z "${MACHINES_JSON}" ]; then
  echo "::warning::fly machines list failed for ${APP} - machine state unknown; skipping wedge recovery and continuing to the next deploy attempt."
  exit 0
fi

# Emit one machine id per line for started app-group machines whose
# app_liveness check is critical. A parse failure is treated as "unknown"
# (warn, no action) rather than "healthy".
WEDGED_IDS="$(printf '%s' "${MACHINES_JSON}" | python3 -c '
import json, sys

try:
    machines = json.load(sys.stdin)
except Exception as exc:  # noqa: BLE001 - any parse failure means "unknown"
    print(f"::warning::could not parse the flyctl machines output: {exc}", file=sys.stderr)
    sys.exit(1)

for machine in machines or []:
    if str(machine.get("state", "")) != "started":
        continue
    config = machine.get("config") or {}
    group = (config.get("metadata") or {}).get("fly_process_group") or config.get("env", {}).get("FLY_PROCESS_GROUP") or ""
    if str(group) != "app":
        continue
    checks = machine.get("checks") or []
    if any(str(c.get("name", "")) == "app_liveness" and str(c.get("status", "")) == "critical" for c in checks):
        print(machine.get("id", ""))
')" || {
  echo "::warning::could not parse the flyctl machines output for ${APP} - machine state unknown; skipping wedge recovery."
  exit 0
}

if [ -z "${WEDGED_IDS}" ]; then
  echo "No wedged app machine in ${APP} - nothing to restart."
  exit 0
fi

# Where the wedged machine's boot log is preserved for the deploy job's
# artifact upload (deploy.yml "Upload app machine logs"). A destroy discards
# the only record of WHY the app process group never became healthy, which is
# exactly what made the 2026-10-23 outage undiagnosable from CI alone: fresh
# machines reached `started`, never passed a check, and each retry replaced
# them with no log surviving.
LOG_DIR="${WEDGED_LOG_DIR:-/tmp/app-machine-logs}"
mkdir -p "${LOG_DIR}" 2>/dev/null || true

while IFS= read -r MID; do
  [ -z "${MID}" ] && continue
  # Bounded and best-effort: capturing the log must never delay or fail the
  # recovery it precedes.
  LOG_FILE="${LOG_DIR}/${MID}.log"
  timeout 90 fly logs --app "${APP}" --machine "${MID}" --no-tail > "${LOG_FILE}" 2>&1 || true
  # The entrypoint prints the DATABASE_URL prefix, so redact the credential
  # part before the log is echoed or persisted. Match on the `://user:pw@`
  # shape rather than a bare `postgres(ql)://` prefix: driver-qualified schemes
  # (postgresql+asyncpg://, postgresql+psycopg://) otherwise leak the password
  # into this file and the deploy artifact (2026-10-23 outage).
  sed -i -E 's#(://[^:/@]+:)[^@]*@#\1***@#g' "${LOG_FILE}" 2>/dev/null || true
  echo "::notice::Restarting wedged app machine ${MID} in ${APP} (app_liveness critical) so the next deploy attempt can make progress."
  if [ -s "${LOG_FILE}" ]; then
    echo "----- wedged machine ${MID} log (tail) -----"
    tail -n 60 "${LOG_FILE}" || true
    echo "----- end wedged machine ${MID} log -----"
  fi
  if fly machine restart "${MID}" --app "${APP}" </dev/null; then
    echo "Restarted ${MID}."
  else
    # The restart failed (typically its health wait timed out), so the same
    # machine would swallow the next `fly deploy` attempt too. Remove it and
    # let the deploy create a healthy replacement.
    echo "::warning::fly machine restart failed for ${MID} in ${APP} - destroying it so the next deploy attempt can create a replacement."
    if fly machine destroy "${MID}" --app "${APP}" --force </dev/null; then
      echo "Destroyed wedged machine ${MID}; the next deploy attempt will create a replacement."
    else
      echo "::warning::fly machine destroy failed for ${MID} in ${APP} - continuing to the next deploy attempt."
    fi
  fi
done <<< "${WEDGED_IDS}"

exit 0
