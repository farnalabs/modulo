#!/usr/bin/env bash
#
# build-bundle.sh — build, sign, and attest the native release bundle.
#
# Part of FAR-1869 (release slice 5). Called by release-unified.yml for
# cut/recut modes. Produces the bundle artefacts in release-artefacts/
# and the bundle-v* tag is pushed by the caller.
#
# Usage:
#   bash scripts/build-bundle.sh <VERSION_NUM> <RC_TAG>
#
# Example:
#   bash scripts/build-bundle.sh 0.1.0 v0.1.0-rc1

set -euo pipefail

VERSION_NUM="${1:-}"
RC_TAG="${2:-}"

if [ -z "$VERSION_NUM" ] || [ -z "$RC_TAG" ]; then
  echo "Usage: $0 <VERSION_NUM> <RC_TAG>"
  echo "Example: $0 0.1.0 v0.1.0-rc1"
  exit 1
fi

ARCH="amd64"
BUNDLE_ROOT="modulo-${VERSION_NUM}-linux-${ARCH}"
TARBALL_NAME="modulo-${VERSION_NUM}-linux-${ARCH}.tar.gz"
BUNDLE_TAG="bundle-v${VERSION_NUM}"

echo "Building bundle ${BUNDLE_TAG} for RC ${RC_TAG}"

# Source pinned versions
source scripts/bundle-versions.env

# ---------------------------------------------------------------------------
# Prepare build directory
# ---------------------------------------------------------------------------
mkdir -p build downloads release-artefacts
BUNDLE_DIR="build/${BUNDLE_ROOT}"
mkdir -p "${BUNDLE_DIR}/runtime" "${BUNDLE_DIR}/pg" "${BUNDLE_DIR}/redis" \
         "${BUNDLE_DIR}/backend" "${BUNDLE_DIR}/frontend-dist" "${BUNDLE_DIR}/pg/share"

# ---------------------------------------------------------------------------
# Download and verify python-build-standalone
# ---------------------------------------------------------------------------
echo "Downloading python-build-standalone..."
curl -fsSL --proto '=https' --retry 3 --retry-delay 2 \
  -o "downloads/${PBS_AMD64_TARBALL}" \
  "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_AMD64_TARBALL}"
echo "${PBS_CPYTHON_AMD64_SHA256}  ${PBS_AMD64_TARBALL}" | (cd downloads && sha256sum --check --strict -)

# Extract CPython runtime
tar -xzf "downloads/${PBS_AMD64_TARBALL}" -C "${BUNDLE_DIR}/runtime" --strip-components=1

# ---------------------------------------------------------------------------
# Download and verify PostgreSQL + Redis debs
# ---------------------------------------------------------------------------
echo "Downloading PostgreSQL and Redis debs..."
download_and_verify() {
  local url="$1" name="$2" sha="$3"
  curl -fsSL --proto '=https' --retry 3 --retry-delay 2 -o "downloads/${name}" "$url"
  echo "${sha}  ${name}" | (cd downloads && sha256sum --check --strict -)
}

download_and_verify "${PG_POOL_BASE_URL}/postgresql-16_${PG_DEB_VERSION}_${ARCH}.deb" "postgresql-16.deb" "${PG_SERVER_AMD64_SHA256}"
download_and_verify "${PG_POOL_BASE_URL}/postgresql-client-16_${PG_DEB_VERSION}_${ARCH}.deb" "postgresql-client-16.deb" "${PG_CLIENT_AMD64_SHA256}"
download_and_verify "${REDIS_POOL_BASE_URL}/redis-server_${REDIS_DEB_VERSION}_${ARCH}.deb" "redis-server.deb" "${REDIS_SERVER_AMD64_SHA256}"
download_and_verify "${REDIS_POOL_BASE_URL}/redis-tools_${REDIS_DEB_VERSION}_${ARCH}.deb" "redis-tools.deb" "${REDIS_TOOLS_AMD64_SHA256}"

# Verify deb Version fields
for spec in "postgresql-16.deb:${PG_DEB_VERSION}" "postgresql-client-16.deb:${PG_DEB_VERSION}" "redis-server.deb:${REDIS_DEB_VERSION}" "redis-tools.deb:${REDIS_DEB_VERSION}"; do
  deb="${spec%%:*}"
  want="${spec#*:}"
  got="$(dpkg-deb -f "downloads/${deb}" Version)"
  if [ "$got" != "$want" ]; then
    echo "::error::${deb} Version field is '${got}' but the pin is '${want}'"
    exit 1
  fi
