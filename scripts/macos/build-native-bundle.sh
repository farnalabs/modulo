#!/usr/bin/env bash
# Build the genuine arm64 macOS bundle. Every input is downloaded from an
# upstream source and verified before it is compiled or copied into the app.
#
# The Python runtime is the pinned python-build-standalone CPython distribution,
# not a CPython source build. It is trusted here as a native arm64 runtime input;
# the workflow proves its architecture, version, and linkage before packaging.
set -euo pipefail

VERSION=${1:?version required}
BUILD_ROOT=${2:?build root required}
ROOT_DIR="$(cd -- "$(dirname -- "$0")/../.." && pwd)"
APP_NAME="Modulo-${VERSION}.app"
APP_ROOT="${BUILD_ROOT}/${APP_NAME}"
BUNDLE_ROOT="${APP_ROOT}/Contents/Resources/bundle"
DOWNLOADS="${BUILD_ROOT}/downloads"
mkdir -p "${DOWNLOADS}" "${BUNDLE_ROOT}" "${APP_ROOT}/Contents/MacOS" "${APP_ROOT}/Contents/Resources"

PBS_RELEASE=20260901
PBS_VERSION=3.12.14
PBS_TARBALL="cpython-${PBS_VERSION}+${PBS_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
PBS_SHA256="3ee3ee547cedfeb7c2b16b2b7156039f7b470bb8f857e226fd3d2eb11db83c76"
PG_VERSION=16.15
PG_SHA256="4f200ca23dfb120ff9838f13ce06014aad1d3c432d16ee9f93ab2000c0eeef7b"
REDIS_VERSION=8.0.2
REDIS_SHA256="caf3c0069f06fc84c5153bd2a348b204c578de80490c73857bee01d9b5d7401f"

# A placeholder digest is a deliberate build-stop, never a release path.
case "${PBS_SHA256}" in
  REPLACE_*|'' ) echo "::error::python-build-standalone macOS arm64 SHA-256 has not been provisioned" >&2; exit 1 ;;
  * ) ;;
esac

download_verify() {
  local url=$1 output=$2 expected=$3
  curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --retry-delay 2 --connect-timeout 20 -o "${output}" "${url}"
  printf '%s  %s\n' "${expected}" "${output}" | shasum -a 256 --check --strict
}

download_verify "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_TARBALL}" "${DOWNLOADS}/${PBS_TARBALL}" "${PBS_SHA256}"
download_verify "https://ftp.postgresql.org/pub/source/v${PG_VERSION}/postgresql-${PG_VERSION}.tar.gz" "${DOWNLOADS}/postgresql-${PG_VERSION}.tar.gz" "${PG_SHA256}"
download_verify "https://github.com/redis/redis/archive/refs/tags/${REDIS_VERSION}.tar.gz" "${DOWNLOADS}/redis-${REDIS_VERSION}.tar.gz" "${REDIS_SHA256}"

tar -xzf "${DOWNLOADS}/${PBS_TARBALL}" -C "${BUNDLE_ROOT}"
mv "${BUNDLE_ROOT}/python" "${BUNDLE_ROOT}/runtime"
# Verify the trusted runtime input before it becomes part of the release bundle.
file "${BUNDLE_ROOT}/runtime/bin/python3" | grep -E 'Mach-O 64-bit executable arm64' >/dev/null
otool -L "${BUNDLE_ROOT}/runtime/bin/python3" | grep -q '/usr/lib/libSystem.B.dylib'
"${BUNDLE_ROOT}/runtime/bin/python3" --version | grep -Fqx "Python ${PBS_VERSION}"

mkdir -p "${BUILD_ROOT}/src"
tar -xzf "${DOWNLOADS}/postgresql-${PG_VERSION}.tar.gz" -C "${BUILD_ROOT}/src"
PG_SRC="${BUILD_ROOT}/src/postgresql-${PG_VERSION}"
mkdir -p "${BUILD_ROOT}/pg-build" "${BUNDLE_ROOT}/pg"
(
  cd "${BUILD_ROOT}/pg-build"
  "${PG_SRC}/configure" --prefix="${BUNDLE_ROOT}/pg" --without-readline --without-zlib --without-icu
  make -j"$(sysctl -n hw.ncpu)"
  make install
)

