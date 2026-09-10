#!/usr/bin/env bash
#
# Modulo native installer (Linux) — single-install distribution, P1a.
# Spec: ADR 031 (single-install native distribution); this ticket: FAR-670.
#        + pre-upgrade upgrade path (FAR-672).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/farnalabs/modulo/main/scripts/install.sh -o modulo-install.sh
#   bash modulo-install.sh [options]
#   ./install.sh [--force] [--from-file <tarball>] [--skip-backup]
#
# NOTE: download to a file and run it (as above) rather than streaming a
# download straight into a shell. install.sh verifies every download's sha256
# BEFORE executing anything, so the saved-file path is the one that actually
# enforces integrity; a streamed install skips that verification entirely.
#
# What it does:
#   1. Downloads the bundle release tarball for the detected architecture
#      (P1a: linux amd64/arm64) from GitHub Releases.
#   2. Verifies its sha256 against the release SHA256SUMS BEFORE extraction.
#   3. Lays it out under a user-writable install root:
#        ~/.local/opt/modulo/versions/<version>/    (the extracted bundle)
#        ~/.local/opt/modulo/current                (symlink -> versions/<version>)
#      The `current` symlink is the primitive reused by the upgrade machinery
#      (FAR-672): upgrades re-point it; nothing else is version-specific.
#   4. Writes a PATH shim at ~/.local/bin/modulo pointing at current/launcher.
#
# Upgrade path (FAR-672, ADR 031 Decision 7 — the P1a upgrade = rerun this
# installer against an existing install):
#
#   1. When a populated bundled data dir exists, the installer ENFORCES a
#      pre-upgrade pg_dump via the OLD installation's bundled runtime
#      (python -m modulo.launcher.upgrade): bundled pg_dump client, dump
#      written to a versioned snapshot dir inside the data dir, verified
#      non-empty. ANY dump failure aborts the whole installer — no binary
#      swap happens without a verified snapshot.
#      The dump needs the bundled Postgres REACHABLE, so run the installer
#      FIRST (while the launcher is running), then:
#   2. The installer REFUSES to perform the versioned-dir/current-symlink
#      swap while the data-dir lock is held (the live launcher holds it).
#      Stop the launcher ('modulo stop'), then re-run this installer with
#      --skip-backup: the flag is THE loud, explicit acknowledgement that
#      the enforced dump already succeeded and must not be redone (the
#      stopped stack cannot be dumped).
#   3. The swap itself reuses the versioned dir + `current` symlink
#      primitive from step 3 above; the PATH shim never changes.
#
# Migrating from Docker Compose (pg_dump -> `modulo restore`):
#   docker compose exec postgres pg_dump -U modulo --clean --if-exists \
#            --no-owner --no-acl modulo > modulo-backup.sql
#   docker compose down
#   bash modulo-install.sh            # fresh native install
#   modulo restore <backup-dir> --data-dir <fresh-data-dir> --yes
#   (or restore the SQL directly: pg_restore-free plain SQL is replayed via
#   psql by the backup CLI). The compose cluster is alpine/musl and the
#   native one is glibc — the restore hard-warns on collation-version drift
#   (pg_collation versions) and prints reindexdb guidance (FAR-672).
#
# Bundle artifact layout (must stay in sync with .github/workflows/bundle-release.yml):
#   modulo-<version>-linux-<arch>.tar.gz
#     └── modulo-<version>-linux-<arch>/      (single top-level directory)
#         ├── VERSION                         (the release tag, e.g. bundle-v0.1.0)
#         ├── SHA256SUMS                      (sha256 over every shipped file)
#         ├── launcher                        (FAR-671: real launcher; placeholder for now)
#         ├── runtime/                        (python-build-standalone CPython tree)
#         ├── pg/                             (PostgreSQL server + client binaries)
#         ├── redis/                          (redis-server + redis-cli)
#         ├── backend/                        (backend src, .venv, pyproject.toml, uv.lock)
#         └── frontend-dist/                  (built SPA static files)

set -euo pipefail

REPO="farnalabs/modulo"
RELEASES_LATEST_URL="https://github.com/${REPO}/releases/latest"
RELEASES_DOWNLOAD_URL="https://github.com/${REPO}/releases/download"

