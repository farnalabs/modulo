#!/usr/bin/env bash
# Staple a signed+notarized macOS DMG from Apple's ticket store and verify it.
#
# FAR-2480: Apple CloudKit ticket delivery lags notarization. `notarytool submit`
# permanently records the notarization, but the ticket can take minutes to hours
# to become fetchable, and the in-run staple retry can exhaust first. This helper
# is the out-of-band retry: it fetches the ticket from Apple against the persisted
# signed DMG (CloudKit looks the ticket up by the DMG's code-signing identity, so
# the notarytool submission id is not required) and validates every hard gate.
#
# Idempotent: an already-stapled, already-valid DMG is a no-op success, so the
# retry workflow can run on a schedule without churning an already-good release.
#
# Every gate below is fail-closed:
#   * codesign  - the DMG must carry a valid signature before we touch it
#   * stapler   - the ticket must staple and then validate
#   * spctl      - Gatekeeper must accept the DMG
#   * hdiutil    - the disk image must still verify after the ticket is added
#
# Usage:
#   staple-and-verify.sh --dmg <path> [--force] [--github-output]
#
# Writes `staple_result=stapled|unchanged` to $GITHUB_OUTPUT when --github-output
# is given (or when GITHUB_OUTPUT is already set in the environment).
set -euo pipefail

DMG=""
FORCE="false"
WRITE_GITHUB_OUTPUT="false"

usage() {
  cat >&2 <<'USAGE'
usage: staple-and-verify.sh --dmg <path> [--force] [--github-output]

  --dmg <path>       signed+notarized DMG to staple (required)
  --force            re-staple even when the DMG already validates
  --github-output    write staple_result to $GITHUB_OUTPUT
USAGE
}

log() {
  printf '%s staple-and-verify: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

fail() {
  log "ERROR: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dmg)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      DMG="$2"
      shift 2
      ;;
    --force)
      FORCE="true"
      shift
      ;;
    --github-output)
      WRITE_GITHUB_OUTPUT="true"
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      log "unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

[[ -n "$DMG" ]] || { log "--dmg is required" >&2; usage; exit 2; }
[[ -f "$DMG" ]] || fail "DMG not found: $DMG"

# Every tool this helper depends on must be present. A missing binary would make
# a gate silently pass, so refuse to run rather than skip a check.
for tool in codesign xcrun spctl hdiutil; do
  command -v "$tool" >/dev/null 2>&1 || fail "required tool not on PATH: $tool"
done

record_result() {
  local result="$1"
  if [[ "$WRITE_GITHUB_OUTPUT" == "true" || -n "${GITHUB_OUTPUT:-}" ]]; then
    printf 'staple_result=%s\n' "$result" >>"${GITHUB_OUTPUT:-/dev/null}"
  fi
  log "staple_result=${result}"
}

# The DMG must be signed before stapling. This is the same gate CI runs on the
# freshly built image; re-running it here stops a wrong or corrupted download
# from being stapled and published.
log "codesign --verify $DMG"
codesign --verify --verbose=2 "$DMG" || fail "DMG code signature is invalid; refusing to staple"

# Idempotency guard: an already-stapled DMG validates offline and needs nothing
# further. Do not contact Apple and do not touch the file's bytes.
if [[ "$FORCE" != "true" ]] && xcrun stapler validate "$DMG" >/dev/null 2>&1; then
  log "DMG already has a valid notarization ticket; nothing to staple"
  spctl --assess --type open --context context:primary-signature "$DMG" ||
    fail "already-stapled DMG failed Gatekeeper assessment"
  record_result unchanged
  exit 0
fi

log "stapling $DMG from Apple's ticket store"
xcrun stapler staple "$DMG" || fail "stapler staple failed; ticket is not yet fetchable from CloudKit"

# Hard gates after stapling: the ticket must validate, Gatekeeper must accept the
# DMG, and appending the ticket must not corrupt the disk image.
log "validating stapled ticket"
xcrun stapler validate "$DMG" || fail "stapler validate failed after staple"

log "spctl Gatekeeper assessment"
spctl --assess --type open --context context:primary-signature "$DMG" ||
  fail "Gatekeeper assessment failed after staple"

log "hdiutil verify"
hdiutil verify "$DMG" || fail "hdiutil verify failed after staple"

record_result stapled
log "DMG stapled and verified"
