#!/usr/bin/env pwsh
<##
.SYNOPSIS
Build the Windows native bundle layout and its validated manifest.

.DESCRIPTION
Assembles a Windows bundle directory from three inputs: a frontend build, a
PyInstaller-packaged `modulo.exe`, and operator-supplied PostgreSQL/Redis native
binaries. Missing inputs fail closed. This produces a bundle directory, not an
Inno Setup installer.

What this script enforces:
  * The output directory is resolved and REFUSED before anything is written or
    deleted unless it is strictly beneath the repository root, is not the repo
    root, is not inside `.git`, and is not a filesystem root or the user profile.
    A directory containing a reparse point (junction/symlink) is refused rather
    than recursively deleted.
  * PyInstaller scaffolding (`pyinstaller/`, `pyinstaller-work/`,
    `pyinstaller-spec/`, `pyinstaller-entrypoint.py`) is built in a temp staging
    directory OUTSIDE the bundle, and the emitted bundle is checked for it.
  * PyInstaller runs from the backend's own uv environment, and the packaged
    launcher must run `version` successfully - reporting this bundle's version -
    before the manifest is written. A launcher that cannot run fails the build.
    A Windows launcher can only be built and smoke-run on Windows.
  * Prerequisite checks are reported one line each and summarised, so a run
    states exactly what was and was not checked.

What this script does NOT claim:
  * It is not reproducible and does not pin tooling. pnpm, Python and
    PyInstaller are whatever the machine and the backend uv environment resolve;
    their versions are RECORDED in the manifest for diagnostics only.
  * The PostgreSQL and Redis binaries are supplied by the operator. They are
    checked for presence and hashed as copied; the script does not verify their
    provenance, licensing or completeness beyond the required inventory.
  * The result is not a supported Windows runtime: the launcher refuses Windows
    until FAR-2229 lands, so the manifest records runtime_support.status as
    `unsupported`.

.EXAMPLE
pwsh scripts/build-windows-bundle.ps1 -PostgresDirectory C:\pg -RedisDirectory C:\redis

.EXAMPLE
# Prerequisites only: no build, no writes. Also verifies the toolchain.
pwsh scripts/build-windows-bundle.ps1 -ValidateOnly
#>

#Requires -Version 7.0