INSTALL_ROOT="${MODULO_INSTALL_ROOT:-${HOME}/.local/opt/modulo}"
BIN_DIR="${MODULO_BIN_DIR:-${HOME}/.local/bin}"
MARKER_FILE="${INSTALL_ROOT}/.modulo-native-install"

FORCE=0
FROM_FILE=""
SKIP_BACKUP=0

die() {
  printf 'ERROR: %b\n' "$*" >&2
  exit 1
}

info() {
  printf '%s\n' "$*"
}

usage() {
  cat <<'EOF'
Modulo native installer (Linux, P1a)

Options:
  --force                  Replace an existing /usr/local/bin/modulo that this
                           installer did not create (it is renamed, not deleted).
  --from-file <tarball>    Install from a locally downloaded
                           modulo-<version>-linux-<arch>.tar.gz instead of
                           downloading from GitHub Releases (offline installs;
                           see ADR 031 for the signed-manifest roadmap).
  --skip-backup            Skip the upgrade's ENFORCED pre-upgrade pg_dump
                           (upgrade-only). LOUD: pass this ONLY when a previous
                           installer run already produced the snapshot and the
                           bundled stack is stopped (the swap needs it stopped,
                           and a stopped stack cannot be dumped). The installer
                           prints the snapshot path it left behind.
  --help                   Show this help.

Environment overrides:
  VERSION=<tag>            Install a specific bundle tag (default: latest release).
  MODULO_INSTALL_ROOT      Install root (default: ~/.local/opt/modulo).
  MODULO_BIN_DIR           Shim directory (default: ~/.local/bin).
  MODULO_DATA_DIR          Bundled data dir (default: ~/.local/share/modulo/data).

When running the downloaded script, pass options like:  bash modulo-install.sh --force
EOF
}

fetch() {
  # fetch <url> <output-path> — curl with strict TLS, retries, and friendly
  # failure text (proxy / corporate TLS interception / offline alternative).
  local url="$1" out="$2" rc
  curl -fsSL --proto '=https' --tlsv1.2 --connect-timeout 15 --retry 3 --retry-delay 2 -o "$out" "$url" || {
    rc=$?
    printf 'ERROR: download failed (curl exit %s): %s\n' "$rc" "$url" >&2
    printf '\nTroubleshooting:\n' >&2
    printf '  - Behind a corporate proxy? Export HTTPS_PROXY and HTTP_PROXY (curl honours both).\n' >&2
    printf '  - Corporate TLS interception? curl may reject the interception CA\n' >&2
    printf '    (curl exit 60). Ask IT to trust the CA system-wide, or install from a file:\n' >&2
    printf '      install.sh --from-file /path/to/modulo-<version>-linux-<arch>.tar.gz\n' >&2
    exit 1
  }
}

sha256_of() {
  # sha256_of <file> — print the digest, preferring sha256sum.
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    die "no sha256 tool found (need sha256sum or shasum) — cannot verify the download"
  fi
}

verify_sums_in_dir() {
  # verify_sums_in_dir <dir> — verify every file listed in <dir>/SHA256SUMS.
  local dir="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    (cd "$dir" && sha256sum --check --quiet SHA256SUMS) || {
      die "bundle integrity check FAILED: one or more files in the bundle do not match SHA256SUMS. Do not use this download — report it at https://github.com/${REPO}/issues"
    }
  else
    local sum path actual
    while read -r sum path; do
      actual="$(sha256_of "${dir}/${path}")"
      [ "$actual" = "$sum" ] || die "bundle integrity check FAILED for ${path}: expected ${sum}, got ${actual}"
    done < "${dir}/SHA256SUMS"
  fi
}

# --- upgrade machinery (FAR-672) --------------------------------------------

DATA_DIR="${MODULO_DATA_DIR:-${XDG_DATA_HOME:-${HOME}/.local/share}/modulo/data}"
LOCK_FILE="${DATA_DIR}.lock"

# lock_holder_pid — the holder PID recorded in the data-dir lock file, or 0.
# The native launcher writes {"pid": N, "mode": ..., "starttime": ...} there
# while it holds the exclusive flock; it truncates on a CLEAN release, so a
# recorded PID is either a live holder or (after SIGKILL) a stale identity.
lock_holder_pid() {
  local pid
  [ -f "${LOCK_FILE}" ] || return 1
  pid="$(grep -o '"pid": *[0-9]*' "${LOCK_FILE}" 2>/dev/null | grep -o '[0-9]*' | head -n 1)" || return 1
  [ -n "${pid}" ] || return 1
  printf '%s' "${pid}"
}

