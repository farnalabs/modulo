<##
.SYNOPSIS
Validate the native Windows Inno Setup handoff without inventing bundle inputs.

The script creates only empty contract directories so Inno Setup proves its
compile-time presence guards. It also creates intentionally invalid placeholder
content for the positive compile probe: Inno Setup checks presence only, not
cryptographic validity. The real bundle builder performs signed-manifest
verification before this handoff.
#>
[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version = "0.3.323",
    [ValidateSet("amd64")][string]$Arch = "amd64"
)
$ErrorActionPreference = "Stop"
$repoRoot = (Get-Location).Path
$issPath = Join-Path $repoRoot "deploy\windows\modulo.iss"
if (-not (Test-Path -LiteralPath $issPath -PathType Leaf)) { throw "Run from repo root." }
$isccPath = @((Join-Path ${env:ProgramFiles} "Inno Setup 6\ISCC.exe"), (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $isccPath) { throw "Inno Setup 6 not found. Install from https://jrsoftware.org/isdl.php" }
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("modulo-installer-contract-" + [guid]::NewGuid().ToString("N"))
$missingLauncher = Join-Path $tempRoot "missing-launcher"
$missingSignature = Join-Path $tempRoot "missing-signature"
$invalidFixture = Join-Path $tempRoot "invalid-fixture"
$output = Join-Path $tempRoot "output"
foreach ($bundle in @($missingLauncher, $missingSignature, $invalidFixture)) {
    foreach ($relative in @("runtime", "pg", "redis", "bin", "frontend-dist", "backend\.venv")) { New-Item -ItemType Directory -Path (Join-Path $bundle $relative) -Force | Out-Null }
}
New-Item -ItemType Directory -Path $output -Force | Out-Null
function Add-InvalidManifestFixture([string]$Bundle, [bool]$Signature) {
    # Deliberately invalid placeholders exercise rejection only; they are not a
    # native launcher, signed manifest, or valid installer bundle.
    Set-Content -LiteralPath (Join-Path $Bundle "launcher") -Value "invalid fixture: not a PE launcher" -NoNewline
    Set-Content -LiteralPath (Join-Path $Bundle "RELEASE_MANIFEST.json") -Value '{"manifest_version":1}' -NoNewline
    if ($Signature) { Set-Content -LiteralPath (Join-Path $Bundle "RELEASE_MANIFEST.json.sig") -Value '{"key_id":"invalid-fixture","signature":"not-a-signature"}' -NoNewline }
}
Add-InvalidManifestFixture $missingLauncher $true
Remove-Item -LiteralPath (Join-Path $missingLauncher "launcher")
Add-InvalidManifestFixture $missingSignature $false
Add-InvalidManifestFixture $invalidFixture $true
function Invoke-Iscc([string]$Bundle, [string]$Name) {
    & $isccPath "/Qp" "/O$output" "/F$Name" "/DVERSION_NUM=$Version" "/DBUNDLE_ARCH=$Arch" "/DBUNDLE_DIR=$Bundle" "/DMODULO_URL=http://127.0.0.1:8000" $issPath 2>&1 | ForEach-Object { Write-Information $_ -InformationAction Continue }
    return [int]$LASTEXITCODE
}
try {
    Write-Output "=== Modulo Windows Installer Contract Test ==="
    if ((Invoke-Iscc $missingLauncher "missing-launcher") -eq 0) { throw "Installer accepted missing native launcher." }
    if ((Invoke-Iscc $missingSignature "missing-signature") -eq 0) { throw "Installer accepted missing signed manifest signature." }
    if ((Invoke-Iscc $invalidFixture "invalid-fixture") -ne 0) { throw "Installer failed to compile a structurally complete bundle: Inno Setup validates presence, not cryptographic content." }
    Write-Output "Presence guards reject missing native launcher and signed-manifest signature."
    Write-Output "Structurally complete invalid-content fixture compiled; cryptographic validity belongs to the bundle builder, not Inno Setup."
}
finally { if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force } }
