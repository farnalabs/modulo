# Windows native bundle build

`scripts/build-windows-bundle.ps1` is the Windows counterpart to the native Linux/macOS bundle architecture. It targets one layout only:

```text
runtime/       # python-build-standalone CPython tree
pg/            # pinned PostgreSQL runtime and client tools
redis/         # verified Redis-compatible runtime and tools
bin/           # launcher/service helper boundary
frontend-dist/ # built SPA
backend/.venv/ # frozen backend environment
launcher       # real native launcher contract
```

The builder does not use PyInstaller, operator-supplied arbitrary service folders, or a Windows-only runtime manifest. Release signing owns `RELEASE_MANIFEST.json` and `RELEASE_MANIFEST.json.sig`, the same signed manifest machinery used by the other native bundles. The installer receives that bundle and does not invent another provenance contract.

## Fail-closed prerequisites

The shared `scripts/bundle-versions.env` must provide a Windows CPython artifact name and SHA-256, plus a pinned PostgreSQL Windows URL and SHA-256. Those values are intentionally absent until they are verified and provisioned; the builder refuses to download or accept an unverified substitute.

Redis is currently a hard blocker. No defensible Redis-compatible Windows source and licence input is provisioned. Do not use Memurai, WSL, Cygwin, an operator folder, or a fake executable. The FAR-2229 launcher seams are also POSIX-only: a real Windows launcher and Job Object/service contract must land before assembly can proceed. No placeholder PE is created.

`-ValidateOnly` prints each prerequisite and fails with the missing input names. It is evidence of the contract, not a green dry-run manufactured around missing runtime inputs.

## Commands

```powershell
pwsh scripts/build-windows-bundle.ps1 -ValidateOnly -Version 0.4.0
pwsh scripts/build-windows-bundle.ps1 -Version 0.4.0
```

The second command remains blocked until all pinned inputs and the real launcher are available. Once available, the release workflow must place the signed `RELEASE_MANIFEST.json` and `.sig` adjacent to the bundle root and validate the handoff with:

```powershell
uv run --project backend python scripts/validate_windows_bundle_manifest.py `
  --bundle build/windows-bundle `
  --manifest build/RELEASE_MANIFEST.json
```

The validator checks the established layout, requires the signed release manifest, and rejects `modulo-runtime.json` by construction. It never creates, signs, or mutates a manifest.

## Safety

The output path must be strictly beneath the repository root, outside `.git`, and free of reparse-point ancestors. The builder performs this check before it writes or deletes anything. A failed prerequisite never creates an incomplete bundle.