[CmdletBinding()]
param(
    [string]$Version = "",
    [ValidateSet("amd64")]
    [string]$Architecture = "amd64",
    [string]$OutputDirectory = "build/windows-bundle",
    [string]$PostgresDirectory = "",
    [string]$RedisDirectory = "",
    [string]$ManifestPath = "",
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendRoot = Join-Path $repoRoot "backend"
$frontendRoot = Join-Path $repoRoot "frontend"
$schemaPath = Join-Path $PSScriptRoot "windows-bundle-manifest.schema.json"
$validatorPath = Join-Path $PSScriptRoot "validate_windows_bundle_manifest.py"
$postgresEnvVar = "MODULO_WINDOWS_POSTGRES_DIR"
$redisEnvVar = "MODULO_WINDOWS_REDIS_DIR"

function Fail([string]$Message) {
    throw "Windows bundle build failed: $Message"
}

# ---------------------------------------------------------------------------
# Safety: paths that may be written into or recursively deleted
# ---------------------------------------------------------------------------

function Get-PlatformPathComparison {
    <#
    .SYNOPSIS
    The string comparison a path-containment guard must use on this platform.

    Windows and macOS filesystems are case-insensitive, so a case-sensitive
    comparison there would let a differently-cased path slip past a guard.
    #>
    if ($IsWindows -or $IsMacOS) {
        return [StringComparison]::OrdinalIgnoreCase
    }
    return [StringComparison]::Ordinal
}

function Get-NormalizedFullPath {
    <#
    .SYNOPSIS
    The absolute form of a path, with any trailing directory separators removed.

    Trimming the trailing separator is what lets two paths be compared as
    ancestors ("C:\repo" against "C:\repo\") without a prefix mismatch.
    #>
    param([Parameter(Mandatory)][string]$Path)

    $separators = [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    return [IO.Path]::GetFullPath($Path).TrimEnd($separators)
}

function Resolve-OutputCandidatePath {
    <#
    .SYNOPSIS
    Resolve -OutputDirectory against the repository root when it is not already rooted.
    #>
    param(
        [Parameter(Mandatory)][string]$RootFull,
        [Parameter(Mandatory)][AllowEmptyString()][string]$OutputDirectory
    )

    $candidate = if ([IO.Path]::IsPathRooted($OutputDirectory)) {
        $OutputDirectory
    } else {
        Join-Path $RootFull $OutputDirectory
    }
    return Get-NormalizedFullPath -Path $candidate
}

function Assert-NotFilesystemRoot {
    <#
    .SYNOPSIS
    Refuse a path that IS a filesystem root: there is nothing safely deletable above it.
    #>
    param(
        [Parameter(Mandatory)][string]$Reference,
        [Parameter(Mandatory)][string]$Resolved
    )

    $separators = [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $pathRoot = [IO.Path]::GetPathRoot($Resolved).TrimEnd($separators)
    if ($Resolved -eq $pathRoot) {
        Fail "refusing OutputDirectory '$Reference': '$Resolved' is a filesystem root."
    }
}

function Assert-NotUserProfilePath {
    <#
    .SYNOPSIS
    Refuse the user profile and any ancestor of it - a recursive delete there is destructive.
    #>
    param(
        [Parameter(Mandatory)][string]$Reference,
        [Parameter(Mandatory)][string]$Resolved,
        [Parameter(Mandatory)][string]$Separator,
        [Parameter(Mandatory)][StringComparison]$Comparison
    )

    if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        return
    }
    $separators = [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $profileFull = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd($separators)
    if ($Resolved -eq $profileFull) {
        Fail "refusing OutputDirectory '$Reference': '$Resolved' is the user profile."
    }
    if ($profileFull.StartsWith($Resolved + $Separator, $Comparison)) {
        Fail "refusing OutputDirectory '$Reference': '$Resolved' is an ancestor of the user profile."
    }
}

function Assert-NoGitSegment {
    <#
    .SYNOPSIS
    Refuse an output path whose relative segments include a `.git` directory.
    #>
    param(
        [Parameter(Mandatory)][string]$Reference,
        [Parameter(Mandatory)][string]$Resolved,
        [Parameter(Mandatory)][string]$Prefix
    )

    $separators = [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $relative = $Resolved.Substring($Prefix.Length)
    foreach ($segment in $relative.Split($separators, [StringSplitOptions]::RemoveEmptyEntries)) {
        if ($segment -eq ".git") {
            Fail "refusing OutputDirectory '$Reference': '$Resolved' is inside a .git directory."
        }
    }
}

function Assert-BeneathRepositoryRoot {
    <#
    .SYNOPSIS
    Refuse anything that is not STRICTLY beneath the repository root, or that lies inside `.git`.
    #>
    param(
        [Parameter(Mandatory)][string]$Reference,
        [Parameter(Mandatory)][string]$Resolved,
        [Parameter(Mandatory)][string]$RootFull,
        [Parameter(Mandatory)][string]$Separator,
        [Parameter(Mandatory)][StringComparison]$Comparison
    )

    if ($Resolved -eq $RootFull) {
        Fail "refusing OutputDirectory '$Reference': '$Resolved' is the repository root."
    }
    $prefix = $RootFull + $Separator
    if (-not $Resolved.StartsWith($prefix, $Comparison)) {
        Fail "refusing OutputDirectory '$Reference': '$Resolved' is not strictly beneath the repository root '$RootFull'."
    }
    Assert-NoGitSegment -Reference $Reference -Resolved $Resolved -Prefix $prefix
}

function Resolve-BundleOutputPath {
    <#
    .SYNOPSIS
    Resolve -OutputDirectory to an absolute path that is safe to write into and delete.

    .DESCRIPTION
    Throws (touching nothing on disk) unless the resolved path is strictly beneath
    the repository root and is not the repository root, a `.git` directory, a
    filesystem root, or the user profile / an ancestor of it.
    #>
    param(
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][AllowEmptyString()][string]$OutputDirectory
    )

    if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
        Fail "OutputDirectory must not be empty."
    }
    $separator = [IO.Path]::DirectorySeparatorChar
    $comparison = Get-PlatformPathComparison
    $rootFull = Get-NormalizedFullPath -Path $RepositoryRoot
    $resolved = Resolve-OutputCandidatePath -RootFull $rootFull -OutputDirectory $OutputDirectory
    Assert-NotFilesystemRoot -Reference $OutputDirectory -Resolved $resolved
    Assert-NotUserProfilePath -Reference $OutputDirectory -Resolved $resolved -Separator $separator -Comparison $comparison
    Assert-BeneathRepositoryRoot -Reference $OutputDirectory -Resolved $resolved -RootFull $rootFull -Separator $separator -Comparison $comparison
    return $resolved
}

function Get-ParentPathOrFail {
    <#
    .SYNOPSIS
    The parent directory of $Path, or a failure when the filesystem root is reached first.
    #>
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Reference
    )

    $parent = [IO.Directory]::GetParent($Path)
    if ($null -eq $parent) {
        Fail "refusing OutputDirectory '$Reference': its existing ancestors cannot be traced to the repository root."
    }
    return Get-NormalizedFullPath -Path $parent.FullName
}

function Get-ExistingAncestorChain {
    <#
    .SYNOPSIS
    Every EXISTING directory from $CandidatePath up to and including the repository root.

    The walk always continues to the root, so a chain that cannot be traced there is
    refused rather than partially checked. Non-existent members are skipped: they
    cannot be inspected, and nothing is written into them before they are created.
    #>
    param(
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][string]$CandidatePath
    )

    $existing = [System.Collections.Generic.List[string]]::new()
    $cursor = Get-NormalizedFullPath -Path $CandidatePath
    while ($true) {
        if (Test-Path -LiteralPath $cursor) {
            $existing.Add($cursor)
        }
        if ($cursor -eq $RepositoryRoot) {
            return $existing
        }
        $cursor = Get-ParentPathOrFail -Path $cursor -Reference $CandidatePath
    }
}

function Assert-AncestorIsSafe {
    <#
    .SYNOPSIS
    Refuse an existing ancestor that is a reparse point, or that resolves outside the repository.

    A reparse point is refused even when it resolves back inside the repository,
    because a later recursive delete or copy must never follow a junction/symlink.
    #>
    param(
        [Parameter(Mandatory)][string]$Reference,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$RootFull,
        [Parameter(Mandatory)][string]$RootPrefix,
        [Parameter(Mandatory)][StringComparison]$Comparison
    )

    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        Fail "refusing OutputDirectory '$Reference': existing ancestor '$Path' is a reparse point (junction/symlink)."
    }
    $resolvedItem = Get-NormalizedFullPath -Path (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    if (($resolvedItem -ne $RootFull) -and (-not $resolvedItem.StartsWith($RootPrefix, $Comparison))) {
        Fail "refusing OutputDirectory '$Reference': existing ancestor '$Path' resolves outside the repository root."
    }
}

function Assert-SafeExistingAncestors {
    <#
    .SYNOPSIS
    Inspect every existing path from the repository root to a candidate output.

    This runs before any output directory is created or deleted. Reparse points
    are refused even when they resolve beneath the repository, because a later
    recursive delete or copy must never follow a junction/symlink.
    #>
    param(
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][string]$CandidatePath
    )

    $comparison = Get-PlatformPathComparison
    $rootFull = Get-NormalizedFullPath -Path $RepositoryRoot
    $rootPrefix = $rootFull + [IO.Path]::DirectorySeparatorChar
    $existing = @(Get-ExistingAncestorChain -RepositoryRoot $rootFull -CandidatePath $CandidatePath)
    foreach ($path in $existing | Sort-Object Length) {
        Assert-AncestorIsSafe -Reference $CandidatePath -Path $path -RootFull $rootFull -RootPrefix $rootPrefix -Comparison $comparison
    }
}

