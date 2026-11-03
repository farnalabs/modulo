<#
.SYNOPSIS
Validate the Windows Inno Setup installer contract.

.DESCRIPTION
Two independent proofs, plus a supplementary fast signal:

  1. COMPILE (Windows + Inno Setup 6.2.2): ISCC compiles deploy/windows/modulo.iss
     against fixture bundles. A bundle missing modulo.exe or modulo-runtime.json
     must FAIL to compile; a complete bundle must compile and produce an installer.
  2. REDACTION (any OS + Free Pascal): the Redact() source is extracted verbatim
     from modulo.iss, compiled with fpc and executed against credential fixtures.
     This proves the function removes credentials rather than merely mentioning
     a redaction pattern in the file.
  3. Supplementary text checks only ever run alongside a proof - a text match is
     never accepted as evidence that the script works.

In CI at least one compiler must be present or the run fails; a silent "nothing
was verified" pass is the defect this script exists to prevent.
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

if ($PSScriptRoot) {
    $repoRoot = Split-Path -Parent $PSScriptRoot
} else {
    $repoRoot = (Get-Location).Path
}

$issPath = [System.IO.Path]::Combine($repoRoot, 'deploy', 'windows', 'modulo.iss')
$launcherScriptPath = [System.IO.Path]::Combine($repoRoot, 'scripts', 'create-windows-launcher.ps1')

if (-not (Test-Path -LiteralPath $issPath -PathType Leaf)) {
    throw "Cannot find deploy/windows/modulo.iss under $repoRoot"
}
if (-not (Test-Path -LiteralPath $launcherScriptPath -PathType Leaf)) {
    throw "Cannot find scripts/create-windows-launcher.ps1 under $repoRoot"
}

$proofsRun = @()

# --- Locate compilers -------------------------------------------------------

$isccPath = $null
$isccCandidates = @()
if ($env:ProgramFiles) {
    $isccCandidates += [System.IO.Path]::Combine($env:ProgramFiles, 'Inno Setup 6', 'ISCC.exe')
}
if (${env:ProgramFiles(x86)}) {
    $isccCandidates += [System.IO.Path]::Combine(${env:ProgramFiles(x86)}, 'Inno Setup 6', 'ISCC.exe')
}
$isccCommand = Get-Command -Name 'ISCC.exe' -ErrorAction SilentlyContinue
if ($isccCommand) {
    $isccCandidates += $isccCommand.Source
}
foreach ($candidate in $isccCandidates) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $isccPath = $candidate
        break
    }
}

$fpcCommand = Get-Command -Name 'fpc' -ErrorAction SilentlyContinue
$fpcPath = $null
if ($fpcCommand) {
    $fpcPath = $fpcCommand.Source
}

$tempRoot = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), ("modulo-installer-contract-" + [guid]::NewGuid().ToString("N")))
$missingExeBundle = [System.IO.Path]::Combine($tempRoot, 'missing-exe')
$missingMarkerBundle = [System.IO.Path]::Combine($tempRoot, 'missing-marker')
$validBundle = [System.IO.Path]::Combine($tempRoot, 'valid')
$outputDir = [System.IO.Path]::Combine($tempRoot, 'output')
$redactDir = [System.IO.Path]::Combine($tempRoot, 'redact')
$launcherProbeDir = [System.IO.Path]::Combine($tempRoot, 'launcher-probe')

New-Item -ItemType Directory -Path $missingExeBundle, $missingMarkerBundle, $validBundle, $outputDir, $redactDir, $launcherProbeDir -Force | Out-Null

[System.IO.File]::WriteAllText([System.IO.Path]::Combine($missingExeBundle, 'modulo-runtime.json'), '{"contract":"native-windows-v1"}')
[System.IO.File]::WriteAllText([System.IO.Path]::Combine($missingMarkerBundle, 'modulo.exe'), 'placeholder')

