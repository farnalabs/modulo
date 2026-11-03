# Windows native bundle: build and installer compile

`deploy/windows/modulo.iss` compiles a Windows installer around a **native
Windows bundle directory**. The installer is fail-closed: it refuses to compile
unless the bundle's required inputs are present, so a partial artifact or a
relabelled Linux build can never be published as a Windows installer.

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
   `backend/src`, `frontend-dist` and `bin/modulo.exe`, then adds `redis/` and
   `pg/bin/`.
4. Verifies the staged binaries, writes `manifest.json`, and uploads the
   payload archive as the `windows-native-artifacts` artifact.

`scripts/create-windows-launcher.ps1` never fabricates a launcher. It only
validates an existing native `modulo.exe` (`-RequireBinary` makes an absent
binary a hard failure and executes it to check the commands the installer
depends on).

Not yet reconciled: the installer contract below expects `modulo.exe` and
`modulo-runtime.json` at the root of `BUNDLE_DIR`, but the staging function
places the launcher at `bin/modulo.exe` and nothing in the repository writes
`modulo-runtime.json`. Producing a bundle directory that satisfies the
installer is therefore still a separate step, which is why the release workflow
(`.github/workflows/windows-installer.yml`, job `build`) refuses to package.

## Installer bundle contract

`modulo.iss` requires, at compile time:

| Bundle path | Input |
|---|---|
| `<BUNDLE_DIR>\modulo.exe` | Native Windows Modulo launcher |
| `<BUNDLE_DIR>\modulo-runtime.json` | Native runtime marker |

Everything else under `BUNDLE_DIR` is copied into the install directory.
Required defines: `VERSION_NUM`, `BUNDLE_ARCH` (only `amd64` is accepted) and
`BUNDLE_DIR`; `MODULO_URL` is optional.

## Compiling

```powershell
iscc /DVERSION_NUM=0.4.0 /DBUNDLE_ARCH=amd64 /DBUNDLE_DIR=C:\bundle deploy\windows\modulo.iss
```

## CI gate: `.iss` changes are compiled, not read

`.github/workflows/windows-installer.yml` runs on every release invocation and on
pull requests that touch `deploy/windows/**`, `scripts/create-windows-launcher.ps1`,
`scripts/test-windows-installer.ps1` or the workflow itself. All proofs live in
`scripts/test-windows-installer.ps1`:

- Job `compile-check` (Windows runner, Inno Setup 6.2.2 via Chocolatey) runs
  `./scripts/test-windows-installer.ps1 -RequireIscc`. It compiles `modulo.iss`
  against fixture bundles: a bundle missing `modulo.exe` **must fail**, a bundle
  missing `modulo-runtime.json` **must fail**, and a complete bundle **must
  compile** and produce an installer. On a non-zero exit ISCC's own output is
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

This is a compile-level gate only: the fixture bundle is a placeholder, so it
proves the installer script is valid and fail-closed, not that a runnable bundle
exists.
