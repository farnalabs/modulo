[CmdletBinding()]
param(
    [string[]]$TestPath = @()
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$pesterVersion = "5.7.1"
$pesterPackageSha256 = "4A27904C6814A5FBE4758F8E49861F6A1994AEE77B71165A5C43C0371BA6C580"
$pesterPackageUrl = "https://www.powershellgallery.com/api/v2/package/Pester/$pesterVersion"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$cacheRoot = if ($env:MODULO_TOOL_CACHE) {
    $env:MODULO_TOOL_CACHE
} else {
    Join-Path $repoRoot ".tool-cache"
}
$moduleRoot = Join-Path $cacheRoot "powershell/Pester/$pesterVersion"
$moduleManifest = Join-Path $moduleRoot "Pester.psd1"

function Install-PinnedPester {
    if (Test-Path -LiteralPath $moduleManifest) {
        $cachedVersion = (Test-ModuleManifest -Path $moduleManifest).Version.ToString()
        if ($cachedVersion -eq $pesterVersion) {
            return
        }
        throw "Cached Pester version is $cachedVersion; expected $pesterVersion. Remove '$moduleRoot' and retry."
    }

    $parent = Split-Path -Parent $moduleRoot
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $stagingRoot = Join-Path $parent (".{0}.staging-{1}" -f $pesterVersion, [guid]::NewGuid().ToString("N"))
    $packagePath = Join-Path $stagingRoot "Pester.$pesterVersion.nupkg"
    $extractPath = Join-Path $stagingRoot "module"

    try {
        New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
        Write-Host "Downloading pinned Pester $pesterVersion..."
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $pesterPackageUrl -OutFile $packagePath

        $actualHash = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash
        if ($actualHash -ne $pesterPackageSha256) {
            throw "Pester package hash mismatch. Expected $pesterPackageSha256, got $actualHash."
        }

        New-Item -ItemType Directory -Path $extractPath -Force | Out-Null
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($packagePath, $extractPath)
        $extractedManifest = Join-Path $extractPath "Pester.psd1"
        if (-not (Test-Path -LiteralPath $extractedManifest)) {
            throw "Downloaded Pester package did not contain Pester.psd1."
        }
        if ((Test-ModuleManifest -Path $extractedManifest).Version.ToString() -ne $pesterVersion) {
            throw "Downloaded Pester package has an unexpected module version."
        }

        if (Test-Path -LiteralPath $moduleRoot) {
            Remove-Item -LiteralPath $moduleRoot -Recurse -Force
        }
        Move-Item -LiteralPath $extractPath -Destination $moduleRoot
    } finally {
        if (Test-Path -LiteralPath $stagingRoot) {
            Remove-Item -LiteralPath $stagingRoot -Recurse -Force
        }
    }
}

Install-PinnedPester
Remove-Module Pester -Force -ErrorAction SilentlyContinue
Import-Module $moduleManifest -Force -ErrorAction Stop
$loadedVersion = (Get-Module Pester).Version.ToString()
if ($loadedVersion -ne $pesterVersion) {
    throw "Loaded Pester $loadedVersion instead of pinned version $pesterVersion."
}

if ($TestPath.Count -eq 0) {
    $testFiles = @(
        Get-ChildItem -LiteralPath (Join-Path $repoRoot "tools/tests") -File -Filter "*.ps1" |
            Sort-Object FullName |
            Select-Object -ExpandProperty FullName
    )
} else {
    $testFiles = @(
        foreach ($pathArgument in $TestPath) {
            foreach ($path in ($pathArgument -split ",")) {
                $candidate = if ([IO.Path]::IsPathRooted($path)) { $path } else { Join-Path $repoRoot $path }
                (Resolve-Path -LiteralPath $candidate).Path
            }
        }
    )
}

if ($testFiles.Count -eq 0) {
    throw "No Pester test files were found."
}

Write-Host "Running $($testFiles.Count) PowerShell test suite(s) with Pester $loadedVersion."
$configuration = New-PesterConfiguration
$configuration.Run.Path = $testFiles
$configuration.Run.PassThru = $true
$configuration.Output.Verbosity = "Detailed"
$result = Invoke-Pester -Configuration $configuration

if (
    $result.Result -ne "Passed" -or
    $result.FailedCount -gt 0 -or
    $result.SkippedCount -gt 0 -or
    $result.NotRunCount -gt 0
) {
    Write-Error (
        "Pester failed: result={0}, failed={1}, skipped={2}, not-run={3}" -f
        $result.Result, $result.FailedCount, $result.SkippedCount, $result.NotRunCount
    )
    exit 1
}

Write-Host "Pester passed: $($result.PassedCount) tests."
exit 0
