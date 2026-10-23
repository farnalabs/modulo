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
# Contract
# --------
# - Usage: restart-wedged-app-machines.sh <app>
# - Restarts only machines that are `started`, in the `app` process group,
#   whose `app_liveness` check is `critical` (the process-local probe - never
#   servicecheck-*, which is deployment-scoped; restarting on it would storm
#   every app machine for a dead queue. Same selection as web-watchdog.yml).
# - Best-effort by design: a missing flyctl, an unreadable machine list, or a
#   failed restart emits a GitHub warning and exits 0. The deploy attempt that
#   follows is still the authoritative gate and fails loudly on its own; this
#   step must never mask or pre-empt it (2026-08-27 masked-deploy incident).
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

while IFS= read -r MID; do
  [ -z "${MID}" ] && continue
  echo "::notice::Restarting wedged app machine ${MID} in ${APP} (app_liveness critical) so the next deploy attempt can make progress."
  if fly machine restart "${MID}" --app "${APP}" </dev/null; then
    echo "Restarted ${MID}."
  else
    echo "::warning::fly machine restart failed for ${MID} in ${APP} - continuing to the next deploy attempt."
  fi
done <<< "${WEDGED_IDS}"

exit 0
