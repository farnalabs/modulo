<#
.SYNOPSIS
Validate the Windows Inno Setup installer contract locally.
.DESCRIPTION
Runs two compile checks against temporary fixture bundles: a bundle missing
required runtime files must fail, while a minimal contract fixture containing
modulo.exe and modulo-runtime.json may compile. The fixture and installer are
never written to the repository.
Run this on Windows with Inno Setup 6.2+ installed.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = "0.3.323",

    [Parameter(Mandatory = $false)]
    [ValidateSet("amd64")]
    [string]$Arch = "amd64"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Get-Location).Path
$issPath = Join-Path $repoRoot "deploy\windows\modulo.iss"
if (-not (Test-Path -LiteralPath $issPath -PathType Leaf)) {
    throw "Cannot find deploy\windows\modulo.iss. Run from repo root."
}

$isccCandidates = @(
    (Join-Path ${env:ProgramFiles} "Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
)
$isccPath = $isccCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $isccPath) {
    throw "Inno Setup 6 not found. Install from https://jrsoftware.org/isdl.php"
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("modulo-installer-contract-" + [guid]::NewGuid().ToString("N"))
$missingExeBundle = Join-Path $tempRoot "missing-exe"
$missingMarkerBundle = Join-Path $tempRoot "missing-marker"
$validBundle = Join-Path $tempRoot "valid"
$outputDir = Join-Path $tempRoot "output"
New-Item -ItemType Directory -Path $missingExeBundle, $missingMarkerBundle, $validBundle, $outputDir | Out-Null

[System.IO.File]::WriteAllText((Join-Path $missingExeBundle "modulo-runtime.json"), '{"contract":"native-windows-v1"}')
[System.IO.File]::WriteAllText((Join-Path $missingMarkerBundle "modulo.exe"), "placeholder")

function Invoke-Iscc {
    param(
        [Parameter(Mandatory = $true)]
        [string]$bundleDir,
        [Parameter(Mandatory = $true)]
        [string]$outputName
    )

    $arguments = @(
        "/Qp",
        "/O$outputDir",
        "/F$outputName",
        "/DVERSION_NUM=$Version",
        "/DBUNDLE_ARCH=$Arch",
        "/DBUNDLE_DIR=$bundleDir",
        "/DMODULO_URL=http://127.0.0.1:8000",
        $issPath
    )
    $compilerOutput = & $isccPath @arguments 2>&1
    $exitCode = [int]$LASTEXITCODE
    $compilerOutput | ForEach-Object { Write-Information -MessageData $_ -InformationAction Continue }
    return $exitCode
}

try {
    Write-Output "=== Modulo Windows Installer Contract Test ==="
    Write-Output "Inno Setup: $isccPath"
    Write-Output "Installer source: $issPath"

    Write-Output "Checking that a bundle missing modulo.exe fails compilation..."
    $missingExeExit = Invoke-Iscc $missingExeBundle "missing-exe-contract"
    Write-Output "Missing modulo.exe compile exit code: $missingExeExit"
    if ($missingExeExit -eq 0) {
        throw "Installer compiled despite missing modulo.exe."
    }

    Write-Output "Checking that a bundle missing modulo-runtime.json fails compilation..."
    $missingMarkerExit = Invoke-Iscc $missingMarkerBundle "missing-marker-contract"
    Write-Output "Missing modulo-runtime.json compile exit code: $missingMarkerExit"
    if ($missingMarkerExit -eq 0) {
        throw "Installer compiled despite missing modulo-runtime.json."
    }

    # Inno Setup validates source presence, not PE semantics. This deliberately
    # minimal fixture proves the installer contract without pretending to be a
    # runnable native runtime.
    [System.IO.File]::WriteAllText((Join-Path $validBundle "modulo.exe"), "placeholder")
    [System.IO.File]::WriteAllText((Join-Path $validBundle "modulo-runtime.json"), '{"contract":"native-windows-v1"}')

    Write-Output "Checking that the minimal declared contract compiles..."
    $validExit = Invoke-Iscc $validBundle "valid-contract"
    Write-Output "Valid contract compile exit code: $validExit"
    $installerPath = Join-Path $outputDir "valid-contract.exe"
    if ($validExit -ne 0 -or -not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
        throw "Installer failed to compile the minimal declared contract."
    }

    Write-Output "PASS: missing required runtime files fail closed."
    Write-Output "PASS: declared marker/executable contract compiles without committing a fake bundle."
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}