function Assert-RemovableDirectory {
    <#
    .SYNOPSIS
    Refuse a recursive delete that would follow a reparse point (junction/symlink).
    #>
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) { return }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        Fail "refusing to remove '$Path': it is not a directory."
    }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        Fail "refusing to remove '$Path': it is itself a reparse point (junction/symlink)."
    }
    $links = @(Get-ChildItem -LiteralPath $Path -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })
    if ($links.Count -gt 0) {
        Fail "refusing to remove '$Path': it contains a reparse point a recursive delete would follow: $($links[0].FullName)"
    }
}

# ---------------------------------------------------------------------------
# Tooling, inventory and prerequisite checks
# ---------------------------------------------------------------------------

function Invoke-BackendPython {
    <#
    .SYNOPSIS
    Run the backend uv environment's Python. Returns exit code plus captured output.
    #>
    param([Parameter(Mandatory)][string[]]$Arguments)

    $output = & $script:uvPath run --project $backendRoot python @Arguments 2>&1
    return [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output   = @(@($output) | ForEach-Object { [string]$_ })
    }
}

function Get-CommandVersion {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [string[]]$Arguments = @("--version")
    )

    $output = & $Executable @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { return $null }
    $line = @(@($output) | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Last 1)
    if ($line.Count -eq 0) { return $null }
    return $line[0].Trim()
}