tar -xzf "${DOWNLOADS}/redis-${REDIS_VERSION}.tar.gz" -C "${BUILD_ROOT}/src"
REDIS_SRC="${BUILD_ROOT}/src/redis-${REDIS_VERSION}"
(
  cd "${REDIS_SRC}"
  make -j"$(sysctl -n hw.ncpu)" BUILD_TLS=no
)
mkdir -p "${BUNDLE_ROOT}/redis" "${BUNDLE_ROOT}/bin"
cp "${REDIS_SRC}/src/redis-server" "${BUNDLE_ROOT}/redis/redis-server"
cp "${REDIS_SRC}/src/redis-cli" "${BUNDLE_ROOT}/redis/redis-cli"
for binary in initdb postgres pg_isready pg_dump psql; do
  ln -s "../pg/bin/${binary}" "${BUNDLE_ROOT}/bin/${binary}"
done
for binary in redis-server redis-cli; do
  ln -s "../redis/${binary}" "${BUNDLE_ROOT}/bin/${binary}"
done

# Build and copy the real Vue/Vite distribution. The macOS workflow pins Node
# 22 and pnpm 11 before invoking this script; never ship an API-only bundle.
(
  cd "${ROOT_DIR}/frontend"
  pnpm install --frozen-lockfile --ignore-scripts
  pnpm run build
)
mkdir -p "${BUNDLE_ROOT}/frontend-dist"
cp -a "${ROOT_DIR}/frontend/dist/." "${BUNDLE_ROOT}/frontend-dist/"

mkdir -p "${BUNDLE_ROOT}/backend"
cp -a "${ROOT_DIR}/backend/src" "${BUNDLE_ROOT}/backend/src"
cp "${ROOT_DIR}/backend/pyproject.toml" "${ROOT_DIR}/backend/uv.lock" "${BUNDLE_ROOT}/backend/"
# Build the venv against the bundled interpreter with copies, rather than copying
# the developer venv. This keeps the signed app relocatable: venv entry points
# and interpreter links resolve inside the app instead of to build-time paths.
"${BUNDLE_ROOT}/runtime/bin/python3.12" -m venv --copies "${BUNDLE_ROOT}/backend/.venv"
uv sync --frozen --no-dev --no-install-project --project "${BUNDLE_ROOT}/backend" --python "${BUNDLE_ROOT}/backend/.venv/bin/python"

# Assert the installed bundle's actual launcher module emits PostgreSQL's
# value-bearing long option as one argv element. A shell command string would
# be the wrong contract here: the supervisor passes an argv list directly.
PYTHONPATH="${BUNDLE_ROOT}/backend/src:${BUNDLE_ROOT}/backend/.venv/lib/python3.12/site-packages" \
  "${BUNDLE_ROOT}/backend/.venv/bin/python" - "${BUNDLE_ROOT}" <<'PY'
from pathlib import Path
import sys

from modulo.launcher.initdb import postgres_server_argv

bundle = Path(sys.argv[1])
pgdata = bundle / "runtime-contract-pgdata"
argv = postgres_server_argv(pgdata, "127.0.0.1", 18000, bundle / "bin")
assert argv[1] == f"--pgdata={pgdata}", argv
assert argv[2:] == ["--host", "127.0.0.1", "--port", "18000"], argv
print(f"bundled postgres argv contract: {argv}")
PY

