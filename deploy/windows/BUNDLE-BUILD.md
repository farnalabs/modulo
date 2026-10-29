# Windows bundle build

`scripts/build-windows-bundle.ps1` assembles a Windows **bundle directory** -
the tree `deploy/windows/modulo.iss` expects as `BUNDLE_DIR`. It does not build
the installer, and it does not make the bundle a supported Windows runtime.

> **Current state: fail-closed.** The launcher refuses Windows until FAR-2229
> lands, so the manifest records `runtime_support.status = "unsupported"`. A full
> build also fails if PyInstaller is absent from the backend environment or if the
> packaged launcher cannot run `version`. `-ValidateOnly` is the supported validation
> mode, but it also fails closed when required toolchain or native inputs are missing;
> it is not expected to pass on every machine.

## Prerequisites

| Input | How it is supplied |
|---|---|
| PostgreSQL binaries (`initdb.exe`, `postgres.exe`, `pg_isready.exe`, `psql.exe`, `pg_dump.exe`) | `-PostgresDirectory <dir>` or `MODULO_WINDOWS_POSTGRES_DIR` |
| Redis binaries (`redis-server.exe`, `redis-cli.exe`) | `-RedisDirectory <dir>` or `MODULO_WINDOWS_REDIS_DIR` |
| `uv` | on `PATH` |
| pnpm | on `PATH`; prefer the version in `frontend/package.json` (`packageManager`), e.g. through `corepack` |
| backend Python + `jsonschema` | `uv sync` in `backend/` |
| PyInstaller | `uv add --group dev pyinstaller` in `backend/` |

The required native binary inventory and both bundle-relative layouts
(`postgres/bin`, `redis/bin`) come from `scripts/validate_windows_bundle_manifest.py`
(`--print-inventory`). Do not duplicate that list elsewhere; change the
validator.

**Tooling is not pinned and the build is not reproducible.** pnpm, Python and
PyInstaller are whatever the machine and the backend uv environment resolve; the
versions are *recorded* in the manifest for diagnostics only. The PostgreSQL and
Redis binaries are supplied by the operator - the build checks presence and
records hashes of the copied files, nothing more.

## Commands

```powershell
# Prerequisites only: no build, no writes. Reports every check with OK/FAIL/SKIP
# and an explicit "Checked:" / "Not checked:" summary.
pwsh scripts/build-windows-bundle.ps1 -ValidateOnly

# Validate an existing bundle's manifest against the schema and the files.
pwsh scripts/build-windows-bundle.ps1 -ValidateOnly -ManifestPath build/windows-bundle/modulo-runtime.json

# Full build (Windows only).
pwsh scripts/build-windows-bundle.ps1 -PostgresDirectory C:\pg -RedisDirectory C:\redis
```

## Output path guard

Before anything is written, created or deleted the output directory
(`-OutputDirectory`, default `build/windows-bundle`) is resolved and **refused**
unless it is:

- strictly beneath the repository root (not the repo root itself, not a sibling
  whose name merely starts the same way);
- not inside a `.git` directory;
- not a filesystem root, the user profile, or an ancestor of the user profile.

A target directory that already exists and contains a reparse point
(junction/symlink) is also refused, because a recursive delete would follow it.

Verify the guard without touching this repo by running the script from a
throwaway copy:

```powershell
$fake = Join-Path $env:TEMP 'bundle-guard-check'
New-Item -ItemType Directory -Force -Path "$fake/scripts", "$fake/frontend" | Out-Null
Copy-Item scripts/build-windows-bundle.ps1, scripts/windows-bundle-manifest.schema.json,
          scripts/validate_windows_bundle_manifest.py -Destination "$fake/scripts"
'{"version":"9.9.9"}' | Set-Content "$fake/frontend/package.json"
'keep me' | Set-Content "$fake/keep-me.txt"

# Refuses with "... is the repository root." and deletes nothing.
pwsh "$fake/scripts/build-windows-bundle.ps1" -OutputDirectory . -ValidateOnly

# `keep-me.txt` must still exist afterwards.
Test-Path "$fake/keep-me.txt"
```

The same refusal applies to `..`, `scripts/../../..` and `.git/hooks`. The
automated form of this check is
`backend/tests/unit/scripts/test_windows_bundle_manifest.py`.

## Bundle hygiene

PyInstaller writes its work directory, spec file and generated entrypoint into a
temp staging directory outside the bundle. Only `dist/modulo.exe` and the
intended payload (`frontend-dist/`, `postgres/`, `redis/`) are copied into the
bundle, and the emitted bundle is rejected if it still contains any of
`pyinstaller/`, `pyinstaller-work/`, `pyinstaller-spec/` or
`pyinstaller-entrypoint.py` (list owned by the validator, checked with
`--check-bundle`).

## Manifest

`modulo-runtime.json` at the bundle root records the version, architecture, the
launcher contract, the frontend dist path, per-binary SHA-256 and size, and the
tool versions that produced the build. It is validated against
`scripts/windows-bundle-manifest.schema.json` and against the emitted files
before the build is reported as complete. Validation never mutates the manifest.