function Get-ToolchainReport {
    <#
    .SYNOPSIS
    Probe every build tool; each value is a version string or $null when absent.
    #>
    $status = [ordered]@{
        uv                  = $null
        pnpm                = $null
        backend_python      = $null
        backend_pyinstaller = $null
    }
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -eq $uvCommand) { return $status }
    $status["uv"] = Get-CommandVersion -Executable $uvCommand.Source

    $pnpmCommand = Get-Command pnpm -ErrorAction SilentlyContinue
    if ($null -ne $pnpmCommand) { $status["pnpm"] = Get-CommandVersion -Executable $pnpmCommand.Source }

    $pythonProbe = Invoke-BackendPython -Arguments @("-c", "import jsonschema, sys; print(sys.version.split()[0])")
    if ($pythonProbe.ExitCode -eq 0) {
        $status["backend_python"] = (@($pythonProbe.Output) | Select-Object -Last 1).Trim()
    }
    $pyinstallerProbe = & $script:uvPath run --project $backendRoot pyinstaller --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        $line = @(@($pyinstallerProbe) | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Last 1)
        if ($line.Count -gt 0) { $status["backend_pyinstaller"] = $line[0].Trim() }
    }
    return $status
}

function Get-BundleInventory {
    <#
    .SYNOPSIS
    Read the required native binary inventory from the validator (single source).
    #>
    $result = Invoke-BackendPython -Arguments @($validatorPath, "--print-inventory")
    if ($result.ExitCode -ne 0) { return $null }
    try {
        $inventory = ($result.Output -join [Environment]::NewLine) | ConvertFrom-Json -AsHashtable
    } catch {
        return $null
    }
    foreach ($service in @("postgresql", "redis")) {
        if (-not $inventory.ContainsKey($service)) { return $null }
        $requirement = $inventory[$service]
        if ([string]::IsNullOrWhiteSpace([string]$requirement["relative_path"])) { return $null }
        if (@($requirement["binaries"]).Count -eq 0) { return $null }
    }
    return $inventory
}

function New-Check {
    param([string]$Label, [string]$Status, [string]$Detail)
    return [pscustomobject]@{ Label = $Label; Status = $Status; Detail = $Detail }
}

