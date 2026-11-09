#!/usr/bin/env bash
# Install a pinned, SHA-256-verified ShellCheck binary WITHOUT Homebrew.
#
# Intel (x86_64) macOS is a Homebrew Tier-3 configuration: Homebrew no longer
# builds bottles for x86_64 Darwin, so `brew install shellcheck` there either
# fails at the bottle fetch or falls back to an unbounded source build
# (FAR-2479). The Intel macOS installer therefore fetches the pinned upstream
# ShellCheck release directly and verifies it against a pinned SHA-256 before
# it is used, so the Intel bundle never depends on Homebrew (FAR-2486).
#
# The digests below come from the GitHub release asset metadata for
# https://github.com/koalaman/shellcheck/releases/tag/v0.11.0 (sha256 digest
# published per asset). Bumping the version means re-pinning BOTH digests from
# the new release assets in the same change.
#
# Usage: install-pinned-shellcheck.sh <arch: arm64|x86_64> <install-prefix>
# On success the binary is written to <install-prefix>/shellcheck and the
# install prefix is printed on stdout.
set -euo pipefail

ARCH="${1:?architecture required (arm64 or x86_64)}"
PREFIX="${2:?install prefix required}"

SHELLCHECK_VERSION="v0.11.0"
# Overridable only so the download/verification contract can be exercised
# against a loopback fixture; production uses the default upstream host.
SHELLCHECK_BASE_URL="${SHELLCHECK_BASE_URL:-https://github.com/koalaman/shellcheck/releases/download}"
# The transport is pinned to HTTPS by default. The regression test narrows this
# to the loopback fixture server it serves; never widen it for a real build.
SHELLCHECK_DOWNLOAD_PROTOCOLS="${SHELLCHECK_DOWNLOAD_PROTOCOLS:-https}"
SHELLCHECK_MAX_ATTEMPTS="${SHELLCHECK_MAX_ATTEMPTS:-5}"

case "$ARCH" in
  x86_64)
    TARBALL="shellcheck-${SHELLCHECK_VERSION}.darwin.x86_64.tar.gz"
    SHA256="c2c15e08df0e8fbc374c335b230a7ee958c313fa5714817a59aa59f1aa594f51"
    ;;
  arm64)
    TARBALL="shellcheck-${SHELLCHECK_VERSION}.darwin.aarch64.tar.gz"
    SHA256="339b930feb1ea764467013cc1f72d09cd6b869ebf1013296ba9055ab2ffbd26f"
    ;;
  *)
    printf '::error::install-pinned-shellcheck: unsupported architecture: %s\n' "$ARCH" >&2
    exit 2
    ;;
esac

# BEGIN shellcheck_verify_install (extracted verbatim by test_macos_installer_normalize)
sha256_digest() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | awk '{print $1}'
  else
    openssl dgst -sha256 "$path" | awk '{print $NF}'
  fi
}

# verify_and_install_shellcheck <archive> <expected-sha256> <prefix> <version> <work-dir>
#
# Verifies the archive's SHA-256 BEFORE anything is copied into the destination,
# then extracts the release and installs its `shellcheck` binary. Any mismatch,
# missing archive, or missing binary is a hard failure: an unverified artefact
# is never installed.
verify_and_install_shellcheck() {
  local archive="$1" expected_sha256="$2" prefix="$3" version="$4" work_dir="$5"
  local actual candidate binary=''
  if [ ! -s "$archive" ]; then
    printf '::error::install-pinned-shellcheck: downloaded archive is missing or empty: %s\n' "$archive" >&2
    return 1
  fi
  actual="$(sha256_digest "$archive")"
  if [ "$actual" != "$expected_sha256" ]; then
    printf '::error::install-pinned-shellcheck: checksum verification failed (expected %s, got %s)\n' \
      "$expected_sha256" "$actual" >&2
    return 1
  fi
  printf 'install-pinned-shellcheck: verified sha256 of %s\n' "$archive" >&2
  mkdir -p "$work_dir/unpacked"
  tar -xzf "$archive" -C "$work_dir/unpacked"
  for candidate in "$work_dir/unpacked/shellcheck-${version}/shellcheck" "$work_dir/unpacked/shellcheck"; do
    if [ -f "$candidate" ]; then
      binary="$candidate"
      break
    fi
  done
  if [ -z "$binary" ]; then
    printf '::error::install-pinned-shellcheck: shellcheck binary not found in %s\n' "$archive" >&2
    return 1
  fi
  mkdir -p "$prefix"
  cp "$binary" "$prefix/shellcheck"
  chmod 755 "$prefix/shellcheck"
  "$prefix/shellcheck" --version >&2
}
# END shellcheck_verify_install