# lock_holder_mode — the holder's recorded lock mode (backup/restore/serve),
# for the refusal transcript. Empty when the record lacks a mode field.
lock_holder_mode() {
  [ -f "${LOCK_FILE}" ] || return 1
  grep -o '"mode": *"[^"]*"' "${LOCK_FILE}" 2>/dev/null | head -n 1 | sed 's/"mode": *//' || true
}

# refuse_when_locked — the swap phase NEVER races a live launcher.
# Linux-first (P1a): liveness = /proc/<pid>. On a non-Linux host /proc/<pid>
# never matches, so a record would read as stale — but this installer refuses
# non-Linux platforms in preflight() anyway, so that branch is unreachable.
# The holder PID is RE-READ after the /proc check: a lock can change hands
# between the grep and the /proc check, and the die/info text must describe
# the record that is actually on disk now.
refuse_when_locked() {
  local holder_pid holder_mode
  holder_pid="$(lock_holder_pid)" || return 0
  [ -n "${holder_pid}" ] || return 0
  holder_mode="$(lock_holder_mode)"
  if [ -d "/proc/${holder_pid}" ]; then
    holder_pid="$(lock_holder_pid)" || return 0
    holder_mode="$(lock_holder_mode)"
    die "Refusing: the data dir is locked by another launcher (holder PID ${holder_pid}${holder_mode:+, mode ${holder_mode}}).\nStop it first:  modulo stop\nThen re-run this installer. The enforced pre-upgrade snapshot already on disk is reused when you pass --skip-backup:\n  bash modulo-install.sh --skip-backup"
  fi
  info "INFO: stale lock record (holder PID ${holder_pid}${holder_mode:+, mode ${holder_mode}} is gone) — the kernel released the lock; proceeding."
}

# run_pre_upgrade_dump — the INSTALLER-ENFORCED pre-upgrade pg_dump (ADR 031
# Decision 7). Runs the OLD (current) installation's bundled runtime upgrade
# helper: bundled pg_dump client, versioned snapshot inside the data dir,
# non-empty verification in the helper itself. ANY failure aborts here and
# nothing version-specific is laid out. The snapshot path is the helper's
# final stdout line (the installer reports it; the operator aborts with it).
run_pre_upgrade_dump() {
  local old_root="${INSTALL_ROOT}/current"
  local py="${old_root}/runtime/bin/python3"
  local helper="${old_root}/backend/src/modulo/launcher/upgrade.py"
  local rc snapshot_path
  [ -x "${py}" ] || die "cannot run the pre-upgrade pg_dump: no bundled runtime at ${py} (no prior native install to upgrade — remove the populated data dir or ignore)"
  [ -f "${helper}" ] || die "cannot run the pre-upgrade pg_dump: the installed bundle predates the launcher upgrade helper (${helper})"
  info "Pre-upgrade pg_dump (enforced by FAR-672) — the bundled stack must be running..."
  if snapshot_path="$(PYTHONPATH="${old_root}/backend/src${PYTHONPATH:+:${PYTHONPATH}}" \
      MODULO_BUNDLED_BIN_DIR="${old_root}/pg" \
      "${py}" -m modulo.launcher.upgrade --data-dir "${DATA_DIR}")"; then
    :
  else
    rc=$?
    die "pre-upgrade pg_dump FAILED (exit ${rc}) — upgrade ABORTED, no binary swap was made. The installer aborted BEFORE installing anything; fix the dump failure and re-run. If a verified snapshot ALREADY exists in ${DATA_DIR} and the bundled stack is stopped (a stopped stack cannot be dumped), re-run with --skip-backup:\n  bash modulo-install.sh --skip-backup"
  fi
  [ -d "${snapshot_path}" ] || die "pre-upgrade pg_dump succeeded but the snapshot path is missing: '${snapshot_path}'"
  UPGRADE_SNAPSHOT_PATH="${snapshot_path}"
  info "Pre-upgrade snapshot verified: ${UPGRADE_SNAPSHOT_PATH}"
  info "  Restore later with: modulo restore ${UPGRADE_SNAPSHOT_PATH} --data-dir ${DATA_DIR} --yes"
}