function Get-ServiceInputCheck {
    <#
    .SYNOPSIS
    Check one operator-supplied native runtime directory against the inventory.
    #>
    param(
        [Parameter(Mandatory)][string]$Service,
        [Parameter(Mandatory)][AllowEmptyString()][string]$InputDirectory,
        [Parameter(Mandatory)][string]$Hint,
        [Parameter(Mandatory)]$Requirement
    )

    $relative = ([string]$Requirement["relative_path"]).Replace("/", "\")
    $binRelative = Split-Path -Parent $relative
    if ([string]::IsNullOrWhiteSpace($InputDirectory)) {
        return New-Check "$Service inputs" "FAIL" "not provided. $Hint"
    }
    if (-not (Test-Path -LiteralPath $InputDirectory -PathType Container)) {
        return New-Check "$Service inputs" "FAIL" "directory does not exist: $InputDirectory. $Hint"
    }
    $binDirectory = if ([string]::IsNullOrWhiteSpace($binRelative)) { $InputDirectory } else { Join-Path $InputDirectory $binRelative }
    if (-not (Test-Path -LiteralPath $binDirectory -PathType Container)) {
        return New-Check "$Service inputs" "FAIL" "expected '$relative' beneath '$InputDirectory'. $Hint"
    }
    $missing = @()
    foreach ($binary in @($Requirement["binaries"])) {
        if (-not (Test-Path -LiteralPath (Join-Path $binDirectory ([string]$binary)) -PathType Leaf)) {
            $missing += [string]$binary
        }
    }
    if ($missing.Count -gt 0) {
        return New-Check "$Service inputs" "FAIL" "missing $(($missing -join ', ')) under '$binDirectory'."
    }
    return New-Check "$Service inputs" "OK" "$(@($Requirement['binaries']).Count) required binaries under '$binDirectory'"
}

function Add-ToolchainChecks {
    param([System.Collections.Generic.List[object]]$Checks, $Toolchain)

    $definitions = @(
        @("uv", "not found on PATH - see https://docs.astral.sh/uv/"),
        @("pnpm", "not found on PATH - install the version recorded in frontend/package.json"),
        @("backend python (jsonschema)", "unavailable via 'uv run --project backend' - run 'uv sync' in backend/"),
        @("backend PyInstaller", "not installed in the backend environment - run 'uv add --group dev pyinstaller' in backend/")
    )
    foreach ($definition in $definitions) {
        $key = switch ($definition[0]) {
            "backend python (jsonschema)" { "backend_python"; break }
            "backend PyInstaller" { "backend_pyinstaller"; break }
            default { $definition[0] }
        }
        $value = $Toolchain[$key]
        $status = if ($null -ne $value) { "OK" } else { "FAIL" }
        $detail = if ($null -ne $value) { [string]$value } else { $definition[1] }
        $Checks.Add((New-Check $definition[0] $status $detail))
    }
}

function Add-InventoryChecks {
    param([System.Collections.Generic.List[object]]$Checks, $Inventory, [string]$PostgresDirectory, [string]$RedisDirectory)

    if ($null -eq $Inventory) {
        $Checks.Add((New-Check "native binary inventory" "SKIP" "unavailable (needs the backend Python environment)"))
        foreach ($service in @("postgresql", "redis")) {
            $Checks.Add((New-Check "$service inputs" "SKIP" "not checked: the inventory is unavailable"))
        }
        return
    }
    $summary = foreach ($service in @("postgresql", "redis")) {
        "${service}: $(@($Inventory[$service]['binaries']).Count) binaries at '$([string]$Inventory[$service]['relative_path'])'"
    }
    $Checks.Add((New-Check "native binary inventory" "OK" (($summary -join "; ") + " (from the validator)")))
    $hints = @{
        postgresql = "Pass -PostgresDirectory <dir> or set $postgresEnvVar to the PostgreSQL distribution directory."
        redis = "Pass -RedisDirectory <dir> or set $redisEnvVar to the Redis distribution directory."
    }
    $inputs = @{ postgresql = $PostgresDirectory; redis = $RedisDirectory }
    foreach ($service in @("postgresql", "redis")) {
        $Checks.Add((Get-ServiceInputCheck -Service $service -InputDirectory $inputs[$service] -Hint $hints[$service] -Requirement $Inventory[$service]))
    }
}

function Get-PrerequisiteChecks {
    param($Toolchain, $Inventory, [string]$PostgresDirectory, [string]$RedisDirectory)

    $checks = [System.Collections.Generic.List[object]]::new()
    $versionStatus = if ($Version -match '^\d+\.\d+\.\d+$') { "OK" } else { "FAIL" }
    $schemaStatus = if (Test-Path -LiteralPath $schemaPath -PathType Leaf) { "OK" } else { "FAIL" }
    $checks.Add((New-Check "version" $versionStatus "$Version ($Architecture)"))
    $checks.Add((New-Check "manifest schema" $schemaStatus "$schemaPath"))
    Add-ToolchainChecks -Checks $checks -Toolchain $Toolchain
    Add-InventoryChecks -Checks $checks -Inventory $Inventory -PostgresDirectory $PostgresDirectory -RedisDirectory $RedisDirectory
    return $checks
}

function Show-CheckReport {
    param($Checks)

    Write-Output "Windows bundle prerequisite check"
    Write-Output ""
    foreach ($check in $Checks) {
        Write-Output ("  {0,-30} {1,-5} {2}" -f ($check.Label + ":"), $check.Status, $check.Detail)
    }
    Write-Output ""
}

# ---------------------------------------------------------------------------
# 1. Output path guard - runs before anything is written, created or deleted.
# ---------------------------------------------------------------------------
$output = Resolve-BundleOutputPath -RepositoryRoot $repoRoot -OutputDirectory $OutputDirectory
$outputParent = [IO.Directory]::GetParent($output).FullName
Assert-SafeExistingAncestors -RepositoryRoot $repoRoot -CandidatePath $outputParent

# ---------------------------------------------------------------------------
# 2. Version and schema
# ---------------------------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($Version)) {
    $packageJsonPath = Join-Path $frontendRoot "package.json"
    if (-not (Test-Path -LiteralPath $packageJsonPath -PathType Leaf)) {
        Fail "cannot derive the version: '$packageJsonPath' is missing. Pass -Version X.Y.Z."
    }
    $packageJson = Get-Content -Raw -Encoding UTF8 -LiteralPath $packageJsonPath | ConvertFrom-Json
    $Version = [string]$packageJson.version
}

