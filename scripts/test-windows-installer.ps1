#Requires -Version 5.1
<#
.SYNOPSIS
Gate deploy/windows/modulo.iss on a real Inno Setup compile.

.DESCRIPTION
FAR-2319: the Windows installer source was reviewed by humans who cannot run
the Inno Setup compiler in their heads, and scripts/test-windows-installer.ps1
existed but no CI job invoked it. This script makes the compiler the reviewer.

It runs three checks against temporary fixture bundles (nothing is written into
the repository):

  1. FAIL-CLOSED PRESENCE: an empty bundle must NOT compile. This preserves the
     installer's existing fail-closed bundle-presence contract, whatever the
     current bundle layout is.
  2. POSITIVE: the real installer source must compile against a minimal,
     structurally-valid fixture bundle, and must produce an installer artifact.
     It then asserts the compiled source's service Parameters double-quote the
     service executable and contain no backslash-escaped quotes (Inno Setup
     rejects \" in parameter values - only "" is valid).
  3. PROVE-THE-FIX (negative): a copy of the same source whose service
     Parameters use backslash-escaped quotes MUST FAIL to compile. Without this
     the gate cannot discriminate between a valid installer and a broken one.

This is a COMPILE-LEVEL gate only. It never manufactures a runnable Modulo
product bundle and never claims a bundle exists. The installer still refuses to
compile unless its required bundle inputs are present.

The script fails closed: if the Inno Setup command-line compiler cannot be
found it throws and never skips or passes vacuously.

Run from anywhere; paths resolve from the repository root. Set ISCC_PATH to
override compiler discovery. Inno Setup 6 and 7 under Program Files are both
recognised.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '0.0.0',

    [Parameter(Mandatory = $false)]
    [ValidateSet('amd64')]
    [string]$Arch = 'amd64',

    # Override the installer source. CI ALWAYS uses the default (the real
    # deploy/windows/modulo.iss). The override exists only so the gate's own
    # positive/negative discrimination can be proven against a corrected copy.
    [Parameter(Mandatory = $false)]
    [string]$IssPath
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$defaultIssPath = Join-Path $repoRoot 'deploy\windows\modulo.iss'
$selfTestMode = $false
if ([string]::IsNullOrWhiteSpace($IssPath)) {
    $IssPath = $defaultIssPath
}
else {
    $IssPath = (Resolve-Path -LiteralPath $IssPath).Path
    $selfTestMode = $IssPath -ne (Resolve-Path -LiteralPath $defaultIssPath).Path
}
if (-not (Test-Path -LiteralPath $IssPath -PathType Leaf)) {
    throw "Installer source not found: $IssPath"
}

function Resolve-Iscc {
    $override = $env:ISCC_PATH
    if (-not [string]::IsNullOrWhiteSpace($override)) {
        if (-not (Test-Path -LiteralPath $override -PathType Leaf)) {
            throw "ISCC_PATH is set to '$override' but no file exists there. Failing closed."
        }
        return (Resolve-Path -LiteralPath $override).Path
    }

    $searchRoots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    $candidates = foreach ($major in @('7', '6')) {
        foreach ($root in $searchRoots) {
            Join-Path $root "Inno Setup $major\ISCC.exe"
        }
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw (
        'Inno Setup command-line compiler (ISCC.exe) was not found under ' +
        '"Inno Setup 7" or "Inno Setup 6" in Program Files. Install Inno Setup ' +
        '6.2+ (or 7) or set ISCC_PATH. This gate fails closed: it never skips ' +
        'and never passes without a real compile.'
    )
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

# A copy of the installer source in a throwaway tree that mirrors the relative
# paths it uses ({#SourcePath}\..\..\LICENSE, {#SourcePath}\INSTALLER-INFO.txt).
function New-IssMirror {
    param(
        [Parameter(Mandatory = $true)][string]$DestinationRoot,
        [Parameter(Mandatory = $true)][string]$SourceIss
    )
    $mirrorWindows = Join-Path $DestinationRoot 'deploy\windows'
    New-Item -ItemType Directory -Path $mirrorWindows -Force | Out-Null

    $licensePath = Join-Path $repoRoot 'LICENSE'
    if (-not (Test-Path -LiteralPath $licensePath -PathType Leaf)) {
        throw "Cannot find LICENSE at $licensePath (required by LicenseFile={#SourcePath}\..\..\LICENSE)."
    }
    Copy-Item -LiteralPath $licensePath -Destination (Join-Path $DestinationRoot 'LICENSE')

    $repoWindowsDir = Join-Path $repoRoot 'deploy\windows'
    $supportFiles = @()
    if (Test-Path -LiteralPath $repoWindowsDir -PathType Container) {
        $supportFiles += Get-ChildItem -LiteralPath $repoWindowsDir -File
    }
    $sourceDir = Split-Path -Parent $SourceIss
    if ((Resolve-Path -LiteralPath $sourceDir).Path -ne (Resolve-Path -LiteralPath $repoWindowsDir).Path) {
        $supportFiles += Get-ChildItem -LiteralPath $sourceDir -File
    }
    foreach ($file in $supportFiles) {
        if ($file.Extension -eq '.iss') {
            continue
        }
        Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $mirrorWindows $file.Name) -Force
    }

    return $mirrorWindows
}

function Invoke-Iscc {
    param(
        [Parameter(Mandatory = $true)][string]$IssFile,
        [Parameter(Mandatory = $true)][string]$BundleDir,
        [Parameter(Mandatory = $true)][string]$OutputName
    )
    $arguments = @(
        '/Qp',
        "/O$script:outputDir",
        "/F$OutputName",
        "/DVERSION_NUM=$Version",
        "/DBUNDLE_ARCH=$Arch",
        "/DBUNDLE_DIR=$BundleDir",
        '/DMODULO_URL=http://127.0.0.1:8000',
        $IssFile
    )
    # Windows PowerShell 5.1 turns a native command's redirected stderr into a
    # terminating error while $ErrorActionPreference is 'Stop', so relax it for
    # the compiler invocation only. The compiler's exit code is the contract.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $compilerOutput = & $script:isccPath @arguments 2>&1
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    $exitCode = [int]$LASTEXITCODE
    $script:lastCompilerOutput = @($compilerOutput | ForEach-Object { [string]$_ })
    # Host stream, not the success stream: the only value this function returns
    # is the compiler exit code.
    $script:lastCompilerOutput | ForEach-Object { Write-Host $_ }
    return $exitCode
}

$script:isccPath = Resolve-Iscc
$script:outputDir = $null
$script:lastCompilerOutput = @()
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('modulo-inno-compile-' + [guid]::NewGuid().ToString('N'))
$emptyBundle = Join-Path $tempRoot 'empty-bundle'
$validBundle = Join-Path $tempRoot 'valid-bundle'
$outputDir = Join-Path $tempRoot 'output'
$mirrorRoot = Join-Path $tempRoot 'mirror'

try {
    New-Item -ItemType Directory -Path $emptyBundle, $validBundle, $outputDir, $mirrorRoot -Force | Out-Null
    $script:outputDir = $outputDir

    # A structurally-valid fixture bundle. The installer does not validate PE
    # semantics, so placeholders are honest here: this is a compile-level gate,
    # not a runtime bundle. The fixture carries the union of the native bundle
    # inputs the installer source has required across the current layout
    # transition, plus one unexcluded payload file so the [Files] wildcard
    # always has something to copy.
    $itemFiles = @(
        'modulo.exe',
        'modulo-runtime.json',
        'launcher',
        'RELEASE_MANIFEST.json',
        'RELEASE_MANIFEST.json.sig'
    )
    foreach ($name in $itemFiles) {
        Write-Utf8NoBom -Path (Join-Path $validBundle $name) -Content 'compile-gate fixture (not a runnable runtime)'
    }
    $payloadDirs = @('runtime', 'pg', 'redis', 'bin', 'frontend-dist', 'backend\.venv')
    foreach ($dir in $payloadDirs) {
        $full = Join-Path $validBundle $dir
        New-Item -ItemType Directory -Path $full -Force | Out-Null
        Write-Utf8NoBom -Path (Join-Path $full 'fixture.txt') -Content 'compile-gate fixture'
    }
    Write-Utf8NoBom -Path (Join-Path $validBundle 'payload.txt') -Content 'compile-gate fixture'

    Write-Output '=== Modulo Windows installer compile gate ==='
    Write-Output "Inno Setup: $script:isccPath"
    Write-Output "Installer source: $IssPath"
    Write-Output "Fixture bundle: $validBundle"
    if ($selfTestMode) {
        Write-Output "SELF-TEST MODE: compiling '$IssPath' instead of the repository installer source."
    }

    # 1. Fail-closed presence contract.
    Write-Output 'Asserting an empty bundle fails closed...'
    $emptyExit = Invoke-Iscc -IssFile $IssPath -BundleDir $emptyBundle -OutputName 'empty-bundle'
    Write-Output "Empty bundle compile exit code: $emptyExit"
    if ($emptyExit -eq 0) {
        throw 'Installer compiled against an empty bundle; the fail-closed bundle-presence contract is broken.'
    }

    # 2. Positive: the real installer source must compile.
    Write-Output 'Compiling the installer against a structurally-valid fixture bundle...'
    $validExit = Invoke-Iscc -IssFile $IssPath -BundleDir $validBundle -OutputName 'valid-contract'
    Write-Output "Valid bundle compile exit code: $validExit"
    if ($validExit -ne 0) {
        $hint = ''
        if (($script:lastCompilerOutput -join "`n") -match 'is missing: \{\#') {
            # Verified on Inno Setup 6.7.4 and 7.1.1: ISPP does not expand an
            # inline directive ({#...}) inside a preprocessor directive, so a
            # guard written #ifexist "{#BUNDLE_DIR}\file" compares against the
            # literal text and can never match - the compiler then reports a
            # present file as missing. Name the real cause instead of leaving
            # the misleading compiler message alone.
            $hint = (
                ' The compiler reported a required bundle input as missing while ' +
                'printing a literal "{#...}" macro. ISPP does not expand inline ' +
                'directives inside preprocessor directives, so a guard written ' +
                '#ifexist "{#BUNDLE_DIR}\file" can never match. Use the expression ' +
                'form instead: #ifexist BUNDLE_DIR + "\file".'
            )
        }
        throw ('Installer source failed to compile against a structurally-valid fixture bundle.' + $hint)
    }
    $artifact = Join-Path $outputDir 'valid-contract.exe'
    if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
        throw "Compile reported success but produced no installer artifact at $artifact."
    }
    Write-Output "Installer artifact: $artifact"

    # 3. Service Parameters must use doubled quotes.
    $issText = [System.IO.File]::ReadAllText($IssPath, [System.Text.Encoding]::UTF8)
    $sourceLines = @($issText -split "\r?\n")
    $serviceIndex = -1
    for ($i = 0; $i -lt $sourceLines.Count; $i++) {
        $line = $sourceLines[$i]
        if ($line -match '(?i)^\s*Filename:\s*"sc\.exe"' -and
            $line -match '(?i)\bParameters:' -and
            $line -match '(?i)\bcreate\s+Modulo\b') {
            $serviceIndex = $i
            break
        }
    }
    if ($serviceIndex -lt 0) {
        throw 'Installer source has no [Run] "sc.exe" "create Modulo" entry to validate.'
    }
    $serviceLine = $sourceLines[$serviceIndex]
    Write-Output "Service Parameters: $serviceLine"
    if ($serviceLine -notmatch 'binPath=\s+""[^"]+""') {
        throw (
            'Service Parameters must double-quote the service executable ' +
            '(binPath= ""...""). Inno Setup rejects single-level quoting for a ' +
            'path containing spaces.'
        )
    }
    if ($serviceLine.Contains('\"')) {
        throw (
            'Service Parameters contain backslash-escaped quotes (\"). Inno ' +
            'Setup rejects them with "Mismatched or misplaced quotes on ' +
            'parameter Parameters"; use doubled quotes ("").'
        )
    }

    # 4. Prove-the-fix: the malformed form must NOT compile.
    $malformedLines = [string[]]$sourceLines.Clone()
    $malformedLines[$serviceIndex] = $serviceLine.Replace('""', '\"')
    if ($malformedLines[$serviceIndex] -eq $serviceLine) {
        throw 'Could not construct the malformed Parameters fixture; the gate cannot prove it discriminates.'
    }
    $mirrorWindows = New-IssMirror -DestinationRoot $mirrorRoot -SourceIss $IssPath
    $malformedIss = Join-Path $mirrorWindows 'malformed-parameters.iss'
    Write-Utf8NoBom -Path $malformedIss -Content ($malformedLines -join "`r`n")

    Write-Output 'Compiling a deliberately malformed installer source (backslash-escaped Parameters)...'
    $malformedExit = Invoke-Iscc -IssFile $malformedIss -BundleDir $validBundle -OutputName 'malformed-parameters'
    Write-Output "Malformed Parameters compile exit code: $malformedExit"
    if ($malformedExit -eq 0) {
        throw (
            'The gate cannot discriminate: an installer whose service Parameters ' +
            'use backslash-escaped quotes compiled successfully.'
        )
    }
    Write-Output 'Compiler rejected the malformed Parameters as expected.'

    Write-Output 'PASS: empty bundle fails closed.'
    Write-Output 'PASS: the installer source compiles and produces an artifact.'
    Write-Output 'PASS: service Parameters double-quote the executable and contain no backslash-escaped quotes.'
    Write-Output 'PASS: a malformed Parameters value fails to compile (gate discriminates).'
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
