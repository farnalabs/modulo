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

FLYCTL_VERSION="${FLYCTL_VERSION:-v0.5.1}"
FLYCTL_INSTALL="${FLYCTL_INSTALL:-$HOME/.fly}"

version_num="${FLYCTL_VERSION#v}"
if ! [[ "${FLYCTL_VERSION}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "::error::FLYCTL_VERSION must look like vX.Y.Z (got: '${FLYCTL_VERSION}')" >&2
  exit 1
fi

base_url="https://github.com/superfly/flyctl/releases/download/${FLYCTL_VERSION}"
api_base="https://api.github.com/repos/superfly/flyctl"
tarball="flyctl_${version_num}_Linux_x86_64.tar.gz"
checksums_file="flyctl_${version_num}_checksums.txt"
work_dir="$(mktemp -d)"
trap 'rm -rf "${work_dir}"' EXIT

curl_flags=(-fsSL --proto '=https' --tlsv1.2 --connect-timeout 15)

# The github.com/.../releases/download path and the release-asset API are
# served by DIFFERENT CDN hosts. On 2026-11-08 part of the releases/download
# CDN returned persistent 504s for 40+ minutes - long enough to outlast every
# retry - while api.github.com/repos/.../releases/assets/<id> served the same
# bytes from release-assets.githubusercontent.com with 200. So try the direct
# release path briefly, then fall back to the API asset endpoint. Both paths
# end at the same checksum verification below, so the fallback does not
# weaken the integrity check.
resolve_asset_id() {
  local asset="$1" release_json
  release_json="$(curl "${curl_flags[@]}" --retry 3 --retry-delay 2 --max-time 60 \
    -H 'Accept: application/vnd.github+json' \
    "${api_base}/releases/tags/${FLYCTL_VERSION}")"
  printf '%s' "${release_json}" | python3 -c '
import json
import sys

name = sys.argv[1]
release = json.load(sys.stdin)
for asset in release.get("assets", []):
    if asset.get("name") == name:
        print(asset["id"])
        break
else:
    sys.exit(1)
' "${asset}"
}

download_release_asset() {
  local asset="$1" dest="$2" asset_id
  if curl "${curl_flags[@]}" --retry 2 --retry-delay 2 --retry-max-time 30 \
    --max-time 300 -o "${dest}" "${base_url}/${asset}"; then
    return 0
  fi
  echo "::warning::Could not download ${asset} from ${base_url}; retrying via the GitHub release API asset endpoint." >&2
  asset_id="$(resolve_asset_id "${asset}")" || {
    echo "::error::Could not resolve the '${asset}' asset id for ${FLYCTL_VERSION} via the GitHub API." >&2
    return 1
  }
  # Accept: application/octet-stream makes the asset endpoint return the
  # artefact (a 302 to the signed release-assets URL) rather than its JSON
  # metadata.
  curl "${curl_flags[@]}" --retry 6 --retry-delay 5 --retry-max-time 180 \
    --max-time 300 -H 'Accept: application/octet-stream' \
    -o "${dest}" "${api_base}/releases/assets/${asset_id}"
}

echo "Downloading flyctl ${FLYCTL_VERSION} (linux x86_64)..."
download_release_asset "${tarball}" "${work_dir}/${tarball}"
download_release_asset "${checksums_file}" "${work_dir}/checksums.txt"

expected_sha="$(awk -v file="${tarball}" '$2 == file { print $1 }' "${work_dir}/checksums.txt")"
if [[ -z "${expected_sha}" ]]; then
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