# ---------------------------------------------------------------------------
# 3. Toolchain, inventory and inputs, reported as one set of checks
# ---------------------------------------------------------------------------
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
$script:uvPath = if ($null -ne $uvCommand) { $uvCommand.Source } else { "" }
$toolchain = Get-ToolchainReport
$inventory = if ($null -ne $toolchain["backend_python"]) { Get-BundleInventory } else { $null }

if ([string]::IsNullOrWhiteSpace($PostgresDirectory)) { $PostgresDirectory = $env:MODULO_WINDOWS_POSTGRES_DIR }
if ([string]::IsNullOrWhiteSpace($RedisDirectory)) { $RedisDirectory = $env:MODULO_WINDOWS_REDIS_DIR }

$checks = [System.Collections.Generic.List[object]]::new()
foreach ($check in @(Get-PrerequisiteChecks -Toolchain $toolchain -Inventory $inventory -PostgresDirectory $PostgresDirectory -RedisDirectory $RedisDirectory)) {
    $checks.Add($check)
}

# ---------------------------------------------------------------------------
# 4. -ValidateOnly: report, then exit. Nothing is built and nothing is written.
# ---------------------------------------------------------------------------
if ($ValidateOnly) {
    $manifestCheck = $null
    if (-not [string]::IsNullOrWhiteSpace($ManifestPath)) {
        if ($null -eq $toolchain["backend_python"]) {
            $manifestCheck = New-Check "manifest validation" "SKIP" "not checked: backend Python is unavailable"
        } else {
            $manifestResult = Invoke-BackendPython -Arguments @($validatorPath, "--manifest", $ManifestPath, "--schema", $schemaPath, "--bundle", $output)
            if ($manifestResult.ExitCode -eq 0) {
                $manifestCheck = New-Check "manifest validation" "OK" $ManifestPath
            } else {
                $manifestCheck = New-Check "manifest validation" "FAIL" (($manifestResult.Output -join " ").Trim())
            }
        }
        $checks.Add($manifestCheck)
    }
    Show-CheckReport -Checks $checks

    $checked = @($checks | Where-Object { $_.Status -ne "SKIP" } | ForEach-Object { $_.Label })
    $notChecked = @("frontend dependency install and build", "PyInstaller packaging", "launcher smoke run", "bundle assembly", "manifest generation")
    $notChecked += @($checks | Where-Object { $_.Status -eq "SKIP" } | ForEach-Object { $_.Label })
    Write-Output ("Checked: " + ($checked -join ", ") + ".")
    Write-Output ("Not checked: " + ($notChecked -join ", ") + ".")
    Write-Output ("Output directory: {0} (guard passed; not created in -ValidateOnly)" -f $output)
    if (-not $IsWindows) {
        Write-Output "Note: a full build requires Windows - PyInstaller cannot cross-compile a Windows launcher, and it cannot be smoke-run here."
    }

    $failed = @($checks | Where-Object { $_.Status -eq "FAIL" })
    if ($failed.Count -gt 0) {
        Fail ("prerequisite check failed: " + (($failed | ForEach-Object { $_.Label }) -join "; ") + ". See the report above.")
    }
    Write-Output "Prerequisite check passed. Nothing was built."
    exit 0
}

# ---------------------------------------------------------------------------
# 5. Full build
# ---------------------------------------------------------------------------
Show-CheckReport -Checks $checks
$failed = @($checks | Where-Object { $_.Status -eq "FAIL" })
if ($failed.Count -gt 0) {
    Fail ("prerequisite check failed: " + (($failed | ForEach-Object { $_.Label }) -join "; ") + ". See the report above.")
}
if (-not $IsWindows) {
    Fail "a Windows launcher can only be built and smoke-run on Windows; this host is '$([System.Runtime.InteropServices.RuntimeInformation]::OSDescription)'."
}
$pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
if ($null -eq $pnpm) { Fail "pnpm was not found on PATH." }

