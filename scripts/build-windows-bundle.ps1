#!/usr/bin/env pwsh
<##
.SYNOPSIS
Build the native Windows bundle layout used by the signed release manifest.

.DESCRIPTION
This builder deliberately refuses to manufacture a Windows runtime. It requires
pinned, hash-verified Windows inputs for CPython and PostgreSQL, a defensible
Redis source, and a real Windows launcher. The current launcher seams are
POSIX-only, and no verified Redis Windows input is provisioned, so the build
fails closed with the exact missing prerequisite.

The intended layout is: runtime, pg, redis, bin, frontend-dist, backend/.venv,
and launcher. The release workflow owns RELEASE_MANIFEST.json and its signature;
this script never creates a competing Windows manifest.
#>
#Requires -Version 7.0

[CmdletBinding()]
param(
    [string]$Version = "",
    [ValidateSet("amd64")]
    [string]$Architecture = "amd64",
    [string]$OutputDirectory = "build/windows-bundle",
    [string]$ManifestPath = "",
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$bundleVersions = Join-Path $PSScriptRoot "bundle-versions.env"
$validator = Join-Path $PSScriptRoot "validate_windows_bundle_manifest.py"

function Fail([string]$Message) { throw "Windows bundle build failed: $Message" }
function FullPath([string]$Path) {
    if ([IO.Path]::IsPathRooted($Path)) { return [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar) }
    return [IO.Path]::GetFullPath((Join-Path $repoRoot $Path)).TrimEnd([IO.Path]::DirectorySeparatorChar)
}
function Assert-SafeOutput([string]$Path) {
    $root = FullPath $repoRoot
    $candidate = FullPath $Path
    if ($candidate -eq $root) { Fail "refusing OutputDirectory '$Path': it is the repository root." }
    $prefix = $root + [IO.Path]::DirectorySeparatorChar
    if (-not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        Fail "refusing OutputDirectory '$Path': it is not strictly beneath the repository root '$root'."
    }
    if (($candidate -split '[\\/]') -contains '.git') { Fail "refusing OutputDirectory '$Path': it is inside a .git directory." }
    $cursor = $candidate
    while ($null -ne $cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { Fail "refusing OutputDirectory '$Path': existing ancestor '$cursor' is a reparse point." }
        }
        if ($cursor -eq $root) { break }
        $cursor = [IO.Directory]::GetParent($cursor).FullName
    }
    return $candidate
}
function Read-Pins {
    if (-not (Test-Path -LiteralPath $bundleVersions -PathType Leaf)) { Fail "shared pin file is missing: $bundleVersions" }
    $pins = @{}
    foreach ($line in Get-Content -LiteralPath $bundleVersions -Encoding UTF8) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $pins[$Matches[1]] = $Matches[2] }
    }
    return $pins
}
function Add-Check([System.Collections.Generic.List[object]]$Checks, [string]$Name, [bool]$Ok, [string]$Detail) {
    $Checks.Add([pscustomobject]@{ Name = $Name; Status = if ($Ok) { "OK" } else { "BLOCKED" }; Detail = $Detail })
}
function Get-Prerequisites($Pins) {
    $checks = [System.Collections.Generic.List[object]]::new()
    Add-Check $checks "version" ($Version -match '^\d+\.\d+\.\d+$') "$Version ($Architecture)"
    Add-Check $checks "PowerShell" $true $PSVersionTable.PSVersion.ToString()
    $windowsPbs = $Pins.ContainsKey("PBS_WINDOWS_AMD64_TARBALL") -and $Pins.ContainsKey("PBS_CPYTHON_WINDOWS_AMD64_SHA256")
    Add-Check $checks "CPython Windows pin" $windowsPbs $(if ($windowsPbs) { "provisioned in bundle-versions.env" } else { "PBS_WINDOWS_AMD64_TARBALL and PBS_CPYTHON_WINDOWS_AMD64_SHA256 are not provisioned; refusing unverified download" })
    $windowsPg = $Pins.ContainsKey("PG_WINDOWS_AMD64_URL") -and $Pins.ContainsKey("PG_WINDOWS_AMD64_SHA256")
    Add-Check $checks "PostgreSQL Windows pin" $windowsPg $(if ($windowsPg) { "provisioned in bundle-versions.env" } else { "PG_WINDOWS_AMD64_URL and PG_WINDOWS_AMD64_SHA256 are not provisioned; refusing operator-supplied binaries" })
    Add-Check $checks "Redis Windows source" $false "no verified Redis-compatible Windows source/licence is provisioned; do not use Memurai, WSL, Cygwin, or a fake executable"
    Add-Check $checks "native Windows launcher" $false "FAR-2229 launcher seams are POSIX-only; a real Windows launcher/Job Object contract is required before assembly"
    Add-Check $checks "frontend/backend staging" $false "not attempted until native runtime, services, and launcher prerequisites are available"
    return $checks
}

$output = Assert-SafeOutput $OutputDirectory
if ([string]::IsNullOrWhiteSpace($Version)) {
    $package = Join-Path $repoRoot "frontend/package.json"
    if (-not (Test-Path -LiteralPath $package -PathType Leaf)) { Fail "cannot derive version; pass -Version X.Y.Z" }
    $Version = [string]((Get-Content -LiteralPath $package -Raw -Encoding UTF8 | ConvertFrom-Json).version)
}
$pins = Read-Pins
$checks = Get-Prerequisites $pins
Write-Output "Windows native bundle prerequisite check"
$checks | ForEach-Object { Write-Output ("  {0,-28} {1,-8} {2}" -f ($_.Name + ":"), $_.Status, $_.Detail) }
$blocked = @($checks | Where-Object Status -eq "BLOCKED")
if ($ValidateOnly) {
    Write-Output "Output directory: $output (guard passed; not created)"
    if ($blocked.Count -gt 0) { Fail ("prerequisites are blocked: " + (($blocked | ForEach-Object Name) -join "; ")) }
    exit 0
}
if ($blocked.Count -gt 0) { Fail ("prerequisites are blocked: " + (($blocked | ForEach-Object Name) -join "; ")) }
if (-not $IsWindows) { Fail "a native Windows bundle can only be built on Windows" }

# This boundary remains intentionally unreachable until the real inputs and
# launcher are provisioned. Never create a placeholder PE or unverified service.
Fail "native Windows inputs are not yet provisioned; no bundle was assembled"