done

# Extract PostgreSQL
dpkg-deb -x downloads/postgresql-16.deb debx/pg-server
dpkg-deb -x downloads/postgresql-client-16.deb debx/pg-client
cp -a debx/pg-server/usr/lib/postgresql/"${PG_MAJOR}"/. "${BUNDLE_DIR}/pg/"
cp -a debx/pg-client/usr/lib/postgresql/"${PG_MAJOR}"/. "${BUNDLE_DIR}/pg/"
cp -a debx/pg-server/usr/share/postgresql/"${PG_MAJOR}"/. "${BUNDLE_DIR}/pg/share/"
cp -a debx/pg-client/usr/share/postgresql/"${PG_MAJOR}"/. "${BUNDLE_DIR}/pg/share/"

# Extract Redis
dpkg-deb -x downloads/redis-server.deb debx/redis-server
dpkg-deb -x downloads/redis-tools.deb debx/redis-tools
cp -a debx/redis-server/usr/bin/redis-server "${BUNDLE_DIR}/redis/"
cp -a debx/redis-tools/usr/bin/. "${BUNDLE_DIR}/redis/"

# ---------------------------------------------------------------------------
# Stage backend into bundle
# ---------------------------------------------------------------------------
echo "Staging backend..."
cd backend
uv sync --frozen --no-dev --no-build
BUNDLE_BACKEND="../${BUNDLE_DIR}/backend"
mkdir -p "${BUNDLE_BACKEND}"
cp -a src "${BUNDLE_BACKEND}/src"
cp pyproject.toml uv.lock "${BUNDLE_BACKEND}/"
cp -a .venv "${BUNDLE_BACKEND}/.venv"
cd ..

# ---------------------------------------------------------------------------
# Build frontend and stage into bundle
# ---------------------------------------------------------------------------
echo "Building frontend..."
cd frontend
pnpm install --frozen-lockfile --ignore-scripts
pnpm run build
BUNDLE_FRONTEND="../${BUNDLE_DIR}/frontend-dist"
mkdir -p "${BUNDLE_FRONTEND}"
cp -a dist/. "${BUNDLE_FRONTEND}/"
cd ..

# ---------------------------------------------------------------------------
# Launcher placeholder (FAR-671)
# ---------------------------------------------------------------------------
cat > "${BUNDLE_DIR}/launcher" <<'EOF'
#!/bin/sh
# PLACEHOLDER -- the Modulo launcher is implemented in FAR-671.
# The bundle layout is complete; the launcher wires it together.
echo "Modulo bundle layout is present, but the launcher is not implemented yet (tracked as FAR-671)." >&2
exit 1
EOF
chmod 755 "${BUNDLE_DIR}/launcher"

# ---------------------------------------------------------------------------
# VERSION file
# ---------------------------------------------------------------------------
echo "${BUNDLE_TAG}" > "${BUNDLE_DIR}/VERSION"

# ---------------------------------------------------------------------------
# Generate SHA256SUMS over every shipped file
# ---------------------------------------------------------------------------
echo "Generating SHA256SUMS..."
cd "${BUNDLE_DIR}"
find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > SHA256SUMS
wc -l SHA256SUMS
cd ../..

# ---------------------------------------------------------------------------
# Create tar.gz and its release checksum
# ---------------------------------------------------------------------------
echo "Creating tarball..."
cd build
tar -czf "${TARBALL_NAME}" "${BUNDLE_ROOT}"
sha256sum "${TARBALL_NAME}" > SHA256SUMS
cat SHA256SUMS
ls -lh "${TARBALL_NAME}"
cd ..

# ---------------------------------------------------------------------------
# Generate and sign the SIGNED release manifest
# ---------------------------------------------------------------------------
echo "Generating and signing release manifest..."
if [ -z "${BUNDLE_MANIFEST_SIGNING_KEY:-}" ]; then
  echo "::error::BUNDLE_MANIFEST_SIGNING_KEY is NOT set - refusing to publish an UNSIGNED release manifest."
  echo "Provisioning: generate an ed25519 keypair; add the private key's hex as the repo secret BUNDLE_MANIFEST_SIGNING_KEY; paste the public half into backend/src/modulo/launcher/manifest.py (_TRUST_ROWS) and scripts/install.sh (TRUST_KEY_CURRENT_B64/NEXT_B64)."
  exit 1