resolve_latest_tag() {
  # Follow the /releases/latest redirect (no API token, no API rate limit):
  # https://github.com/farnalabs/modulo/releases/latest -> .../tag/<tag>
  local effective
  effective="$(curl -fsSLI --proto '=https' --tlsv1.2 --connect-timeout 15 -o /dev/null -w '%{url_effective}' "${RELEASES_LATEST_URL}")" || {
    printf 'ERROR: could not resolve the latest bundle release (network or proxy problem).\n' >&2
    printf 'Set VERSION=<tag> to pin one, or use --from-file for offline installs.\n' >&2
    exit 1
  }
  RELEASE_TAG="${effective##*/}"
  case "${RELEASE_TAG}" in
    bundle-v*) : ;;
    *) die "resolved release tag '${RELEASE_TAG}' is not a bundle tag (expected bundle-v*)" ;;
  esac
}

preflight() {
  [ "$(uname -s)" = "Linux" ] || die "this installer supports Linux only (P1a). macOS and Windows installers come later — see ADR 031."
  command -v curl >/dev/null 2>&1 || die "curl is required (install it, e.g. 'apt install curl')"
  command -v tar >/dev/null 2>&1 || die "tar is required (install it, e.g. 'apt install tar')"
  command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || die "a sha256 tool (sha256sum or shasum) is required to verify the download"

  case "$(uname -m)" in
    x86_64) ARCH="amd64" ;;
    aarch64 | arm64) ARCH="arm64" ;;
    *)
      die "unsupported architecture: $(uname -m). Supported: x86_64 (amd64), aarch64 (arm64). Browse https://github.com/${REPO}/releases for available artifacts."
      ;;
  esac
}

# --- argument parsing -------------------------------------------------------

while [ $# -gt 0 ]; do
  case "$1" in
    --force)
      FORCE=1
      shift
      ;;
    --from-file)
      [ $# -ge 2 ] || die "--from-file requires a path to modulo-<version>-linux-<arch>.tar.gz"
      FROM_FILE="$2"
      shift 2
      ;;
    --skip-backup)
      SKIP_BACKUP=1
      shift
      ;;
    --help | -h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
done

preflight

# --- resolve version --------------------------------------------------------

if [ -n "${FROM_FILE}" ]; then
  [ -f "${FROM_FILE}" ] || die "--from-file: no such file: ${FROM_FILE}"
  tarball_name="$(basename "${FROM_FILE}")"
  case "${tarball_name}" in
    modulo-*-linux-*.tar.gz)
      VERSION_NUM="${tarball_name#modulo-}"
      VERSION_NUM="${VERSION_NUM%%-linux-*}"
      ;;
    *)
      die "cannot derive the bundle version from '${tarball_name}' (expected modulo-<version>-linux-<arch>.tar.gz)"
      ;;
  esac
  RELEASE_TAG="(offline: ${tarball_name})"
else
  if [ -n "${VERSION:-}" ] && [ "${VERSION:-}" != "latest" ]; then
    RELEASE_TAG="${VERSION}"
    case "${RELEASE_TAG}" in
      bundle-v*) : ;;
      *) die "VERSION='${RELEASE_TAG}' is not a bundle tag (expected bundle-v*)" ;;
    esac
  else
    resolve_latest_tag
  fi
  VERSION_NUM="${RELEASE_TAG#bundle-v}"
  tarball_name="modulo-${VERSION_NUM}-linux-${ARCH}.tar.gz"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

# --- download and verify the tarball BEFORE extraction ----------------------

if [ -n "${FROM_FILE}" ]; then
  info "Offline install from ${FROM_FILE}"
  info "Note: the release-page checksum cannot be checked for offline installs; the bundle's internal SHA256SUMS is still verified after extraction. Signed manifests (minisign) land in P1b — see ADR 031."
  cp -- "${FROM_FILE}" "${tmp}/${tarball_name}"
