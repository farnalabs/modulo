#!/usr/bin/env pwsh
<##
.SYNOPSIS
Assemble and validate the native Windows bundle consumed by Inno Setup.

The builder never creates a launcher, runtime, database, cache, Python
environment, release manifest, or signature. Those are provisioned inputs.
Missing or invalid inputs fail closed before the output is touched.
#>
#Requires -Version 7.0

[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version = "",
    [ValidateSet("amd64")][string]$Architecture = "amd64",
    [string]$OutputDirectory = "build/windows-bundle",
    [string]$LauncherPath = "",
    [string]$RuntimeDirectory = "",
    [string]$PostgresDirectory = "",
    [string]$RedisDirectory = "",
    [string]$BinDirectory = "",
    [string]$FrontendDistDirectory = "",
    [string]$BackendVenvDirectory = "",
    [string]$ReleaseManifestPath = "",
    [string]$ReleaseManifestSignaturePath = "",
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$validatorPath = Join-Path $PSScriptRoot "validate_windows_bundle_manifest.py"
$schemaPath = Join-Path $PSScriptRoot "windows-bundle-manifest.schema.json"

function Fail([string]$Message) { throw "Windows bundle build failed: $Message" }
function Full([string]$Path) { return [IO.Path]::GetFullPath($Path).TrimEnd([char[]]@('\','/')) }
function Resolve-Output([string]$Reference) {
    if ([string]::IsNullOrWhiteSpace($Reference)) { Fail "OutputDirectory must not be empty." }
    $root = Full $repoRoot
    $candidate = if ([IO.Path]::IsPathRooted($Reference)) { Full $Reference } else { Full (Join-Path $root $Reference) }
    if ($candidate -eq $root) { Fail "refusing OutputDirectory '$Reference': repository root." }
    if (-not $candidate.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { Fail "refusing OutputDirectory '$Reference': outside repository root." }
    if ($candidate -match '(?i)(^|[\\/])\.git([\\/]|$)') { Fail "refusing OutputDirectory '$Reference': inside .git." }
    if (Test-Path -LiteralPath $candidate) {
        $item = Get-Item -LiteralPath $candidate -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { Fail "refusing OutputDirectory '$Reference': reparse point." }
        $links = @(Get-ChildItem -LiteralPath $candidate -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })
        if ($links.Count -gt 0) { Fail "refusing OutputDirectory '$Reference': contains reparse point '$($links[0].FullName)'." }
    }
    return $candidate
}
function Require-File([string]$Path, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { Fail "$Label is missing: $Path" }
    if ((Get-Item -LiteralPath $Path).Length -eq 0) { Fail "$Label is empty: $Path" }
    return (Resolve-Path -LiteralPath $Path).Path
}
function Require-Directory([string]$Path, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path -PathType Container)) { Fail "$Label is missing: $Path" }
    if (@(Get-ChildItem -LiteralPath $Path -Force).Count -eq 0) { Fail "$Label is empty: $Path" }
    return (Resolve-Path -LiteralPath $Path).Path
}
function Resolve-Input([string]$Value, [string]$EnvironmentName) {
    if ([string]::IsNullOrWhiteSpace($Value)) { $Value = [Environment]::GetEnvironmentVariable($EnvironmentName) }
    return $Value
}
function Invoke-Validator([string[]]$Arguments) {
    $result = & uv run --project (Join-Path $repoRoot "backend") python $validatorPath @Arguments 2>&1
    [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = @($result | ForEach-Object { [string]$_ }) }
}
function Check([string]$Label, [scriptblock]$Action) {
    try { & $Action; [pscustomobject]@{ Label=$Label; Status="OK"; Detail="verified" } }
    catch { [pscustomobject]@{ Label=$Label; Status="FAIL"; Detail=$_.Exception.Message } }
}

$output = Resolve-Output $OutputDirectory
if ([string]::IsNullOrWhiteSpace($Version)) {
    $package = Join-Path $repoRoot "frontend/package.json"
    if (-not (Test-Path -LiteralPath $package)) { Fail "Version is required when frontend/package.json is absent." }
    $Version = [string]((Get-Content -Raw -Encoding UTF8 $package | ConvertFrom-Json).version)
}
$LauncherPath = Resolve-Input $LauncherPath "MODULO_WINDOWS_LAUNCHER"
$RuntimeDirectory = Resolve-Input $RuntimeDirectory "MODULO_WINDOWS_RUNTIME_DIR"
$PostgresDirectory = Resolve-Input $PostgresDirectory "MODULO_WINDOWS_POSTGRES_DIR"
$RedisDirectory = Resolve-Input $RedisDirectory "MODULO_WINDOWS_REDIS_DIR"
$BinDirectory = Resolve-Input $BinDirectory "MODULO_WINDOWS_BIN_DIR"
$FrontendDistDirectory = Resolve-Input $FrontendDistDirectory "MODULO_WINDOWS_FRONTEND_DIST_DIR"
$BackendVenvDirectory = Resolve-Input $BackendVenvDirectory "MODULO_WINDOWS_BACKEND_VENV_DIR"
$ReleaseManifestPath = Resolve-Input $ReleaseManifestPath "MODULO_WINDOWS_RELEASE_MANIFEST"
$ReleaseManifestSignaturePath = Resolve-Input $ReleaseManifestSignaturePath "MODULO_WINDOWS_RELEASE_MANIFEST_SIG"

$inputs = @(
    @{ Path=$LauncherPath; Label="native launcher"; File=$true },
    @{ Path=$RuntimeDirectory; Label="CPython runtime"; File=$false },
    @{ Path=$PostgresDirectory; Label="PostgreSQL runtime"; File=$false },
    @{ Path=$RedisDirectory; Label="Redis runtime"; File=$false },
    @{ Path=$BinDirectory; Label="native support binaries"; File=$false },
    @{ Path=$FrontendDistDirectory; Label="frontend distribution"; File=$false },
    @{ Path=$BackendVenvDirectory; Label="backend Python environment"; File=$false },
    @{ Path=$ReleaseManifestPath; Label="signed release manifest"; File=$true },
    @{ Path=$ReleaseManifestSignaturePath; Label="release manifest signature"; File=$true }
)
$checks = foreach ($requirement in $inputs) {
    Check $requirement.Label { if ($requirement.File) { Require-File $requirement.Path $requirement.Label | Out-Null } else { Require-Directory $requirement.Path $requirement.Label | Out-Null } }
}
$checks += Check "manifest schema" { Require-File $schemaPath "manifest schema" | Out-Null }
Write-Output "Windows native bundle prerequisite check"
$checks | ForEach-Object { Write-Output ("  {0,-32} {1,-5} {2}" -f ($_.Label + ":"), $_.Status, $_.Detail) }
if ($ValidateOnly) {
    Write-Output "Dry validation only: no bundle directory or artifact was created."
    if (@($checks | Where-Object Status -eq "FAIL").Count -gt 0) { Fail "one or more provisioned native inputs are unavailable." }
    exit 0
}
if (@($checks | Where-Object Status -eq "FAIL").Count -gt 0) { Fail "one or more provisioned native inputs are unavailable." }
if (-not $IsWindows) { Fail "native Windows assembly requires Windows." }

$manifestCheck = Invoke-Validator @("--signed-manifest", $ReleaseManifestPath, "--signature", $ReleaseManifestSignaturePath, "--schema", $schemaPath, "--version", $Version)
if ($manifestCheck.ExitCode -ne 0) { Fail "signed release manifest validation failed: $($manifestCheck.Output -join ' ')" }
if (Test-Path -LiteralPath $output) { Remove-Item -LiteralPath $output -Recurse -Force }
New-Item -ItemType Directory -Path $output -Force | Out-Null
try {
    Copy-Item -LiteralPath $LauncherPath -Destination (Join-Path $output "launcher")
    Copy-Item -LiteralPath $RuntimeDirectory -Destination (Join-Path $output "runtime") -Recurse
    Copy-Item -LiteralPath $PostgresDirectory -Destination (Join-Path $output "pg") -Recurse
    Copy-Item -LiteralPath $RedisDirectory -Destination (Join-Path $output "redis") -Recurse
    Copy-Item -LiteralPath $BinDirectory -Destination (Join-Path $output "bin") -Recurse
    Copy-Item -LiteralPath $FrontendDistDirectory -Destination (Join-Path $output "frontend-dist") -Recurse
    New-Item -ItemType Directory -Path (Join-Path $output "backend") -Force | Out-Null
    Copy-Item -LiteralPath $BackendVenvDirectory -Destination (Join-Path $output "backend/.venv") -Recurse
    Copy-Item -LiteralPath $ReleaseManifestPath -Destination (Join-Path $output "RELEASE_MANIFEST.json")
    Copy-Item -LiteralPath $ReleaseManifestSignaturePath -Destination (Join-Path $output "RELEASE_MANIFEST.json.sig")
    $result = Invoke-Validator @("--bundle", $output, "--signed-manifest", (Join-Path $output "RELEASE_MANIFEST.json"), "--signature", (Join-Path $output "RELEASE_MANIFEST.json.sig"), "--schema", $schemaPath, "--version", $Version)
    if ($result.ExitCode -ne 0) { Fail "assembled bundle validation failed: $($result.Output -join ' ')" }
    Write-Output "Windows native bundle created: $output"
    Write-Output "Installer input: $output"
} catch { if (Test-Path -LiteralPath $output) { Remove-Item -LiteralPath $output -Recurse -Force -ErrorAction SilentlyContinue }; throw }
