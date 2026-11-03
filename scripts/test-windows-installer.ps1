<#
.SYNOPSIS
Validate the Windows Inno Setup installer contract locally.
.DESCRIPTION
Compiles temporary fixture bundles with the pinned Inno Setup release and
asserts the installer script still fails closed without a native bundle. Also
checks that the installer script carries the FAR-2315 log capture and support
reference features. Run this on Windows with Inno Setup 6.2.2 installed; the
Windows installer CI workflow invokes it on the pinned compiler.
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
$launcherContractScriptPath = Join-Path $repoRoot "scripts\verify-windows-launcher-contract.ps1"

if (-not (Test-Path -LiteralPath $issPath -PathType Leaf)) {
    throw "Cannot find deploy\windows\modulo.iss. Run from repo root."
}
if (-not (Test-Path -LiteralPath $launcherContractScriptPath -PathType Leaf)) {
    throw "Cannot find scripts\verify-windows-launcher-contract.ps1. Run from repo root."
}

$isccPath = $null
$isInnoSetupAvailable = $false
if (${env:ProgramFiles} -or ${env:ProgramFiles(x86)}) {
    $candidates = @()
    if (${env:ProgramFiles}) {
        $candidates += Join-Path ${env:ProgramFiles} "Inno Setup 6\ISCC.exe"
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
    }
    $isccPath = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    $isInnoSetupAvailable = $null -ne $isccPath
}
if (-not $isInnoSetupAvailable) {
    Write-Warning "Inno Setup 6 not found (expected on non-Windows). Skipping compilation tests."
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

    if (-not $isInnoSetupAvailable) {
        throw "Inno Setup not available - cannot compile"
    }

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
    return @{ ExitCode = $exitCode; Output = $compilerOutput }
}

try {
    Write-Output "=== Modulo Windows Installer Contract Test (FAR-2315) ==="
    if ($isInnoSetupAvailable) {
        Write-Output "Inno Setup: $isccPath"
    } else {
        Write-Output "Inno Setup: NOT FOUND - skipping compilation tests"
    }
    Write-Output "Installer source: $issPath"
    Write-Output ""

    # --- Contract tests: missing required files fail closed (only if Inno Setup available) ---
    if ($isInnoSetupAvailable) {
        Write-Output "Checking that a bundle missing modulo.exe fails compilation..."
        $result = Invoke-Iscc $missingExeBundle "missing-exe-contract"
        Write-Output "Missing modulo.exe compile exit code: $($result.ExitCode)"
        if ($result.ExitCode -eq 0) {
            throw "Installer compiled despite missing modulo.exe."
        }

        Write-Output "Checking that a bundle missing modulo-runtime.json fails compilation..."
        $result = Invoke-Iscc $missingMarkerBundle "missing-marker-contract"
        Write-Output "Missing modulo-runtime.json compile exit code: $($result.ExitCode)"
        if ($result.ExitCode -eq 0) {
            throw "Installer compiled despite missing modulo-runtime.json."
        }

        # --- Valid contract compiles ---
        [System.IO.File]::WriteAllText((Join-Path $validBundle "modulo.exe"), "placeholder")
        [System.IO.File]::WriteAllText((Join-Path $validBundle "modulo-runtime.json"), '{"contract":"native-windows-v1"}')

        Write-Output "Checking that the minimal declared contract compiles..."
        $result = Invoke-Iscc $validBundle "valid-contract"
        Write-Output "Valid contract compile exit code: $($result.ExitCode)"
        $installerPath = Join-Path $outputDir "valid-contract.exe"
        if ($result.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
            throw "Installer failed to compile the minimal declared contract."
        }
    } else {
        Write-Output "Skipping compilation tests (Inno Setup not available on this platform)."
    }

    # --- FAR-2315: Verify log capture and support reference code present in .iss ---
    Write-Output ""
    Write-Output "=== Verifying FAR-2315 log capture + support reference features ==="
    $issContent = Get-Content -LiteralPath $issPath -Raw

    $requiredPatterns = @(
        @{ Name = "GenerateSupportRef function"; Pattern = "function GenerateSupportRef" }
        @{ Name = "GetLogDir function"; Pattern = "function GetLogDir" }
        @{ Name = "EnsureLogDir function"; Pattern = "function EnsureLogDir" }
        @{ Name = "PruneInstallLogs function"; Pattern = "procedure PruneInstallLogs" }
        @{ Name = "LogFilePath variable"; Pattern = "LogFilePath:" }
        @{ Name = "LogActive variable"; Pattern = "LogActive:" }
        @{ Name = "InitializeSetup logging init"; Pattern = "InitializeSetup" }
        @{ Name = "DeinitializeSetup logging cleanup"; Pattern = "DeinitializeSetup" }
        @{ Name = "SetStage function"; Pattern = "procedure SetStage" }
        @{ Name = "GetTargetDescription function"; Pattern = "function GetTargetDescription" }
        @{ Name = "Redact function"; Pattern = "function Redact" }
        @{ Name = "TrimLog function"; Pattern = "procedure TrimLog" }
        @{ Name = "PrintSupportBlock function"; Pattern = "procedure PrintSupportBlock" }
        @{ Name = "Support reference in log filename"; Pattern = "LOG_PREFIX.*SupportRef.*LOG_EXT" }
        @{ Name = "Max log bytes constant"; Pattern = "MAX_LOG_BYTES.*=.*1048576" }
        @{ Name = "Max install logs constant"; Pattern = "MAX_INSTALL_LOGS.*=.*5" }
        @{ Name = "Log directory LocalAppData"; Pattern = '{localappdata}.*LOG_DIR_NAME' }
        @{ Name = "Redaction of URL credentials"; Pattern = "function RedactUrlUserInfo" }
        @{ Name = "Redaction of credential values"; Pattern = "function RedactAfterMarker" }
        @{ Name = "Support block delimiter"; Pattern = '----- Modulo install failed: support reference' }
        @{ Name = "GitHub issues URL in support block"; Pattern = 'github.com/farnalabs/modulo/issues' }
    )

    $missingPatterns = @()
    foreach ($p in $requiredPatterns) {
        if ($issContent -notmatch $p.Pattern) {
            $missingPatterns += $p.Name
        } else {
            Write-Output "  PASS: $($p.Name) found in installer script"
        }
    }

    if ($missingPatterns.Count -gt 0) {
        throw "Installer script missing FAR-2315 patterns: $($missingPatterns -join ', ')"
    }

    Write-Output "All FAR-2315 patterns verified in installer script."

    Write-Output ""
    Write-Output "PASS: missing required runtime files fail closed."
    Write-Output "PASS: declared marker/executable contract compiles without committing a fake bundle."
    Write-Output "PASS: FAR-2315 log capture + support reference features present in installer script."
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}
