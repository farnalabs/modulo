# PowerShell Tests

Run every PowerShell suite from the repository root:

```powershell
powershell -NoProfile -File tools/run-pester-tests.ps1
```

The runner uses Pester 5.7.1 regardless of globally installed modules. On first
use it downloads the pinned PowerShell Gallery package, verifies its SHA-256,
and extracts it under `.tool-cache/`. Later runs reuse that ignored cache. A
hash mismatch, discovery error, failed test, skipped test, or not-run test makes
the command fail.

Pass one or more repository-relative paths to run selected suites:

```powershell
powershell -NoProfile -File tools/run-pester-tests.ps1 `
  -TestPath tools/tests/test-graph-manifest.ps1,tools/tests/test-validate-manifest.ps1
```
