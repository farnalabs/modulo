#!/usr/bin/env pwsh
<##
.SYNOPSIS
Build the reproducible Windows native bundle layout and its validated manifest.

.DESCRIPTION
Builds frontend/dist with the repository's pinned pnpm tooling, packages the
Python CLI with PyInstaller, and stages only real files into a bundle directory.
PostgreSQL and Redis native binaries are explicit inputs; missing inputs fail
closed. This produces a bundle directory, not an Inno Setup installer.

The resulting artifact is not a runnable Windows runtime yet. The launcher
currently refuses Windows; FAR-2229 must land before this contract can be marked
supported. The manifest records runtime_support as unsupported pending FAR-2229.
Pass -ValidateOnly to validate prerequisite inputs without running the full build.
#>

[CmdletBinding()]
param(
    [string]$Version = "",
    [ValidateSet("amd64")]
    [string]$Architecture = "amd64",
    [string]$OutputDirectory = "build/windows-bundle",
    [string]$PostgresDirectory = "",
    [string]$RedisDirectory = "",
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Fail([string]$Message) {
    throw "Windows bundle build failed: $Message"
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendRoot = Join-Path $repoRoot "backend"
$frontendRoot = Join-Path $repoRoot "frontend"
$schemaPath = Join-Path $PSScriptRoot "windows-bundle-manifest.schema.json"

if ([string]::IsNullOrWhiteSpace($Version)) {
    $packageJson = Get-Content -Raw -Encoding UTF8 (Join-Path $frontendRoot "package.json") | ConvertFrom-Json
    $Version = [string]$packageJson.version
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { Fail "Version must be X.Y.Z; got '$Version'." }
if (-not (Test-Path -LiteralPath $schemaPath)) { Fail "Manifest schema is missing: $schemaPath" }

if ([string]::IsNullOrWhiteSpace($PostgresDirectory)) {
    $PostgresDirectory = $env:MODULO_WINDOWS_POSTGRES_DIR
}
if ([string]::IsNullOrWhiteSpace($RedisDirectory)) {
    $RedisDirectory = $env:MODULO_WINDOWS_REDIS_DIR
}

function RequireDirectory([string]$Label, [string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        Fail "$Label input is required. Pass -${Label}Directory or set MODULO_WINDOWS_$($Label.ToUpper())_DIR."
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        Fail "$Label directory does not exist: $Path"
    }
}

function RequireFile([string]$Label, [string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Fail "$Label binary is missing: $Path"
    }
}

RequireDirectory "Postgres" $PostgresDirectory
RequireDirectory "Redis" $RedisDirectory
$postgresBin = Join-Path $PostgresDirectory "bin"
$redisBin = Join-Path $RedisDirectory "bin"
RequireDirectory "Postgres bin" $postgresBin
RequireDirectory "Redis bin" $redisBin
$requiredPostgresBinaries = @("initdb.exe", "postgres.exe", "pg_isready.exe", "psql.exe", "pg_dump.exe")
$requiredRedisBinaries = @("redis-server.exe", "redis-cli.exe")
foreach ($binary in $requiredPostgresBinaries) { RequireFile "PostgreSQL $binary" (Join-Path $postgresBin $binary) }
foreach ($binary in $requiredRedisBinaries) { RequireFile "Redis $binary" (Join-Path $redisBin $binary) }

if ($ValidateOnly) {
    Write-Host "Validated Windows bundle artifact prerequisites for $Version ($Architecture). Runtime startup remains unsupported until FAR-2229."
    exit 0
}

$pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
if ($null -eq $pnpm) { Fail "pnpm is required. Install the version declared in frontend/package.json (pnpm@11.28.8)." }
$pyinstaller = Get-Command pyinstaller -ErrorAction SilentlyContinue
if ($null -eq $pyinstaller) {
    Fail "PyInstaller is required to create a real modulo.exe. Install it in the backend environment (uv pip install pyinstaller) and retry."
}
$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $python) { Fail "Python is required to validate the manifest against JSON Schema." }

$pnpmVersion = ((& $pnpm.Source --version 2>&1) | Select-Object -Last 1).ToString().Trim()
$pythonVersion = ((& $python.Source -c "import platform; print(platform.python_version())" 2>&1) | Select-Object -Last 1).ToString().Trim()
$pyinstallerVersion = ((& $pyinstaller.Source --version 2>&1) | Select-Object -Last 1).ToString().Trim()
$validatedAtUtc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")

$output = [IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDirectory))
if (Test-Path -LiteralPath $output) { Remove-Item -LiteralPath $output -Recurse -Force }
New-Item -ItemType Directory -Force -Path $output | Out-Null
$frontendDist = Join-Path $frontendRoot "dist"
if (Test-Path -LiteralPath $frontendDist) { Remove-Item -LiteralPath $frontendDist -Recurse -Force }

Push-Location $frontendRoot
try {
    & $pnpm.Source install --frozen-lockfile --ignore-scripts
    if ($LASTEXITCODE -ne 0) { Fail "frontend dependency installation failed with exit code $LASTEXITCODE." }
    & $pnpm.Source run build
    if ($LASTEXITCODE -ne 0) { Fail "frontend build failed with exit code $LASTEXITCODE." }
} finally { Pop-Location }
if (-not (Test-Path -LiteralPath $frontendDist -PathType Container)) { Fail "frontend build produced no dist directory." }

$backendDist = Join-Path $output "pyinstaller"
$entryPoint = Join-Path $output "pyinstaller-entrypoint.py"
@'
from modulo.cli.main import cli

if __name__ == "__main__":
    cli()
'@ | Set-Content -Encoding UTF8 -LiteralPath $entryPoint
Push-Location $backendRoot
try {
    & $pyinstaller.Source --noconfirm --clean --onefile --name modulo --paths (Join-Path $backendRoot "src") --distpath $backendDist --workpath (Join-Path $output "pyinstaller-work") --specpath (Join-Path $output "pyinstaller-spec") $entryPoint
    if ($LASTEXITCODE -ne 0) { Fail "PyInstaller failed with exit code $LASTEXITCODE." }
} finally { Pop-Location }
$launcher = Join-Path $backendDist "modulo.exe"
RequireFile "PyInstaller output" $launcher

Copy-Item -LiteralPath $launcher -Destination (Join-Path $output "modulo.exe")
New-Item -ItemType Directory -Force -Path (Join-Path $output "frontend-dist") | Out-Null
Copy-Item -Path (Join-Path $frontendDist "*") -Destination (Join-Path $output "frontend-dist") -Recurse -Force
New-Item -ItemType Directory -Force -Path (Join-Path $output "postgres") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $output "redis") | Out-Null
Copy-Item -Path (Join-Path $PostgresDirectory "*") -Destination (Join-Path $output "postgres") -Recurse -Force
Copy-Item -Path (Join-Path $RedisDirectory "*") -Destination (Join-Path $output "redis") -Recurse -Force

