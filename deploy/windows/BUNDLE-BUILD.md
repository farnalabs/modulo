# Windows native bundle build

`deploy/windows/modulo.iss` compiles a Windows installer around a **provisioned
native Windows bundle**. This repository does not manufacture that bundle: the
installer refuses to compile unless the bundle's required inputs are present, so
a partial artifact or a relabelled Linux build can never be published as a
Windows installer.

## Bundle contract

The installer requires, at compile time:

| Bundle path | Input |
|---|---|
| `<BUNDLE_DIR>\modulo.exe` | Native Windows Modulo launcher |
| `<BUNDLE_DIR>\modulo-runtime.json` | Native runtime marker |

`scripts/create-windows-launcher.ps1` deliberately refuses to fabricate a
launcher; the executable must come from the native Windows runtime build.

## Compiling

```powershell
iscc /DVERSION_NUM=0.4.0 /DBUNDLE_ARCH=amd64 /DBUNDLE_DIR=C:\bundle deploy\windows\modulo.iss
```

## CI gate: `.iss` changes are compiled, not read

Until this gate existed, `deploy/windows/modulo.iss` was reviewed by humans who
cannot run the Inno Setup compiler in their heads. Five review rounds each found
a genuine, compiler-level defect afterwards - most recently service Parameters
using backslash-escaped quotes (`\"`), which the Inno Setup directive parser
rejects (only doubled quotes, `""`, are valid). `scripts/test-windows-installer.ps1`
already encoded the compile checks, but no CI job invoked it, so the defect
survived. The compiler is now the reviewer.

Every change under `deploy/windows/**` (or to the gate script or its workflow)
runs `.github/workflows/windows-installer-compile.yml` on a Windows runner. It
provisions a pinned, sha256-verified Inno Setup, then:

1. compiles against an empty bundle - **must fail** (the fail-closed presence contract);
2. compiles the real installer source against a minimal, structurally-valid fixture bundle - **must succeed** and produce an installer artifact;
3. asserts the service Parameters double-quote the executable (`""`) and contain no backslash-escaped quotes (`\"`);
4. compiles a deliberately malformed copy whose Parameters use `\"` - **must fail**, otherwise the gate cannot discriminate.

It fails closed: if Inno Setup cannot be provisioned, the job fails - it never
skips and never passes without a real compile. This is a compile-level gate
only: it does not create, ship, or claim a runnable bundle.

Run the same gate locally (Inno Setup 6 or 7 under Program Files, or set
`ISCC_PATH` to the compiler):

```powershell
pwsh scripts/test-windows-installer.ps1
```
