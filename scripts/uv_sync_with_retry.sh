#!/usr/bin/env bash
# Shared retry wrapper for `uv sync` dependency installs in CI.
#
# WHY: the backend lockfile pins source-archive dependencies (notably
# pytest-bdd @ https://github.com/pytest-dev/pytest-bdd/archive/<sha>.tar.gz).
# `uv sync --frozen` downloads those archives from GitHub's codeload CDN, which
# intermittently answers 504 Gateway Timeout. That is pure infrastructure
# flake - the pin is immutable - yet it failed the whole pre-deploy gate on
# main (deploy runs 42471171588 and 42474368995, 2026-11-08) in four jobs at
# once, before a single test ran.
#
# CLASSIFICATION: retry ONLY on transient download/network signatures. A
# failure that does not match them (an unsatisfiable lockfile, a resolver
# conflict, a deterministic build error) is TERMINAL and fails immediately so
# the loud ::error:: annotation is not delayed by pointless waiting.
#
# BOUNDED: attempts and backoff are capped, so a genuine outage cannot hold a
# pre-deploy job open indefinitely.
#
# Usage:
#   bash scripts/uv_sync_with_retry.sh --frozen
#   bash ../scripts/uv_sync_with_retry.sh --frozen          # when working-directory is backend
#   bash scripts/uv_sync_with_retry.sh --project backend --frozen
#
# Environment overrides (tests use them):
#   UV_SYNC_MAX_ATTEMPTS   total attempts (default 3)
#   UV_SYNC_BACKOFF_SECONDS  linear backoff base in seconds (default 15)

set -uo pipefail

MAX_ATTEMPTS="${UV_SYNC_MAX_ATTEMPTS:-3}"
BACKOFF_SECONDS="${UV_SYNC_BACKOFF_SECONDS:-15}"

# The transient-download signatures observed from uv when the archive host is
# unhealthy. Keep this list to NETWORK/SERVER failures only.
TRANSIENT_RE='HTTP status server error|Failed to download|failed to download|Gateway Timeout|Bad Gateway|Service Unavailable|[45]0[234] (Gateway|Bad|Service|error)|timed out|Timed out|Timeout|Connection reset|Connection refused|Could not connect|Max retries exceeded|Temporary failure in name resolution|EOF occurred'

LOG="$(mktemp)"
trap 'rm -f "${LOG}"' EXIT

for attempt in $(seq 1 "${MAX_ATTEMPTS}"); do
  echo "--- uv sync attempt ${attempt} of ${MAX_ATTEMPTS}: uv sync $* ---"
  : > "${LOG}"
  uv sync "$@" 2>&1 | tee "${LOG}"
  RC=${PIPESTATUS[0]}
  if [ "${RC}" -eq 0 ]; then
    exit 0
  fi
  if [ "${attempt}" -ge "${MAX_ATTEMPTS}" ]; then
    echo "::error::uv sync failed after ${MAX_ATTEMPTS} attempts (exit ${RC})."
    exit "${RC}"
  fi
  if grep -qE "${TRANSIENT_RE}" "${LOG}"; then
    echo "::warning::uv sync hit a transient download/network error (attempt ${attempt}/${MAX_ATTEMPTS}); retrying in ${BACKOFF_SECONDS}s."
    sleep "${BACKOFF_SECONDS}"
    continue
  fi
  echo "::error::uv sync failed with a non-transient error (exit ${RC}); not retrying."
  exit "${RC}"
done
