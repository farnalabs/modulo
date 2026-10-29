<#
.SYNOPSIS
Test Windows Inno Setup installer compilation locally
.DESCRIPTION
Validates that the Inno Setup script compiles and produces a valid installer.
Run this on a Windows machine with Inno Setup 6.2+ installed.
#>

param(
    [Parameter(Mandatory=$false)]
    [string]$Version = "0.3.323",
    
    [Parameter(Mandatory=$false)]
    [string]$Arch = "amd64",
    
    [Parameter(Mandatory=$false)]
    [string]$BundleDir = "."
)

# Check if Inno Setup is installed
$isccPath = "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $isccPath)) {
    $isccPath = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
}
if (-not (Test-Path $isccPath)) {
    Write-Error "Inno Setup 6 not found. Install from https://jrsoftware.org/isdl.php"
    exit 1
}

# Verify we're in the right place
$issPath = "deploy\windows\modulo.iss"
if (-not (Test-Path $issPath)) {
    Write-Error "Cannot find deploy\windows\modulo.iss. Run from repo root."
    exit 1
}

Write-Host "=== Modulo Windows Installer Local Compilation Test ==="
Write-Host "Version: $Version"
Write-Host "Architecture: $Arch"
Write-Host "Bundle Dir: $BundleDir"
Write-Host "Inno Setup: $isccPath"
Write-Host ""

# Compile with Inno Setup
$issPathFull = Resolve-Path "deploy\windows\modulo.iss"
$outputDir = "build\windows"
$outputFile = "modulo-${Version}-windows-${Arch}.exe"

Write-Host "Compiling with Inno Setup..."
Write-Host "ISS: $issPathFull"
Write-Host "Output: $outputDir\$outputFile"
Write-Host ""

$cmd = @(
    "iscc",
    "/Qp",
    "/O$outputDir",
    "/Fmodulo-${Version}-windows-${Arch}",
    "/DVERSION_NUM=$Version",
    "/DBUNDLE_ARCH=$Arch",
    "/DBUNDLE_DIR=$BundleDir",
    $issPathFull.FullName
)

Write-Host "Running: $($cmd -join ' ')"
Write-Host ""

$result = & $cmd
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    $exePath = "build\windows\modulo-${Version}-windows-${Arch}.exe"
    if (Test-Path $exePath) {
        $size = (Get-Item $exePath).Length
        $sizeMB = [math]::Round($size / 1MB, 2)
        Write-Host ""
        Write-Host "✅ SUCCESS! Installer compiled successfully."
        Write-Host "Output: $(Resolve-Path $exePath)"
        Write-Host "Size: ${sizeMB} MB"
        
        # Quick validation
        Write-Host ""
        Write-Host "Validating installer..."
        $verify = & cmd /c "`"$exePath`" /?" 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "✅ Installer runs and shows help"
        } else {
            Write-Warning "Installer may not run correctly (exit code: $LASTEXITCODE)"
        }
    } else {
        Write-Error "Compiler succeeded but output file not found"
        exit 1
    }
} else {
    Write-Error "Compilation failed with exit code $exitCode"
    exit 1
}

Write-Host ""
Write-Host "=== Test Complete ==="