else
  info "Installing Modulo bundle ${RELEASE_TAG} (linux-${ARCH})"
  fetch "${RELEASES_DOWNLOAD_URL}/${RELEASE_TAG}/SHA256SUMS" "${tmp}/SHA256SUMS"
  fetch "${RELEASES_DOWNLOAD_URL}/${RELEASE_TAG}/${tarball_name}" "${tmp}/${tarball_name}"

  expected="$(awk -v f="${tarball_name}" '$2 == f {print $1}' "${tmp}/SHA256SUMS")"
  [ -n "${expected}" ] || die "${tarball_name} is not listed in the release SHA256SUMS — refusing to install an unlisted artifact"
  actual="$(sha256_of "${tmp}/${tarball_name}")"
  [ "${actual}" = "${expected}" ] || die "sha256 mismatch for ${tarball_name}: expected ${expected}, got ${actual}. Do not use this download — report it at https://github.com/${REPO}/issues"
  info "sha256 verified: ${actual}"
fi

# --- extract and verify the bundle's internal checksums ---------------------

tar -xzf "${tmp}/${tarball_name}" -C "${tmp}"
set -- "${tmp}"/*/
if [ "$#" -ne 1 ]; then
  die "unexpected archive layout: expected exactly one top-level directory, found $#, in ${tarball_name}"
fi
bundle_dir="${1%/}"
[ -f "${bundle_dir}/SHA256SUMS" ] || die "malformed bundle: SHA256SUMS missing from the archive"
verify_sums_in_dir "${bundle_dir}"
info "bundle internal SHA256SUMS verified"

# --- upgrade guard (FAR-672): enforced pre-upgrade dump + lock refusal ------
#
# Only when BOTH an existing populated data dir and a prior native install
# exist: that is the rerun-the-installer upgrade. A populated-but-first-time
# native install (e.g. a DR restore finished on a machine that has never had
# this installer) boots its data dir as-is; `current` is created fresh over
# an empty versions/ tree, so the swap below is inert in that case.

# newest pre-upgrade snapshot in the data dir (mtime order), for the
# --skip-backup path: the operator must KNOW which snapshot is authoritative.
newest_pre_upgrade_snapshot() {
  ls -1dt "${DATA_DIR}"/pre-upgrade-dump-* 2>/dev/null | head -n 1
}

UPGRADE_SNAPSHOT_PATH=""
if [ -f "${DATA_DIR}/state.json" ]; then
  if [ -f "${INSTALL_ROOT}/current/runtime/bin/python3" ]; then
    if [ "${SKIP_BACKUP}" -eq 1 ]; then
      printf 'WARNING: --skip-backup: installing WITHOUT creating a new pre-upgrade snapshot.\nYou (the operator) passed the loud explicit flag: confirm a verified snapshot already\nexists in %s and that the bundled stack is stopped (a stopped stack cannot be dumped;\nthe previous installer run printed its snapshot path and left it in the data dir).\n' "${DATA_DIR}" >&2
      newest_snapshot="$(newest_pre_upgrade_snapshot)"
      if [ -n "${newest_snapshot}" ]; then
        UPGRADE_SNAPSHOT_PATH="${newest_snapshot}"
        info "Newest pre-upgrade snapshot found in ${DATA_DIR}: ${UPGRADE_SNAPSHOT_PATH}"
        info "Restore later with: modulo restore ${UPGRADE_SNAPSHOT_PATH} --data-dir ${DATA_DIR} --yes"
      else
        info "No pre-upgrade snapshot directory found in ${DATA_DIR} — verify manually that a snapshot exists before continuing."
      fi
    else
      run_pre_upgrade_dump
    fi
  fi
  # The swap phase NEVER races a live launcher's data-dir lock — whenever a
  # bootstrapped data dir exists, whether or not this run produced a dump and
  # whether or not a prior native install existed. (The dump branch NEEDS
  # the running stack, so the refusal sits AFTER the dump, before the swap.)
  refuse_when_locked
  if [ -n "${UPGRADE_SNAPSHOT_PATH}" ]; then
    info "Upgrade snapshot requirement satisfied (snapshot: ${UPGRADE_SNAPSHOT_PATH})"
  else
    info "Upgrade snapshot requirement satisfied (--skip-backup run; the existing snapshot in ${DATA_DIR} must be verified by the operator)"
  fi
fi

# --- lay out the versioned install + `current` symlink ----------------------

mkdir -p "${INSTALL_ROOT}/versions"

target="${INSTALL_ROOT}/versions/${VERSION_NUM}"
staging="${INSTALL_ROOT}/versions/.staging-${VERSION_NUM}.$$"
mv "${bundle_dir}" "${staging}"

if [ -e "${target}" ]; then
  mv "${target}" "${target}.old.$$"
fi
mv "${staging}" "${target}"
[ ! -e "${target}.old.$$" ] || rm -rf "${target}.old.$$"
info "installed bundle to ${target}"

# Atomic-ish symlink swap: build a temp symlink, then rename it over `current`
# (GNU mv -T). The upgrade machinery (FAR-672) reuses this primitive.
ln -sfn "versions/${VERSION_NUM}" "${INSTALL_ROOT}/.current.new.$$"
mv -Tf "${INSTALL_ROOT}/.current.new.$$" "${INSTALL_ROOT}/current"
info "current -> $(readlink "${INSTALL_ROOT}/current")"

# --- neutralise a previous /usr/local/bin/modulo ----------------------------
#
# The pre-ADR-031 installer put a `modulo` binary in /usr/local/bin. Only
# touch it when a native-install marker matches (this machine is already
# managed by install.sh, or the user passed --force); otherwise refuse loudly:
# we will not replace a file we did not create.

legacy_shim="/usr/local/bin/modulo"
if [ -e "${legacy_shim}" ]; then
  if [ -f "${MARKER_FILE}" ] || [ "${FORCE}" -eq 1 ]; then
    backup="${legacy_shim}.pre-native-$(date +%Y%m%d%H%M%S)"
    mv "${legacy_shim}" "${backup}" || die "could not rename ${legacy_shim} — re-run with sudo, or remove it manually"
    info "neutralised previous ${legacy_shim} (renamed to ${backup})"
  else
    die "found an existing ${legacy_shim} that this installer did not create.\nRe-run with --force to rename it aside and let the native install take over, or remove it manually."
  fi
fi

# --- marker, shim, PATH -----------------------------------------------------

# Marker: presence marks this machine as having a native install managed by
# install.sh. It gates future neutralisation of /usr/local/bin/modulo.
cat > "${MARKER_FILE}" <<EOF
# Written by install.sh (FAR-670). The presence of this file marks this
# machine as having a Modulo native install managed by install.sh.
managed_by=install.sh
installed_version=${VERSION_NUM}
updated=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

mkdir -p "${BIN_DIR}"
shim="${BIN_DIR}/modulo"
cat > "${shim}" <<EOF
#!/bin/sh
# Modulo shim — generated by install.sh (FAR-670). Do not edit.
# Points at the stable \`current\` symlink so upgrades need no shim change.
exec "${INSTALL_ROOT}/current/launcher" "\$@"
EOF
chmod 755 "${shim}"

path_updated=0
for rc_file in "${HOME}/.bashrc" "${HOME}/.profile" "${HOME}/.zshrc"; do
  [ -f "${rc_file}" ] || continue
  if grep -Fqs "${BIN_DIR}" "${rc_file}"; then
    continue
  fi
  printf '\n# Added by Modulo install.sh (FAR-670)\nexport PATH="%s:$PATH"\n' "${BIN_DIR}" >> "${rc_file}"
  path_updated=1
  info "added ${BIN_DIR} to PATH in ${rc_file}"
done

case ":${PATH}:" in
  *":${BIN_DIR}:"*) bin_on_path=1 ;;
  *) bin_on_path=0 ;;
esac

info ""
info "Modulo bundle ${VERSION_NUM} installed."
info "  Bundle: ${INSTALL_ROOT}/current"
info "  Shim:   ${shim}"
if [ -n "${UPGRADE_SNAPSHOT_PATH}" ]; then
  info "  Pre-upgrade snapshot: ${UPGRADE_SNAPSHOT_PATH}"
fi
if [ "${bin_on_path}" -eq 0 ] && [ "${path_updated}" -eq 0 ]; then
  info ""
  info "NOTE: ${BIN_DIR} is not on your PATH and no shell rc file was updated."
  info "Add this to your shell rc:  export PATH=\"${BIN_DIR}:\$PATH\""
elif [ "${bin_on_path}" -eq 0 ]; then
  info ""
  info "Start a new shell (or: source ~/.bashrc) to pick up the updated PATH."
fi
info ""
info "The bundle ships a placeholder launcher until the real launcher lands (FAR-671); running 'modulo' will say so until then."
