# Windows native bundle: build and installer compile

`deploy/windows/modulo.iss` compiles a Windows installer around a **native
Windows bundle directory**. The installer is fail-closed: it refuses to compile
unless the bundle's required inputs are present, so a partial artifact or a
relabelled Linux build can never be published as a Windows installer. Windows
native packaging is Beta scope: the Alpha release path does not build, sign or
publish it.

## How the bundle directory is built

`.github/workflows/windows-artifacts.yml` (push to `main` on the pinned inputs,
weekly schedule, or `workflow_dispatch`) builds and stages the payload on a
Windows runner:

1. Downloads and sha256-verifies the third-party archives pinned in
   `scripts/bundle-versions-windows.env` (Valkey, python-build-standalone
   CPython, PostgreSQL).
2. Builds the frontend (`frontend/dist`) and compiles the native launcher from
   `scripts/windows/modulo-launcher.c` with `scripts/windows/build-launcher.ps1`
   (MSVC by default, `-Compiler clang-cl` also supported).
3. Stages the layout with `Invoke-WindowsBundleStaging` (defined in
   `scripts/windows/build-launcher.ps1`): `runtime/python`, `runtime/bin`,
   `backend/src`, `frontend-dist` and `bin/modulo.exe`, writes the root
   `modulo-runtime.json` marker, then adds `redis/` and `pg/bin/`.
4. Verifies the staged binaries, writes `manifest.json`, and uploads the
   payload archive as the `windows-native-artifacts` artifact.

`scripts/create-windows-launcher.ps1` never fabricates a launcher. It only
validates an existing native `modulo.exe` (`-RequireBinary` makes an absent
binary a hard failure and executes it to check the commands the installer
depends on).

The staged directory satisfies the installer contract below directly: the
launcher is at `bin/modulo.exe` and `modulo-runtime.json` is at the root. The
launcher resolves the bundle root as the parent of its own directory
(`runtime/`, `backend/`, `pg/` and `redis/` are siblings of `bin/`), which is why
the installer places it at `{app}\bin\modulo.exe` and not at the install root.
The marker holds `contract` (`native-windows-v1`), `arch` and, when the caller
passes `-Version`, `version`.

## Release packaging path

`windows-artifacts.yml` is also a reusable workflow (`workflow_call` with a
`ref` input), so a release builds the payload from the RC tag rather than from
whatever main happened to produce.

`.github/workflows/release-unified.yml` calls it as `windows-native-artifacts`
(cut/recut, gated on `include_windows`, which defaults to true because Windows
is Alpha scope), then calls `.github/workflows/windows-installer.yml` with that
job as a dependency.

Inside `windows-installer.yml`:

1. Job `build` downloads the `windows-native-artifacts` payload from the same
   run, extracts it, and re-stages it with
   `Invoke-WindowsBundleStaging -Version <release version>` so
   `modulo-runtime.json` records the release version. `pg/` and `redis/` are
   copied over from the payload. It then compiles `modulo.iss` with
   `ISCC /DVERSION_NUM=<version> /DBUNDLE_ARCH=amd64 /DBUNDLE_DIR=<staged>`
   and uploads `windows-installer-unsigned-<version>`.
2. Job `sign` downloads that artefact and signs it with the existing Azure
   Trusted Signing flow (`azure/login` OIDC with `AZURE_CLIENT_ID`,
   `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, then
   `Azure/trusted-signing-action` against the `farnasigning` account and the
   `farna-public-trust` certificate profile), then uploads
   `windows-installer-signed-<version>`. No secret value is ever printed.
3. Job `attest` downloads the signed installer, verifies the file is present and
   non-empty, attests provenance with `actions/attest-build-provenance`, and
   uploads the public `windows-installer-<version>` artefact the release
   manifest consumes.

Dry-run still skips the payload download, compile-against-real-payload, signing
and attestation. It runs `scripts/test-windows-installer.ps1 -RequireIscc`, so
the dry-run proves the installer contract compiles without publishing anything.

`final-release-manifest` depends on `windows-installer` when
`include_windows` is true, so the `windows-installer-<version>` artefact exists
before the manifest downloads and hashes it.

## Installer bundle contract

`modulo.iss` requires, at compile time:

| Bundle path | Input |
|---|---|
| `<BUNDLE_DIR>\bin\modulo.exe` | Native Windows Modulo launcher |
| `<BUNDLE_DIR>\modulo-runtime.json` | Native runtime marker |

Everything else under `BUNDLE_DIR` is copied into the install directory.
Required defines: `VERSION_NUM`, `BUNDLE_ARCH` (only `amd64` is accepted) and
`BUNDLE_DIR`; `MODULO_URL` is optional.

## Compiling

```powershell
iscc /DVERSION_NUM=0.4.0 /DBUNDLE_ARCH=amd64 /DBUNDLE_DIR=C:\bundle deploy\windows\modulo.iss
```

## CI gate: `.iss` changes are compiled, not read

`.github/workflows/windows-installer.yml` runs its compile and redaction gates on
release invocations through `workflow_call` and on pull requests that touch
`deploy/windows/**`, `scripts/create-windows-launcher.ps1`,
`scripts/windows/build-launcher.ps1`, `scripts/test-windows-installer.ps1` or
`.github/workflows/windows-installer.yml` itself. The unified release pipeline
invokes it only when its Beta Windows input (`include_windows`) is enabled, so
Windows native packaging does not run on the Alpha release path. All proofs live
in `scripts/test-windows-installer.ps1`:

- Job `compile-check` (Windows runner, Inno Setup 6.2.2 via Chocolatey) runs
  `./scripts/test-windows-installer.ps1 -RequireIscc`. It compiles `modulo.iss`
  against fixture bundles: a bundle missing `bin\modulo.exe` (or with the
  launcher at the bundle root) **must fail**, a bundle missing
  `modulo-runtime.json` **must fail**, and a complete bundle **must compile** and
  produce an installer. It then compiles against a bundle produced by the real
  `Invoke-WindowsBundleStaging` (placeholder payload, no signing secret), so the
  contract is proven against the producer too. On a non-zero exit ISCC's own output is
  printed and written to `iscc-<case>.log`, uploaded as the `iscc-output`
  artifact.
- Job `redaction-unit` (Ubuntu, Free Pascal) runs the script with `-RequireFpc`:
  it compiles and executes the extracted `Redact()` fixtures and checks that the
  whole `[Code]` section compiles against Inno support stubs.

`-RequireIscc` and `-RequireFpc` make a missing compiler a failure instead of a
skip: each job owns its proof, so it cannot pass without actually compiling. Run
without them (`pwsh scripts/test-windows-installer.ps1`), the script runs
whichever proofs the host supports and warns that the ISCC compile was not
verified if Inno Setup is not found.

This is a compile-level gate only: the payload is a placeholder, so it proves
the installer script and the staging layout agree and are fail-closed, not that
a runnable bundle exists.
