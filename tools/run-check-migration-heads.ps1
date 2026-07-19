$ErrorActionPreference = "Stop"

$repoRoot = (git rev-parse --show-toplevel).Trim()
$cursor = [System.IO.DirectoryInfo]::new($repoRoot)
$harnessScript = $null

while ($null -ne $cursor) {
    if ($cursor.Name -eq "Repos") {
        $candidate = Join-Path $cursor.FullName "devtools/harness/tools/check-migration-heads.ps1"
        if (Test-Path -LiteralPath $candidate) {
            $harnessScript = $candidate
        }
        break
    }
    $cursor = $cursor.Parent
}

if ($null -eq $harnessScript) {
    Write-Error "Could not locate Repos/devtools/harness/tools/check-migration-heads.ps1 from $repoRoot"
    exit 1
}

& $harnessScript
exit $LASTEXITCODE