function Invoke-Iscc {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BundleDir,
        [Parameter(Mandatory = $true)]
        [string]$OutputName
    )

    if (-not $isccPath) {
        throw "Inno Setup not available - cannot compile"
    }

    $arguments = @(
        "/Qp",
        "/O$outputDir",
        "/F$OutputName",
        "/DVERSION_NUM=$Version",
        "/DBUNDLE_ARCH=$Arch",
        "/DBUNDLE_DIR=$BundleDir",
        "/DMODULO_URL=http://127.0.0.1:8000",
        $issPath
    )
    $compilerOutput = & $isccPath @arguments 2>&1
    $exitCode = [int]$LASTEXITCODE
    $compilerOutput | ForEach-Object { Write-Output $_ }
    return @{ ExitCode = $exitCode; Output = $compilerOutput }
}

function Get-MarkedBlock {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Text,
        [Parameter(Mandatory = $true)]
        [string]$BeginMarker,
        [Parameter(Mandatory = $true)]
        [string]$EndMarker
    )

    $start = $Text.IndexOf($BeginMarker)
    if ($start -lt 0) {
        throw "Marker not found in installer script: $BeginMarker"
    }
    $start = $start + $BeginMarker.Length
    $end = $Text.IndexOf($EndMarker, $start)
    if ($end -lt 0) {
        throw "Marker not found in installer script: $EndMarker"
    }
    return $Text.Substring($start, $end - $start)
}