function Get-BinaryMetadata([string]$FilePath) {
    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
        Fail "Binary missing when computing metadata: $FilePath"
    }
    $hash = (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $size = [int64](Get-Item -LiteralPath $FilePath).Length
    return [ordered]@{
        sha256 = $hash
        size_bytes = $size
    }
}

$postgresMetadata = [ordered]@{}
foreach ($bin in $requiredPostgresBinaries) {
    $binPath = Join-Path $output (Join-Path "postgres\bin" $bin)
    $postgresMetadata[$bin] = Get-BinaryMetadata $binPath
}

$redisMetadata = [ordered]@{}
foreach ($bin in $requiredRedisBinaries) {
    $binPath = Join-Path $output (Join-Path "redis\bin" $bin)
    $redisMetadata[$bin] = Get-BinaryMetadata $binPath
}

$manifest = [ordered]@{
    schema_version = 1
    version = $Version
    architecture = $Architecture
    runtime_support = [ordered]@{
        status = "unsupported"
        reason = "Windows launcher support is pending FAR-2229."
    }
    launcher = [ordered]@{
        path = "modulo.exe"
        entry_point = "modulo.cli.main:cli"
        start_command = "modulo start"
    }
    frontend = [ordered]@{
        dist_path = "frontend-dist"
    }
    native_requirements = [ordered]@{
        postgresql = [ordered]@{
            required = $true
            relative_path = "postgres/bin"
            binaries = @($requiredPostgresBinaries)
            metadata = $postgresMetadata
        }
        redis = [ordered]@{
            required = $true
            relative_path = "redis/bin"
            binaries = @($requiredRedisBinaries)
            metadata = $redisMetadata
        }
    }
    reproducibility = [ordered]@{
        tool_versions = [ordered]@{
            pnpm = $pnpmVersion
            python = $pythonVersion
            pyinstaller = $pyinstallerVersion
        }
        validated_at_utc = $validatedAtUtc
    }
}
$manifestPath = Join-Path $output "modulo-runtime.json"
$manifest | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 -LiteralPath $manifestPath

& $python.Source (Join-Path $PSScriptRoot "validate_windows_bundle_manifest.py") --manifest $manifestPath --schema $schemaPath --bundle $output
if ($LASTEXITCODE -ne 0) { Fail "Manifest validation failed with exit code $LASTEXITCODE." }

Write-Host "Windows bundle created: $output"
Write-Host "Manifest: $manifestPath"
