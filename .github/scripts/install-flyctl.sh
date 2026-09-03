#!/usr/bin/env bash
# Verified flyctl installer (FAR-565, SonarQube S8482).
#
# Replaces the former `curl -fsSL https://fly.io/install.sh | sh` sites in
# .github/workflows/deploy.yml (execute-without-verification): the linux
# x86_64 release tarball is downloaded directly from the superfly/flyctl
# GitHub release and its sha256 is verified against the release's published
# checksums.txt BEFORE anything is extracted or executed. Any mismatch
# aborts non-zero without running the tarball's contents.
#
# Version bumps go through a PR: update FLYCTL_VERSION below (the checksum
# is read from the release's own checksums.txt, so no second edit is
# needed) and let CI exercise the download path. The latest release tag is
# listed at https://api.github.com/repos/superfly/flyctl/releases/latest.
set -euo pipefail

FLYCTL_VERSION="${FLYCTL_VERSION:-v0.4.97}"
FLYCTL_INSTALL="${FLYCTL_INSTALL:-$HOME/.fly}"

version_num="${FLYCTL_VERSION#v}"
if ! [[ "${FLYCTL_VERSION}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "::error::FLYCTL_VERSION must look like vX.Y.Z (got: '${FLYCTL_VERSION}')" >&2
  exit 1
fi

base_url="https://github.com/superfly/flyctl/releases/download/${FLYCTL_VERSION}"
tarball="flyctl_${version_num}_Linux_x86_64.tar.gz"
work_dir="$(mktemp -d)"
trap 'rm -rf "${work_dir}"' EXIT

echo "Downloading flyctl ${FLYCTL_VERSION} (linux x86_64)..."
curl -fsSL --retry 3 --connect-timeout 15 --max-time 300 -o "${work_dir}/${tarball}" "${base_url}/${tarball}"
curl -fsSL --retry 3 --connect-timeout 15 --max-time 300 -o "${work_dir}/checksums.txt" "${base_url}/flyctl_${version_num}_checksums.txt"

expected_sha="$(awk -v file="${tarball}" '$2 == file { print $1 }' "${work_dir}/checksums.txt")"
if [ -z "${expected_sha}" ]; then
  echo "::error::No checksum published for ${tarball} in ${FLYCTL_VERSION} checksums.txt - refusing to install." >&2
  exit 1
fi

# Verify BEFORE extracting or executing anything from the tarball.
echo "${expected_sha}  ${work_dir}/${tarball}" | sha256sum --check --strict -

# Official fly.io installer layout: $FLYCTL_INSTALL/bin/flyctl plus a $FLYCTL_INSTALL/bin/fly
# alias symlink. Consumers add $FLYCTL_INSTALL/bin to PATH and invoke both
# `fly` (deploy.yml) and `flyctl` interchangeably, so both names must exist.
mkdir -p "${FLYCTL_INSTALL}/bin"
tar -xzf "${work_dir}/${tarball}" -C "${work_dir}"
install -m 0755 "${work_dir}/flyctl" "${FLYCTL_INSTALL}/bin/flyctl"
ln -sf flyctl "${FLYCTL_INSTALL}/bin/fly"

"${FLYCTL_INSTALL}/bin/flyctl" version
"${FLYCTL_INSTALL}/bin/fly" version
echo "flyctl ${FLYCTL_VERSION} installed to ${FLYCTL_INSTALL}/bin/flyctl (alias: ${FLYCTL_INSTALL}/bin/fly)"