assert_no_escaping_symlinks() {
  local root="$1" link target resolved canonical_root
  canonical_root="$(cd "$root" && pwd -P)"
  while IFS= read -r -d '' link; do
    target="$(readlink "$link")"
    if [[ "$target" = /* ]]; then
      resolved="$target"
    else
      resolved="$(dirname "$link")/$target"
    fi
    resolved="$(python3 - "$resolved" <<'PY'
import os
import sys
print(os.path.realpath(sys.argv[1]))
PY
)"
    case "$resolved/" in
      "$canonical_root/"*) ;;
      *)
        printf '::error::symlink escapes app bundle: link=%s target=%s resolved=%s\n' "$link" "$target" "$resolved" >&2
        return 1
        ;;
    esac
  done < <(find "${root}/Contents" -type l -print0)
}

assert_no_escaping_symlinks "$APP_ROOT"

cat > "${BUILD_ROOT}/launcher.c" <<'EOF'
#include <libproc.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    uint32_t size = 0;
    if (_NSGetExecutablePath(NULL, &size) == 0 || size == 0) return 70;
    char *path = calloc(size + 1, 1);
    if (!path || _NSGetExecutablePath(path, &size) != 0) return 70;
    char *contents = strrchr(path, '/');
    if (!contents) return 70;
    *contents = '\0';
    char *macos = strrchr(path, '/');
    if (!macos) return 70;
    *macos = '\0';
    char bundle[4096];
    snprintf(bundle, sizeof(bundle), "%s/Resources/bundle", path);
    char python[4096];
    snprintf(python, sizeof(python), "%s/runtime/bin/python3", bundle);
    char src[4096];
    snprintf(src, sizeof(src), "%s/backend/src", bundle);
    char site[4096];
    snprintf(site, sizeof(site), "%s/backend/.venv/lib/python3.12/site-packages", bundle);
    setenv("PYTHONPATH", src, 1);
    char path_env[8192];
    snprintf(path_env, sizeof(path_env), "%s/runtime/bin:%s/pg/bin:%s/redis:%s", bundle, bundle, bundle, getenv("PATH") ? getenv("PATH") : "");
    setenv("PATH", path_env, 1);
    char bin_dir[4096];
    snprintf(bin_dir, sizeof(bin_dir), "%s/bin", bundle);
    setenv("MODULO_BUNDLED_BIN_DIR", bin_dir, 1);
    char frontend_dist[4096];
    snprintf(frontend_dist, sizeof(frontend_dist), "%s/frontend-dist", bundle);
    setenv("MODULO_SERVE_SPA", "1", 1);
    setenv("MODULO_FRONTEND_DIST", frontend_dist, 1);
    char final_pythonpath[12288];
    snprintf(final_pythonpath, sizeof(final_pythonpath), "%s:%s", src, site);
    setenv("PYTHONPATH", final_pythonpath, 1);
    char **child = calloc((size_t)argc + 3, sizeof(char *));
    if (!child) return 70;
    child[0] = python;
    child[1] = "-c";
    child[2] = "from modulo.cli.main import cli; cli()";
    for (int i = 1; i < argc; ++i) child[i + 2] = argv[i];
    child[argc + 2] = NULL;
    execv(python, child);
    perror("Modulo launcher execv");
    return 127;
}
EOF
clang -Wall -Wextra -Werror -o "${APP_ROOT}/Contents/MacOS/modulo" "${BUILD_ROOT}/launcher.c"
chmod 755 "${APP_ROOT}/Contents/MacOS/modulo"

# Single source of truth for the app-bundle metadata: the shared helper in
# scripts/ owns the Info.plist contents (the gate0 filter watches it too).
(
  cd "${APP_ROOT}/Contents"
  "${ROOT_DIR}/scripts/create-macos-info-plist.sh" "${VERSION}" "Info.plist"
)

echo "${VERSION}" > "${BUNDLE_ROOT}/VERSION"
file "${APP_ROOT}/Contents/MacOS/modulo"
"${BUNDLE_ROOT}/pg/bin/postgres" --version
"${BUNDLE_ROOT}/redis/redis-server" --version
"${APP_ROOT}/Contents/MacOS/modulo" --help >/dev/null