fi

cd backend
uv run --no-sync --no-build python - <<'PYEOF'
import hashlib, json, os, sys

sys.path.insert(0, "src")
from modulo.launcher import manifest as manifest_module

version = os.environ["VERSION_NUM"]
arch = os.environ["ARCH"]
tarball_name = os.environ["TARBALL_NAME"]
bundle_dirname = os.environ["BUNDLE_ROOT"]
build_root = os.path.join("..", "build")
pg_version = os.environ.get("PG_DEB_VERSION", "unknown").split("+")[0]
redis_version = os.environ.get("REDIS_DEB_VERSION", "unknown").split("+")[0]

def digest_for(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()

artifacts = [{"name": tarball_name, "sha256": digest_for(os.path.join(build_root, tarball_name)), "version": version}]
for shipped, component in (("pg/bin/postgres", pg_version), ("pg/bin/pg_dump", pg_version), ("redis/redis-server", redis_version)):
    shipped_path = os.path.join(build_root, bundle_dirname, shipped)
    if os.path.isfile(shipped_path):
        artifacts.append({"name": shipped, "sha256": digest_for(shipped_path), "version": component})

manifest = manifest_module.build_release_manifest(
    release=f"bundle-v{version}",
    platform=f"linux-{arch}",
    generated_at=os.environ.get("GITHUB_SHA", ""),
    components={"postgres": pg_version, "redis": redis_version, "python": "3.12"},
    artifacts=artifacts,
)
payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
signature = manifest_module.sign_release_bytes(
    payload, key_id=manifest_module._KEY_ID_CURRENT, private_key_hex=os.environ["MANIFEST_SIGNING_KEY"]
)
with open(os.path.join(build_root, "RELEASE_MANIFEST.json"), "wb") as handle:
    handle.write(payload)
with open(os.path.join(build_root, "RELEASE_MANIFEST.json.sig"), "w", encoding="utf-8") as handle:
    handle.write(json.dumps(signature))
PYEOF
cd ..

# ---------------------------------------------------------------------------
# Generate SBOM for the bundle
# ---------------------------------------------------------------------------
echo "Generating SBOM for bundle..."
# --no-sync/--no-build + an exact pin (S8541/S8544): no dependency build
# scripts run and the one dependency is a verified version.
uv run --no-project --no-sync --no-build --with pyyaml==6.0.3 python backend/scripts/generate-sbom.py --output "build/SBOM.json"

# ---------------------------------------------------------------------------
# Copy OpenAPI spec
# ---------------------------------------------------------------------------
echo "Copying OpenAPI spec..."
# Dump OpenAPI from backend
cd backend
uv run --no-project --no-sync --no-build python - <<'PYEOF'
import os, json, sys
sys.path.insert(0, "src")
os.environ.setdefault("MODULO_DB", "sqlite")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./modulo.db")
os.environ.setdefault("SECRET_KEY", "dev-secret-key-for-openapi-generation")
os.environ.setdefault("FERNET_KEY", "ZmVybmV0LWtleS1mb3ItbW9kdWxvLWRldg==")
from modulo.api.main import app
import uvicorn
# Just generate the openapi.json
openapi_json = app.openapi()
with open("../build/openapi.json", "w") as f:
    json.dump(openapi_json, f, indent=2)
print("OpenAPI spec written to ../build/openapi.json")
PYEOF
cd ..

# ---------------------------------------------------------------------------
# Copy artefacts to release-artefacts/
# ---------------------------------------------------------------------------
echo "Staging artefacts to release-artefacts/..."
cp "build/${TARBALL_NAME}" release-artefacts/
cp "build/SHA256SUMS" release-artefacts/
cp "build/RELEASE_MANIFEST.json" release-artefacts/
cp "build/RELEASE_MANIFEST.json.sig" release-artefacts/
cp "build/SBOM.json" release-artefacts/
cp "build/openapi.json" release-artefacts/

# ---------------------------------------------------------------------------
# Cosign/SLSA attestations for the bundle artefacts
# ---------------------------------------------------------------------------
echo "Generating cosign/SLSA attestations..."
# Note: cosign sign-attest requires the cosign binary and keyless signing
# For now, we use GitHub Actions' built-in attest-build-provenance
# which is invoked in the workflow's attest-artefacts job.
# This script prepares the artefacts; the workflow handles attestation.

echo "Bundle build complete. Artefacts in release-artefacts/:"
ls -la release-artefacts/