try {
    Write-Output "=== Modulo Windows Installer Contract Test (FAR-2315) ==="
    if ($isccPath) {
        Write-Output "Inno Setup ISCC: $isccPath"
    } else {
        Write-Output "Inno Setup ISCC: NOT FOUND on this host"
    }
    if ($fpcPath) {
        Write-Output "Free Pascal fpc: $fpcPath"
    } else {
        Write-Output "Free Pascal fpc: NOT FOUND on this host"
    }
    Write-Output "Installer source: $issPath"
    Write-Output ""

    # --- Proof 1: the script compiles, and fails closed on a broken bundle ---
    if ($isccPath) {
        Write-Output "=== Proof 1: ISCC compilation (Inno Setup 6) ==="

        Write-Output "Checking that a bundle missing modulo.exe fails compilation..."
        $result = Invoke-Iscc -BundleDir $missingExeBundle -OutputName 'missing-exe-contract'
        Write-Output "Missing modulo.exe compile exit code: $($result.ExitCode)"
        if ($result.ExitCode -eq 0) {
            throw "Installer compiled despite missing modulo.exe."
        }

        Write-Output "Checking that a bundle missing modulo-runtime.json fails compilation..."
        $result = Invoke-Iscc -BundleDir $missingMarkerBundle -OutputName 'missing-marker-contract'
        Write-Output "Missing modulo-runtime.json compile exit code: $($result.ExitCode)"
        if ($result.ExitCode -eq 0) {
            throw "Installer compiled despite missing modulo-runtime.json."
        }

        [System.IO.File]::WriteAllText([System.IO.Path]::Combine($validBundle, 'modulo.exe'), 'placeholder')
        [System.IO.File]::WriteAllText([System.IO.Path]::Combine($validBundle, 'modulo-runtime.json'), '{"contract":"native-windows-v1"}')

        Write-Output "Checking that the declared bundle contract compiles..."
        $result = Invoke-Iscc -BundleDir $validBundle -OutputName 'valid-contract'
        Write-Output "Valid contract compile exit code: $($result.ExitCode)"
        $installerPath = [System.IO.Path]::Combine($outputDir, 'valid-contract.exe')
        if ($result.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
            throw "Installer failed to compile the declared bundle contract."
        }
        $proofsRun += 'ISCC compile'
        Write-Output "PASS: ISCC compiled the installer and rejected both incomplete bundles."
    } else {
        Write-Warning "ISCC not found: compilation was NOT verified on this host. The windows-installer CI job compiles it on a Windows runner."
    }

    # --- Proof 2: Redact() compiles and actually removes credentials ---------
    if ($fpcPath) {
        Write-Output ""
        Write-Output "=== Proof 2: Redact() compiled and executed (Free Pascal) ==="

        $issText = Get-Content -LiteralPath $issPath -Raw
        $redactBlock = Get-MarkedBlock -Text $issText -BeginMarker '{ BEGIN REDACT-SOURCE }' -EndMarker '{ END REDACT-SOURCE }'

        $programSource = @'
program RedactUnit;
{$mode objfpc}{$H+}
uses SysUtils;
__REDACT_BLOCK__
var
  Failures: Integer;

procedure Check(const Name, InputText, Expected: string);
var
  Actual: string;
begin
  Actual := Redact(InputText);
  if Actual = Expected then
    WriteLn('PASS: ', Name)
  else
  begin
    Failures := Failures + 1;
    WriteLn('FAIL: ', Name);
    WriteLn('  input   : ', InputText);
    WriteLn('  expected: ', Expected);
    WriteLn('  actual  : ', Actual);
  end;
end;

begin
  Failures := 0;
  Check('password key', 'password=hunter2', 'password=[redacted]');
  Check('uppercase key', 'PASSWORD=hunter2', 'PASSWORD=[redacted]');
  Check('colon separator', 'token: abc.def', 'token: [redacted]');
  Check('bearer scheme', 'Authorization: Bearer abc123', 'Authorization: Bearer [redacted]');
  Check('basic scheme', 'authorization: Basic dXNlcjpwYXNz', 'authorization: Basic [redacted]');
  Check('url userinfo', 'https://user:pass@example.com/path', 'https://[redacted]@example.com/path');
  Check('url without creds', 'https://example.com/path', 'https://example.com/path');
  Check('url without password', 'https://user@example.com/path', 'https://user@example.com/path');
  Check('api key', 'api_key=XYZ', 'api_key=[redacted]');
  Check('hyphenated api key', 'x-api-key: 12345', 'x-api-key: [redacted]');
  Check('delimited value', 'secret = s3cr3t, next=1', 'secret = [redacted], next=1');
  Check('nothing to redact', 'nothing to redact here', 'nothing to redact here');
  Check('idempotent', 'password=[redacted]', 'password=[redacted]');
  Check('postgres dsn', 'postgres://svc:hunter2@db.internal:5432/modulo', 'postgres://[redacted]@db.internal:5432/modulo');
  Check('two secrets', 'url=https://u:p@h/x token=abc', 'url=https://[redacted]@h/x token=[redacted]');
  Check('substring not a key', 'tokenizer=bob', 'tokenizer=bob');
  Check('signature', 'signature=abcdef', 'signature=[redacted]');
  Check('client secret', 'client_secret=abc', 'client_secret=[redacted]');
  Check('credentials plural', 'credentials=abc', 'credentials=[redacted]');
  Check('empty value', 'password=', 'password=');
  if Failures > 0 then
  begin
    WriteLn('FAILED: ', Failures, ' case(s)');
    Halt(1);
  end;
  WriteLn('All redaction cases passed.');
end.
'@
        $programSource = $programSource.Replace('__REDACT_BLOCK__', $redactBlock)
        $programPath = [System.IO.Path]::Combine($redactDir, 'redactunit.pas')
        [System.IO.File]::WriteAllText($programPath, $programSource)

        $exePath = [System.IO.Path]::Combine($redactDir, 'redactunit')
        if ($env:OS -eq 'Windows_NT') {
            $exePath = $exePath + '.exe'
        }

        $compileOutput = & $fpcPath "-FE$redactDir" "-FU$redactDir" "-o$exePath" $programPath 2>&1
        $compileExit = [int]$LASTEXITCODE
        if ($compileExit -ne 0) {
            $compileOutput | ForEach-Object { Write-Output $_ }
            throw "Free Pascal failed to compile the extracted Redact() source (exit $compileExit)."
        }
        Write-Output "Redact() source compiled with fpc (exit $compileExit)."

        $runOutput = & $exePath 2>&1
        $runExit = [int]$LASTEXITCODE
        $runOutput | ForEach-Object { Write-Output $_ }
        if ($runExit -ne 0) {
            throw "Redaction fixture cases failed (exit $runExit)."
        }
        $proofsRun += 'Redact unit test'
        Write-Output "PASS: Redact() removes credentials and is idempotent."

        # --- Proof 2b: the WHOLE [Code] section compiles -------------------
        # Compiled against declarations that mirror Inno Setup 6's documented
        # support-function signatures. Any identifier the script uses that is
        # not in that surface fails here, which is the "unknown identifier"
        # class that shipped in the first revision of this script. ISCC is
        # still the authoritative compiler and runs in the windows CI job.
        Write-Output ""
        Write-Output "=== Proof 2b: whole [Code] section compiles against Inno support stubs ==="

        $codeHeader = [regex]::Match($issText, '(?m)^\[Code\][ \t]*$')
        if (-not $codeHeader.Success) {
            throw "Installer script has no [Code] section."
        }
        $codeSection = $issText.Substring($codeHeader.Index + $codeHeader.Length)

        $codeProgram = @'
program CodeSectionCheck;
{$mode objfpc}{$H+}
{$WARN 5024 OFF}
uses SysUtils;

type
  TFindRec = record
    Name: string;
  end;
  TSetupStep = (ssInstall, ssPostInstall, ssDone);
  TExecWait = (ewNoWait, ewWaitUntilTerminated);
  TMsgBoxType = (mbInformation, mbError);
  TStringArray = array of string;

const
  SW_HIDE = 0;
  MB_OK = 0;
  wpWelcome = 1;
  wpLicense = 2;
  wpSelectDir = 3;
  wpSelectTasks = 4;
  wpReady = 5;

function ExpandConstant(const S: string): string; begin Result := S; end;
function SaveStringToFile(const FileName, S: string; Append: Boolean): Boolean; begin Result := True; end;
function FileExists(const Name: string): Boolean; begin Result := False; end;
function DirExists(const Name: string): Boolean; begin Result := False; end;
function ForceDirectories(const Dir: string): Boolean; begin Result := True; end;
function DeleteFile(const Name: string): Boolean; begin Result := True; end;
function FindFirst(const FileName: string; var FindRec: TFindRec): Boolean; begin Result := False; end;
function FindNext(var FindRec: TFindRec): Boolean; begin Result := False; end;
procedure FindClose(var FindRec: TFindRec); begin end;
procedure SetArrayLength(var A: TStringArray; NewLength: Integer); begin SetLength(A, NewLength); end;
function GetDateTimeString(const Fmt: string; const DateSep, TimeSep: Char): string; begin Result := ''; end;
function Exec(const Filename, Params, WorkingDir: string; ShowCmd: Integer; Wait: TExecWait; var ResultCode: Integer): Boolean; begin ResultCode := 0; Result := True; end;
function MsgBox(const Text: string; Typ: TMsgBoxType; Buttons: Integer): Integer; begin Result := 0; end;
function IsUninstaller: Boolean; begin Result := False; end;
procedure Abort; begin Halt(1); end;
__CODE_SECTION__
begin
end.
'@
        $codeProgram = $codeProgram.Replace('__CODE_SECTION__', $codeSection)
        $codeProgramPath = [System.IO.Path]::Combine($redactDir, 'codesection.pas')
        [System.IO.File]::WriteAllText($codeProgramPath, $codeProgram)

        $codeExePath = [System.IO.Path]::Combine($redactDir, 'codesection')
        if ($env:OS -eq 'Windows_NT') {
            $codeExePath = $codeExePath + '.exe'
        }
        $codeCompileOutput = & $fpcPath "-FE$redactDir" "-FU$redactDir" "-o$codeExePath" $codeProgramPath 2>&1
        $codeCompileExit = [int]$LASTEXITCODE
        if ($codeCompileExit -ne 0) {
            $codeCompileOutput | ForEach-Object { Write-Output $_ }
            throw "Free Pascal failed to compile the [Code] section (exit $codeCompileExit)."
        }
        Write-Output "PASS: [Code] section compiles with no unknown identifiers (exit $codeCompileExit)."
        $proofsRun += 'Code section compile'
    } else {
        Write-Warning "fpc not found: Redact() behaviour and the [Code] compile check were NOT verified on this host. The windows-installer CI job runs both proofs on Ubuntu."
    }

    # --- Supplementary text signal (never the proof) -------------------------
    Write-Output ""
    Write-Output "=== Supplementary text checks (fast signal only, not proof) ==="
    $issContent = Get-Content -LiteralPath $issPath -Raw

    $requiredPatterns = @(
        @{ Name = "Pascal Script section"; Pattern = '\[Code\]' }
        @{ Name = "Support reference generator"; Pattern = 'function GenerateSupportRef' }
        @{ Name = "Log directory helper"; Pattern = 'function GetLogDir' }
        @{ Name = "Log directory creation"; Pattern = 'function EnsureLogDir' }
        @{ Name = "Log pruning"; Pattern = 'procedure PruneInstallLogs' }
        @{ Name = "Log writer"; Pattern = 'procedure WriteInstallLog' }
        @{ Name = "Log path variable"; Pattern = 'LogFilePath:' }
        @{ Name = "Log active flag"; Pattern = 'LogActive:' }
        @{ Name = "Setup initialisation"; Pattern = 'function InitializeSetup' }
        @{ Name = "Setup teardown"; Pattern = 'procedure DeinitializeSetup' }
        @{ Name = "Stage tracking"; Pattern = 'procedure SetStage' }
        @{ Name = "Install target description"; Pattern = 'function GetTargetDescription' }
        @{ Name = "Redaction entry point"; Pattern = 'function Redact\(' }
        @{ Name = "Redaction source markers"; Pattern = 'BEGIN REDACT-SOURCE' }
        @{ Name = "Support block"; Pattern = 'procedure PrintSupportBlock' }
        @{ Name = "Service registration in code"; Pattern = 'function RegisterService' }
        @{ Name = "Failure hook"; Pattern = 'procedure CurStepChanged' }
        @{ Name = "Failure state setter"; Pattern = 'procedure MarkFailure' }
        @{ Name = "Support reference in log filename"; Pattern = '(?s)LOG_PREFIX.*SupportRef.*LOG_EXT' }
        @{ Name = "Max log bytes constant"; Pattern = 'MAX_LOG_BYTES.*=.*1048576' }
        @{ Name = "Max install logs constant"; Pattern = 'MAX_INSTALL_LOGS.*=.*5' }
        @{ Name = "Log directory LocalAppData"; Pattern = '\{localappdata\}.*LOG_DIR_NAME' }
        @{ Name = "Redaction marker"; Pattern = '\[redacted\]' }
        @{ Name = "Support block delimiter"; Pattern = '----- Modulo install failed: support reference' }
        @{ Name = "GitHub issues URL in support block"; Pattern = 'github.com/farnalabs/modulo/issues' }
    )

    $missingPatterns = @()
    foreach ($p in $requiredPatterns) {
        if ($issContent -notmatch $p.Pattern) {
            $missingPatterns += $p.Name
        }
    }
    if ($missingPatterns.Count -gt 0) {
        throw "Installer script missing expected markers: $($missingPatterns -join ', ')"
    }
    Write-Output "  PASS: all expected markers present ($($requiredPatterns.Count) checks)."

    # Identifiers that do not exist in Inno Setup 6 Pascal Script, plus the
    # built-in "Log" shadowing that broke the first revision of this script.
    # Comments are stripped first: prose that *names* an unsupported function
    # (to explain why it is not used) is not a call to it.
    $codeOnly = [regex]::Replace($issContent, '(?s)\{[^}]*\}', '')
    $codeOnly = [regex]::Replace($codeOnly, '(?m)//.*$', '')
    $forbiddenPatterns = @(
        @{ Name = "CryptGenRandom"; Pattern = 'CryptGenRandom' }
        @{ Name = "SetFilePermissions"; Pattern = 'SetFilePermissions' }
        @{ Name = "FindFiles"; Pattern = 'FindFiles' }
        @{ Name = "DeleteOldFiles"; Pattern = 'DeleteOldFiles' }
        @{ Name = "GetFileSize"; Pattern = 'GetFileSize' }
        @{ Name = "FileCreate (ISPP-only)"; Pattern = 'FileCreate' }
        @{ Name = "FileOpen (ISPP-only)"; Pattern = 'FileOpen' }
        @{ Name = "FileWrite (ISPP-only)"; Pattern = 'FileWrite' }
        @{ Name = "FileSeek (ISPP-only)"; Pattern = 'FileSeek' }
        @{ Name = "FileClose (ISPP-only)"; Pattern = 'FileClose' }
        @{ Name = "Built-in Log shadowed"; Pattern = 'procedure Log\(' }
    )
    $forbiddenFound = @()
    foreach ($p in $forbiddenPatterns) {
        if ($codeOnly -match $p.Pattern) {
            $forbiddenFound += $p.Name
        }
    }
    if ($forbiddenFound.Count -gt 0) {
        throw "Installer script still references unsupported identifiers: $($forbiddenFound -join ', ')"
    }
    Write-Output "  PASS: no unsupported Pascal Script identifiers ($($forbiddenPatterns.Count) checks)."

    # --- Launcher helper contract (behavioural, not text matching) -----------
    Write-Output ""
    Write-Output "=== Launcher helper contract ==="

    $probeExe = [System.IO.Path]::Combine($launcherProbeDir, 'modulo.exe')

    Write-Output "Checking the helper is inert when no native binary exists..."
    try {
        & $launcherScriptPath -Version $Version -OutputPath $probeExe | ForEach-Object { Write-Output "  $_" }
    } catch {
        throw "Launcher helper failed when no native binary was present: $_"
    }
    if (Test-Path -LiteralPath $probeExe) {
        throw "Launcher helper created a fake executable at $probeExe"
    }

    Write-Output "Checking the helper fails closed when a binary is required but absent..."
    $threw = $false
    try {
        & $launcherScriptPath -Version $Version -OutputPath $probeExe -RequireBinary | Out-Null
    } catch {
        $threw = $true
    }
    if (-not $threw) {
        throw "Launcher helper did not fail closed with -RequireBinary when the binary was absent."
    }
    if (Test-Path -LiteralPath $probeExe) {
        throw "Launcher helper created a fake executable at $probeExe"
    }

    Write-Output "Checking the helper rejects a non-.exe output path..."
    $threw = $false
    try {
        & $launcherScriptPath -Version $Version -OutputPath ([System.IO.Path]::Combine($launcherProbeDir, 'modulo.zip')) | Out-Null
    } catch {
        $threw = $true
    }
    if (-not $threw) {
        throw "Launcher helper accepted a non-.exe output path."
    }

    $launcherContent = Get-Content -LiteralPath $launcherScriptPath -Raw
    $launcherRequiredPatterns = @(
        @{ Name = "doctor --report flag check"; Pattern = '--report' }
        @{ Name = "support command check"; Pattern = '\bsupport\b' }
        @{ Name = "Required commands list"; Pattern = 'start.*stop.*status.*doctor.*support.*version.*env.*logs' }
    )
    $missingLauncherPatterns = @()
    foreach ($p in $launcherRequiredPatterns) {
        if ($launcherContent -notmatch $p.Pattern) {
            $missingLauncherPatterns += $p.Name
        }
    }
    if ($missingLauncherPatterns.Count -gt 0) {
        throw "Launcher validation script missing contract checks: $($missingLauncherPatterns -join ', ')"
    }
    Write-Output "PASS: launcher helper is inert without a binary, fails closed with -RequireBinary, and rejects non-.exe paths."

    Write-Output ""
    if ($proofsRun.Count -eq 0) {
        $message = "No compiler was available on this host: the installer script and Redact() were NOT verified. CI must provide ISCC (Windows) and fpc (Ubuntu)."
        if ($env:CI) {
            throw $message
        }
        Write-Warning $message
    } else {
        Write-Output "Proofs executed: $($proofsRun -join ', ')"
        Write-Output "PASS: Windows installer contract verified."
    }
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