# download_verified <url> <destination> <label>
#
# Bounded retries for transient HTTP/network failures (the same classification
# bundle-release.yml uses); a permanent failure returns immediately. The
# checksum is verified by verify_and_install_shellcheck before installation.
# BEGIN download_verified (extracted verbatim by test_macos_installer_normalize)
download_verified() {
  local url="$1" destination="$2" label="$3"
  local attempt=1 status curl_rc delay jitter retry_delay temporary status_file
  temporary="${destination}.part"
  rm -f -- "$temporary"
  while [ "$attempt" -le "$SHELLCHECK_MAX_ATTEMPTS" ]; do
    status_file="${temporary}.status"
    printf 'download %s: attempt %s/%s\n' "$label" "$attempt" "$SHELLCHECK_MAX_ATTEMPTS" >&2
    set +e
    curl -fsSL --proto "=${SHELLCHECK_DOWNLOAD_PROTOCOLS}" --connect-timeout 20 --max-time 300 \
      --retry 0 -o "$temporary" -w '%{http_code}' "$url" >"$status_file"
    curl_rc=$?
    set -e
    status="$(cat "$status_file" 2>/dev/null || true)"
    rm -f -- "$status_file"
    if [ "$curl_rc" -eq 0 ]; then
      mv -- "$temporary" "$destination"
      printf 'download %s: saved %s\n' "$label" "$destination" >&2
      return 0
    fi
    case "$curl_rc" in
      22)
        case "$status" in
          408|425|429|500|502|503|504)
            printf 'download %s: transient HTTP %s (curl exit %s)\n' "$label" "$status" "$curl_rc" >&2
            ;;
          *)
            printf '::error::download %s: permanent HTTP %s (curl exit %s)\n' "$label" "$status" "$curl_rc" >&2
            rm -f -- "$temporary"
            return "$curl_rc"
            ;;
        esac
        ;;
      6|7|18|28|52|55|56)
        printf 'download %s: transient network failure (curl exit %s)\n' "$label" "$curl_rc" >&2
        ;;
      *)
        printf '::error::download %s: permanent failure (curl exit %s, HTTP %s)\n' \
          "$label" "$curl_rc" "${status:-none}" >&2
        rm -f -- "$temporary"
        return "$curl_rc"
        ;;
    esac
    rm -f -- "$temporary"
    if [ "$attempt" -eq "$SHELLCHECK_MAX_ATTEMPTS" ]; then
      printf '::error::download %s: exhausted %s attempts\n' "$label" "$SHELLCHECK_MAX_ATTEMPTS" >&2
      return 1
    fi
    delay=$((1 << (attempt - 1)))
    jitter=$((RANDOM % 3))
    retry_delay=$((delay + jitter))
    if [ "$retry_delay" -gt 10 ]; then
      retry_delay=10
    fi
    printf 'download %s: retrying in %ss\n' "$label" "$retry_delay" >&2
    sleep "$retry_delay"
    attempt=$((attempt + 1))
  done
}
# END download_verified

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/modulo-shellcheck.XXXXXX")"
cleanup() { rm -rf -- "$WORK_DIR"; }
trap cleanup EXIT

ARCHIVE="$WORK_DIR/$TARBALL"
download_verified "${SHELLCHECK_BASE_URL%/}/${SHELLCHECK_VERSION}/${TARBALL}" "$ARCHIVE" "shellcheck ${SHELLCHECK_VERSION} ${ARCH}"
verify_and_install_shellcheck "$ARCHIVE" "$SHA256" "$PREFIX" "$SHELLCHECK_VERSION" "$WORK_DIR"

printf '%s\n' "$PREFIX"