function Get-BinaryMetadata([string]$FilePath) {
    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
        Fail "Binary missing when computing metadata: $FilePath"
    }
    $hash = (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $size = [int64](Get-Item -LiteralPath $FilePath).Length
    return [ordered]@{
        sha256     = $hash
        size_bytes = $size
    }
}

function Invoke-LauncherSmokeCheck([string]$Executable, [string]$StagingDirectory) {
    <#
    .SYNOPSIS
    Run `<launcher> version` and fail unless it exits 0 and reports this bundle's version.
    #>
    $stdoutPath = Join-Path $StagingDirectory "launcher-smoke-stdout.txt"
    $stderrPath = Join-Path $StagingDirectory "launcher-smoke-stderr.txt"
    $process = Start-Process -FilePath $Executable -ArgumentList "version" -NoNewWindow -PassThru `
        -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
    if (-not $process.WaitForExit(120000)) {
        # Only a PID this script started.
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        Fail "the packaged launcher did not finish 'version' within 120s."
    }
    # A parameterless WaitForExit after the timed wait is what guarantees ExitCode
    # is populated on a process created with -PassThru.
    $process.WaitForExit()
    $exitCode = $process.ExitCode
    $stdout = if (Test-Path -LiteralPath $stdoutPath) { (Get-Content -Raw -LiteralPath $stdoutPath).Trim() } else { "" }
    $stderr = if (Test-Path -LiteralPath $stderrPath) { (Get-Content -Raw -LiteralPath $stderrPath).Trim() } else { "" }
    if ($exitCode -ne 0) {
        Fail "the packaged launcher exited $exitCode on 'version'. stderr: $stderr"
    }
    if ($stdout -notmatch '^modulo\s+(\S+)$') {
        Fail "the packaged launcher did not report a version. stdout: '$stdout' stderr: '$stderr'"
    }
    $reportedVersion = $Matches[1]
    if ($reportedVersion -ne $Version) {
        Fail "the packaged launcher reports version '$reportedVersion' but the bundle version is '$Version'."
    }
    Write-Output "Launcher smoke check OK: 'modulo.exe version' reported modulo $reportedVersion."
}

$frontendDist = Join-Path $frontendRoot "dist"
$staging = Join-Path ([IO.Path]::GetTempPath()) ("modulo-windows-bundle-{0}" -f [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $staging -Force | Out-Null
try {
    # 5a. Frontend build. Vite owns frontend/dist and empties it on build.
    Push-Location $frontendRoot
    try {
        & $pnpm.Source install --frozen-lockfile --ignore-scripts
        if ($LASTEXITCODE -ne 0) { Fail "frontend dependency installation failed with exit code $LASTEXITCODE." }
        & $pnpm.Source run build
        if ($LASTEXITCODE -ne 0) { Fail "frontend build failed with exit code $LASTEXITCODE." }
    } finally { Pop-Location }
    if (-not (Test-Path -LiteralPath $frontendDist -PathType Container)) { Fail "frontend build produced no dist directory." }
    if (@(Get-ChildItem -LiteralPath $frontendDist -Force).Count -eq 0) { Fail "frontend build produced an empty dist directory." }

    # 5b. PyInstaller into temp staging - nothing here may reach the bundle.
    $entryPoint = Join-Path $staging "pyinstaller-entrypoint.py"
    $entryPointSource = @'
from modulo.cli.main import cli

if __name__ == "__main__":
    cli()
'@
    [IO.File]::WriteAllText($entryPoint, $entryPointSource, [Text.UTF8Encoding]::new($false))

    $pyinstallerArgs = @(
        "run", "--project", $backendRoot, "pyinstaller",
        "--noconfirm", "--clean", "--onefile",
        "--name", "modulo",
        "--paths", (Join-Path $backendRoot "src"),
        "--distpath", (Join-Path $staging "dist"),
        "--workpath", (Join-Path $staging "work"),
        "--specpath", (Join-Path $staging "spec"),
        $entryPoint
    )
    $pyinstallerOutput = & $script:uvPath @pyinstallerArgs 2>&1
    $pyinstallerExit = $LASTEXITCODE
    if ($pyinstallerExit -ne 0) {
        $tail = (@($pyinstallerOutput) | Select-Object -Last 20) -join [Environment]::NewLine
        Fail "PyInstaller failed with exit code $pyinstallerExit.$([Environment]::NewLine)$tail"
    }
    $launcherCandidate = Join-Path $staging "dist/modulo.exe"
    if (-not (Test-Path -LiteralPath $launcherCandidate -PathType Leaf)) {
        Fail "PyInstaller produced no launcher at '$launcherCandidate'."
    }

    # 5c. Smoke-run the packaged launcher BEFORE the bundle is assembled, so a
    # launcher that cannot run (e.g. an unsupported Windows runtime) fails closed
    # and never yields a bundle claiming success.
    Invoke-LauncherSmokeCheck -Executable $launcherCandidate -StagingDirectory $staging

    # 5d. Assemble the bundle: clean the guarded output directory, then copy only
    # the real launcher and the intended payload.
    Assert-RemovableDirectory -Path $output
    if (Test-Path -LiteralPath $output) { Remove-Item -LiteralPath $output -Recurse -Force }
    New-Item -ItemType Directory -Path $output -Force | Out-Null
    Copy-Item -LiteralPath $launcherCandidate -Destination (Join-Path $output "modulo.exe")

    $frontendDest = Join-Path $output "frontend-dist"
    New-Item -ItemType Directory -Path $frontendDest -Force | Out-Null
    Copy-Item -Path (Join-Path $frontendDist "*") -Destination $frontendDest -Recurse -Force

    $serviceSources = [ordered]@{
        postgresql = $PostgresDirectory
        redis = $RedisDirectory
    }
    foreach ($service in @("postgresql", "redis")) {
        $relative = ([string]$inventory[$service]["relative_path"]).Replace("/", "\")
        $destination = Join-Path $output (Split-Path -Parent $relative)
        New-Item -ItemType Directory -Path $destination -Force | Out-Null
        Copy-Item -Path (Join-Path $serviceSources[$service] "*") -Destination $destination -Recurse -Force
    }

    # 5e. Bundle hygiene: no PyInstaller scaffolding may have reached the bundle
    # (the forbidden list is the validator's, so there is one source for it).
    $hygiene = Invoke-BackendPython -Arguments @($validatorPath, "--check-bundle", $output)
    if ($hygiene.ExitCode -ne 0) {
        Fail "bundle hygiene check failed (exit $($hygiene.ExitCode)): $($hygiene.Output -join ' ')"
    }

    # 5f. Manifest, then validate it against the schema and the emitted files.
    $nativeMetadata = [ordered]@{}
    foreach ($service in @("postgresql", "redis")) {
        $relative = ([string]$inventory[$service]["relative_path"]).Replace("/", "\")
        $metadata = [ordered]@{}
        foreach ($bin in @($inventory[$service]["binaries"])) {
            $metadata[[string]$bin] = Get-BinaryMetadata (Join-Path $output (Join-Path $relative ([string]$bin)))
        }
        $nativeMetadata[$service] = $metadata
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
                relative_path = [string]$inventory["postgresql"]["relative_path"]
                binaries = @($inventory["postgresql"]["binaries"])
                metadata = $nativeMetadata["postgresql"]
            }
            redis = [ordered]@{
                required = $true
                relative_path = [string]$inventory["redis"]["relative_path"]
                binaries = @($inventory["redis"]["binaries"])
                metadata = $nativeMetadata["redis"]
            }
        }
        reproducibility = [ordered]@{
            tool_versions = [ordered]@{
                pnpm = [string]$toolchain["pnpm"]
                python = [string]$toolchain["backend_python"]
                pyinstaller = [string]$toolchain["backend_pyinstaller"]
            }
            validated_at_utc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
        }
    }
    $manifestPath = Join-Path $output "modulo-runtime.json"
    $manifestJson = $manifest | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($manifestPath, $manifestJson, [Text.UTF8Encoding]::new($false))

    $validation = Invoke-BackendPython -Arguments @($validatorPath, "--manifest", $manifestPath, "--schema", $schemaPath, "--bundle", $output)
    if ($validation.ExitCode -ne 0) {
        Fail "bundle manifest validation failed (exit $($validation.ExitCode)): $($validation.Output -join ' ')"
    }

    Write-Output "Windows bundle created: $output"
    Write-Output "Manifest: $manifestPath"
} finally {
    # The staging directory was created by this run, outside the repo.
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
}
