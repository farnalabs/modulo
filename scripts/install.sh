#!/usr/bin/env bash
#
# Modulo native installer (Linux) — single-install distribution, P1a.
# Spec: ADR 031 (single-install native distribution); this ticket: FAR-670.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/farnalabs/modulo/main/scripts/install.sh | bash -s -- [options]
#   ./install.sh [--force] [--from-file <tarball>]
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
  --help                   Show this help.

Environment overrides:
  VERSION=<tag>            Install a specific bundle tag (default: latest release).
  MODULO_INSTALL_ROOT      Install root (default: ~/.local/opt/modulo).
  MODULO_BIN_DIR           Shim directory (default: ~/.local/bin).

When piping from curl, pass options like:  curl -fsSL <url> | bash -s -- --force
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

resolve_latest_tag() {
  # Follow the /releases/latest redirect (no API token, no API rate limit):
  # https://github.com/farnalabs/modulo/releases/latest -> .../tag/<tag>
  local effective
  effective="$(curl -fsSI --proto '=https' --tlsv1.2 --connect-timeout 15 -o /dev/null -w '%{url_effective}' "${RELEASES_LATEST_URL}")" || {
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
