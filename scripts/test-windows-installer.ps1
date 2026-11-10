<#
.SYNOPSIS
Validate the Windows Inno Setup installer contract.

.DESCRIPTION
Two independent proofs, plus a fast syntax check and a supplementary text signal:

  0. PREPROCESSOR (any OS, no compiler): no line of modulo.iss may begin with
     a character-code literal, because ISPP reads a leading "#" as a directive
     and aborts the compile with nothing but an exit code to show for it.
  1. COMPILE (Windows + Inno Setup 6.2.2): ISCC compiles deploy/windows/modulo.iss
     against fixture bundles. A bundle missing modulo.exe or modulo-runtime.json
     must FAIL to compile; a complete bundle must compile and produce an installer.
     ISCC's own output is printed whenever the exit code is non-zero - a bare
     exit code is not a diagnosis.
  2. REDACTION (any OS + Free Pascal): the Redact() source is extracted verbatim
     from modulo.iss, compiled with fpc and executed against credential fixtures.
     This proves the function removes credentials rather than merely mentioning
     a redaction pattern in the file.
  3. Supplementary text checks only ever run alongside a proof - a text match is
     never accepted as evidence that the script works.

In CI at least one compiler must be present or the run fails; a silent "nothing
was verified" pass is the defect this script exists to prevent. CI passes
-RequireIscc on the Windows job and -RequireFpc on the Ubuntu job so each proof
is owned by a job that must fail rather than skip when its compiler is missing.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = "0.3.323",

    [Parameter(Mandatory = $false)]
    [ValidateSet("amd64")]
    [string]$Arch = "amd64",

    # CI passes exactly one of these per job so a proof can never be skipped
    # silently: the Windows job owns ISCC, the Ubuntu job owns fpc. Each job
    # fails if the compiler it is responsible for is missing.
    [Parameter(Mandatory = $false)]
    [switch]$RequireIscc,

    [Parameter(Mandatory = $false)]
    [switch]$RequireFpc
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
$rootExeBundle = [System.IO.Path]::Combine($tempRoot, 'root-exe')
$stagedFixtureDir = [System.IO.Path]::Combine($tempRoot, 'staged-fixture')
$stagedBundle = [System.IO.Path]::Combine($tempRoot, 'staged')

New-Item -ItemType Directory -Path $missingExeBundle, $missingMarkerBundle, $validBundle, $outputDir, $redactDir, $launcherProbeDir, $rootExeBundle -Force | Out-Null

[System.IO.File]::WriteAllText([System.IO.Path]::Combine($missingExeBundle, 'modulo-runtime.json'), '{"contract":"native-windows-v1"}')
# The launcher resolves the bundle root as the parent of its own directory, so
# the contract path is bin\modulo.exe. A launcher at the bundle root is the old,
# wrong layout and must not satisfy the installer.
New-Item -ItemType Directory -Path ([System.IO.Path]::Combine($missingMarkerBundle, 'bin')) -Force | Out-Null
[System.IO.File]::WriteAllText([System.IO.Path]::Combine($missingMarkerBundle, 'bin', 'modulo.exe'), 'placeholder')
[System.IO.File]::WriteAllText([System.IO.Path]::Combine($rootExeBundle, 'modulo.exe'), 'placeholder')
[System.IO.File]::WriteAllText([System.IO.Path]::Combine($rootExeBundle, 'modulo-runtime.json'), '{"contract":"native-windows-v1"}')

function Invoke-Iscc {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BundleDir,
        [Parameter(Mandatory = $true)]
        [string]$OutputName,
        [Parameter(Mandatory = $true)]
        [string]$CaseName
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

    # ISCC's own output is the only explanation of WHY a compile failed, so it is
    # captured here and printed whenever the exit code is non-zero. Without this
    # the only signal is a bare exit code and every fix is a guess.
    # ISCC exit codes (Projects/ISCC.dpr in jrsoftware/issrc, tag is-6_2_2):
    #   0 = success, 2 = script compile failure, 1 = command-line/internal error.
    $compilerOutput = & $isccPath @arguments 2>&1
    $exitCode = [int]$LASTEXITCODE
    $lines = @($compilerOutput | ForEach-Object { "$_" })

    $logPath = [System.IO.Path]::Combine($repoRoot, "iscc-$CaseName.log")
    $logHeader = @(
        "ISCC: $isccPath",
        "Case: $CaseName",
        "Arguments: $($arguments -join ' ')",
        "Exit code: $exitCode",
        ''
    )
    $logBody = (@($logHeader) + @($lines)) -join [Environment]::NewLine
    try {
        [System.IO.File]::WriteAllText($logPath, $logBody)
    } catch {
        Write-Warning "Could not write the ISCC log to $logPath : $_"
    }

    # Write-Host, not Write-Output: this function's output stream is captured by
    # the caller into $result, so anything written with Write-Output would be
    # swallowed and never reach the build log - which is exactly how ISCC's
    # diagnosis went missing before.
    if ($exitCode -ne 0) {
        Write-Information "--- ISCC output ($CaseName, exit $exitCode) ---" -InformationAction Continue
        if ($lines.Count -eq 0) {
            Write-Information "  (ISCC wrote nothing to stdout or stderr)" -InformationAction Continue
        } else {
            $tailCount = [System.Math]::Min(40, $lines.Count)
            $tail = $lines[($lines.Count - $tailCount)..($lines.Count - 1)]
            foreach ($line in $tail) {
                Write-Information "  $line" -InformationAction Continue
            }
            if ($lines.Count -gt $tailCount) {
                Write-Information "  (showing the last $tailCount of $($lines.Count) lines)" -InformationAction Continue
            }
        }
        Write-Information "--- end ISCC output ($CaseName) ---" -InformationAction Continue
        Write-Information "Full ISCC output written to $logPath" -InformationAction Continue
    }

    return @{ ExitCode = $exitCode; Output = $lines; LogPath = $logPath }
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

    if ($RequireIscc -and -not $isccPath) {
        throw "Inno Setup ISCC is required by -RequireIscc but was not found on this host. The compilation proof would otherwise be skipped."
    }
    if ($RequireFpc -and -not $fpcPath) {
        throw "Free Pascal fpc is required by -RequireFpc but was not found on this host. The Redact()/[Code] proofs would otherwise be skipped."
    }

    # --- Proof 0: the preprocessor can read these lines --------------------
    # Three outer-parser traps inside [Code], none of which needs a compiler to
    # detect:
    #   a) ISPP treats a line whose first non-blank character is "#" as a
    #      directive (Projects/ISPP/IsppPreprocessor.pas: ParsePreprocCommand),
    #      and an unknown directive aborts the compile. A Pascal character-code
    #      literal at the start of a line is therefore a fatal error. No ISPP
    #      directive begins with a digit or "$", so this has no false positives.
    #   b) {#name} substitution only happens on ordinary lines. A directive's
    #      argument is parsed as an ISPP expression, so `#ifexist "{#DIR}\x"`
    #      tests for a file literally named "{#DIR}\x" and is always false.
    #      #error is exempt: its text is documented as not parsed.
    #   c) Inno Setup treats any trimmed line starting with "[" as a section tag
    #      (Projects/Compile.pas: `if (L <> '') and (L[1] = '[')`), so a "[Run]"
    #      at the start of a comment line inside [Code] is "Invalid section tag".
    Write-Output "=== Proof 0: the preprocessor can read these lines ==="
    $issPreprocText = Get-Content -LiteralPath $issPath -Raw
    $preprocLines = @($issPreprocText -split "`r?`n")
    $directiveViolations = @()
    $inlineViolations = @()
    $sectionTagViolations = @()
    $lineNumber = 0
    $inCodeSection = $false
    foreach ($line in $preprocLines) {
        $lineNumber++
        $trimmed = $line.Trim()
        if ($trimmed -eq '[Code]') {
            $inCodeSection = $true
            continue
        }
        if ($line -match '^\s*#[0-9$]') {
            $directiveViolations += "line ${lineNumber}: $trimmed"
        }
        if (($line -match '^\s*#') -and ($line -notmatch '^\s*#error\b') -and ($line -match '\{#')) {
            $inlineViolations += "line ${lineNumber}: $trimmed"
        }
        if ($inCodeSection -and ($trimmed.StartsWith('['))) {
            $sectionTagViolations += "line ${lineNumber}: $trimmed"
        }
    }
    if ($directiveViolations.Count -gt 0) {
        throw ("A line starts with a character-code literal, which ISPP parses as an unknown preprocessor directive: " + ($directiveViolations -join '; ') + ". Move the '#' escape away from the start of the line.")
    }
    if ($inlineViolations.Count -gt 0) {
        throw ("A preprocessor directive uses inline {#name} substitution, which ISPP does not expand inside a directive - use the name as an expression instead: " + ($inlineViolations -join '; '))
    }
    if ($sectionTagViolations.Count -gt 0) {
        throw ("A [Code] line starts with '[', which Inno Setup parses as a section tag: " + ($sectionTagViolations -join '; ') + ". Keep the bracket away from the start of the line.")
    }
    Write-Output "PASS: no line confuses the preprocessor or the section parser."
    Write-Output ""

    # --- Proof 1: the script compiles, and fails closed on a broken bundle ---
    if ($isccPath) {
        Write-Output "=== Proof 1: ISCC compilation (Inno Setup 6) ==="

        Write-Output "Checking that a bundle missing modulo.exe fails compilation..."
        $result = Invoke-Iscc -BundleDir $missingExeBundle -OutputName 'missing-exe-contract' -CaseName 'missing-exe'
        Write-Output "Missing modulo.exe compile exit code: $($result.ExitCode)"
        if ($result.ExitCode -eq 0) {
            throw "Installer compiled despite missing modulo.exe."
        }

        Write-Output "Checking that a bundle with modulo.exe at the root (not bin) fails compilation..."
        $result = Invoke-Iscc -BundleDir $rootExeBundle -OutputName 'root-exe-contract' -CaseName 'root-exe'
        Write-Output "Root modulo.exe compile exit code: $($result.ExitCode)"
        if ($result.ExitCode -eq 0) {
            throw "Installer compiled although modulo.exe is not at bin\modulo.exe."
        }

        Write-Output "Checking that a bundle missing modulo-runtime.json fails compilation..."
        $result = Invoke-Iscc -BundleDir $missingMarkerBundle -OutputName 'missing-marker-contract' -CaseName 'missing-marker'
        Write-Output "Missing modulo-runtime.json compile exit code: $($result.ExitCode)"
        if ($result.ExitCode -eq 0) {
            throw "Installer compiled despite missing modulo-runtime.json."
        }

        New-Item -ItemType Directory -Path ([System.IO.Path]::Combine($validBundle, 'bin')) -Force | Out-Null
        [System.IO.File]::WriteAllText([System.IO.Path]::Combine($validBundle, 'bin', 'modulo.exe'), 'placeholder')
        [System.IO.File]::WriteAllText([System.IO.Path]::Combine($validBundle, 'modulo-runtime.json'), '{"contract":"native-windows-v1"}')

        Write-Output "Checking that the declared bundle contract compiles..."
        $result = Invoke-Iscc -BundleDir $validBundle -OutputName 'valid-contract' -CaseName 'valid-contract'
        Write-Output "Valid contract compile exit code: $($result.ExitCode)"
        $installerPath = [System.IO.Path]::Combine($outputDir, 'valid-contract.exe')
        if ($result.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
            throw "Installer failed to compile the declared bundle contract."
        }
        $proofsRun += 'ISCC compile'
        Write-Output "PASS: ISCC compiled the installer and rejected the incomplete bundles."
    } else {
        Write-Warning "ISCC not found: compilation was NOT verified on this host. The windows-installer CI job compiles it on a Windows runner."
    }

    # --- Proof 1b: the real staging function produces a bundle the .iss accepts ---
    # Structural half (no ISCC needed): stage a tiny fake payload through
    # Invoke-WindowsBundleStaging and check the layout against the .iss contract.
    # Compile half (ISCC only): compile the .iss against that staged directory, so
    # the contract is proven against the producer, not only a hand-made fixture.
    # No signing secret is involved; the launcher is a placeholder byte string.
    Write-Output ""
    Write-Output "=== Proof 1b: bundle staged by Invoke-WindowsBundleStaging ==="
    $fxPython = [System.IO.Path]::Combine($stagedFixtureDir, 'python')
    $fxBackend = [System.IO.Path]::Combine($stagedFixtureDir, 'backend-src')
    $fxSite = [System.IO.Path]::Combine($stagedFixtureDir, 'site-packages')
    $fxFrontend = [System.IO.Path]::Combine($stagedFixtureDir, 'frontend-dist')
    $fxLauncher = [System.IO.Path]::Combine($stagedFixtureDir, 'launcher.exe')
    New-Item -ItemType Directory -Path $fxPython, ([System.IO.Path]::Combine($fxBackend, 'modulo')), $fxSite, $fxFrontend -Force | Out-Null
    [System.IO.File]::WriteAllText([System.IO.Path]::Combine($fxPython, 'python.exe'), 'python')
    [System.IO.File]::WriteAllText([System.IO.Path]::Combine($fxPython, 'python312.dll'), 'dll')
    [System.IO.File]::WriteAllText([System.IO.Path]::Combine($fxBackend, 'modulo', '__init__.py'), '')
    [System.IO.File]::WriteAllText([System.IO.Path]::Combine($fxFrontend, 'index.html'), 'ok')
    [System.IO.File]::WriteAllText($fxLauncher, 'launcher')
    # Dot-sourced inside a child scope: the script sets Set-StrictMode, which
    # must not leak into the rest of this test.
    $stagingScript = [System.IO.Path]::Combine($repoRoot, 'scripts', 'windows', 'build-launcher.ps1')
    & {
        . $stagingScript
        Invoke-WindowsBundleStaging -StagePath $stagedBundle -PythonRoot $fxPython -BackendSource $fxBackend -SitePackagesSource $fxSite -FrontendDistSource $fxFrontend -LauncherPath $fxLauncher -Version $Version | Out-Null
    }
    foreach ($contractPath in @('bin\modulo.exe', 'modulo-runtime.json')) {
        if (-not (Test-Path -LiteralPath ([System.IO.Path]::Combine($stagedBundle, $contractPath)) -PathType Leaf)) {
            throw "Staged bundle does not satisfy the modulo.iss contract: missing $contractPath"
        }
    }
    $stagedMarker = Get-Content -LiteralPath ([System.IO.Path]::Combine($stagedBundle, 'modulo-runtime.json')) -Raw | ConvertFrom-Json
    if ($stagedMarker.contract -ne 'native-windows-v1') {
        throw "Staged modulo-runtime.json has contract '$($stagedMarker.contract)', expected 'native-windows-v1'."
    }
    if ($stagedMarker.version -ne $Version) {
        throw "Staged modulo-runtime.json has version '$($stagedMarker.version)', expected '$Version'."
    }
    $proofsRun += 'staged bundle layout'
    Write-Output "PASS: staging wrote bin\modulo.exe and a valid modulo-runtime.json."
    if ($isccPath) {
        $result = Invoke-Iscc -BundleDir $stagedBundle -OutputName 'staged-contract' -CaseName 'staged-contract'
        Write-Output "Staged bundle compile exit code: $($result.ExitCode)"
        if ($result.ExitCode -ne 0 -or -not (Test-Path -LiteralPath ([System.IO.Path]::Combine($outputDir, 'staged-contract.exe')) -PathType Leaf)) {
            throw "Installer failed to compile a bundle produced by Invoke-WindowsBundleStaging."
        }
        $proofsRun += 'ISCC compile (staged bundle)'
        Write-Output "PASS: ISCC compiled the installer against the staged bundle."
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
        # The declarations below are copied verbatim from Inno Setup 6.2.2, not
        # invented: https://github.com/jrsoftware/issrc (tag is-6_2_2)
        #   Projects/ScriptFunc.pas   - the support-function declaration tables
        #   Projects/ScriptFunc_C.pas - registered types, enums and constants
        #   Components/UniPs          - Pascal Script standard procedures
        #                               (SetArrayLength, Copy, Pos, Delete,
        #                               Insert, Length, LowerCase, IntToStr)
        # Anything the script uses that neither these declarations nor FPC's own
        # runtime provide fails here, which catches an unknown identifier or a
        # wrong argument shape without a Windows host. Its blind spot is an
        # identifier Pascal Script lacks but FPC's RTL provides (Randomize,
        # Sleep, ...): that is caught by the forbidden-identifier list below.
        # It does NOT run the ISPP preprocessor (see Proof 0), and ISCC remains
        # the authoritative compiler in the windows CI job.
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
  TFileTime = record
    dwLowDateTime: LongWord;
    dwHighDateTime: LongWord;
  end;
  TFindRec = record
    Name: string;
    Attributes: LongWord;
    SizeHigh: LongWord;
    SizeLow: LongWord;
    CreationTime: TFileTime;
    LastAccessTime: TFileTime;
    LastWriteTime: TFileTime;
    AlternateName: string;
    FindHandle: LongWord;
  end;
  TSetupStep = (ssPreInstall, ssInstall, ssPostInstall, ssDone);
  TExecWait = (ewNoWait, ewWaitUntilTerminated, ewWaitUntilIdle);
  TMsgBoxType = (mbInformation, mbConfirmation, mbError, mbCriticalError);
  TArrayOfString = array of string;

const
  SW_HIDE = 0;
  MB_OK = 0;
  wpWelcome = 1;
  wpLicense = 2;
  wpSelectDir = 3;
  wpSelectTasks = 4;
  wpReady = 5;

function ExpandConstant(const S: String): String; begin Result := S; end;
function SaveStringToFile(const FileName: String; const S: AnsiString; const Append: Boolean): Boolean; begin Result := True; end;
function FileExists(const Name: String): Boolean; begin Result := False; end;
function DirExists(const Name: String): Boolean; begin Result := False; end;
function ForceDirectories(Dir: string): Boolean; begin Result := True; end;
function DeleteFile(const FileName: string): Boolean; begin Result := True; end;
function FindFirst(const FileName: String; var FindRec: TFindRec): Boolean; begin Result := False; end;
function FindNext(var FindRec: TFindRec): Boolean; begin Result := False; end;
procedure FindClose(var FindRec: TFindRec); begin end;
{ Pascal Script standard procedure; Inno's help gives it as
  procedure SetArrayLength(var Arr: Array; I: Longint). }
procedure SetArrayLength(var Arr: TArrayOfString; const I: Longint); begin SetLength(Arr, I); end;
function GetDateTimeString(const DateTimeFormat: String; const DateSeparator, TimeSeparator: Char): String; begin Result := ''; end;
function CompareText(const S1, S2: string): Integer; begin Result := 0; end;
function Random(const Range: Integer): Integer; begin Result := 0; end;
function Exec(const Filename, Params, WorkingDir: String; const ShowCmd: Integer; const Wait: TExecWait; var ResultCode: Integer): Boolean; begin ResultCode := 0; Result := True; end;
function MsgBox(const Text: String; const Typ: TMsgBoxType; const Buttons: Integer): Integer; begin Result := 0; end;
function WizardSilent: Boolean; begin Result := False; end;
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
        @{ Name = "Deferred service mode notice"; Pattern = 'procedure WriteServiceModeNotice' }
        @{ Name = "Failure hook"; Pattern = 'procedure CurStepChanged' }
        @{ Name = "Failure state setter"; Pattern = 'procedure MarkFailure' }
        @{ Name = "Support reference in log filename"; Pattern = '(?s)LOG_PREFIX.*SupportRef.*LOG_EXT' }
        @{ Name = "Max log bytes constant"; Pattern = 'MAX_LOG_BYTES.*=.*1048576' }
        @{ Name = "Max install logs constant"; Pattern = 'MAX_INSTALL_LOGS.*=.*5' }
        @{ Name = "Log directory LocalAppData"; Pattern = '\{localappdata\}.*LOG_DIR_NAME' }
        @{ Name = "Redaction marker"; Pattern = '\[redacted\]' }
        @{ Name = "Support block delimiter"; Pattern = '----- Farna Control install failed: support reference' }
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
    # Comments are stripped first - brace comments, // comments and the .iss
    # section-file ";" comment lines - because prose that *names* an unsupported
    # function (to explain why it is not used) is not a call to it.
    # Randomize is listed because FPC's own RTL provides it, so the stub compile
    # cannot catch it - but Pascal Script has only Random (Setup seeds the
    # generator in InitializeCommonVars), and ISCC rejects Randomize.
    $codeOnly = [regex]::Replace($issContent, '(?s)\{[^}]*\}', '')
    $codeOnly = [regex]::Replace($codeOnly, '(?m)//.*$', '')
    $codeOnly = [regex]::Replace($codeOnly, '(?m)^[ \t]*;.*$', '')
    $forbiddenPatterns = @(
        @{ Name = "Randomize (not in Pascal Script; use Random)"; Pattern = '\bRandomize\b' }
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

    # Windows service mode is deferred to Beta (FAR-2347): Alpha installs a
    # foreground-start shortcut and records the limitation with
    # WriteServiceModeNotice. The marker table above can only assert presence, so
    # the absence of service registration is checked explicitly here - otherwise
    # a future re-introduction of the deleted RegisterService (plus the sc.exe
    # call and [UninstallRun] hook that go with it) would pass this gate silently.
    # Scanned against the comment-stripped text, so the "No [UninstallRun]
    # section" explanatory comment above can neither satisfy nor trip the check,
    # while a real section header or Exec() call survives.
    Write-Output "Checking that no Windows service registration has crept back in..."
    $forbiddenServicePatterns = @(
        @{ Name = "Service registration procedure"; Pattern = '\bRegisterService\b' }
        @{ Name = "Service Control Manager invoked via sc.exe"; Pattern = '\bsc\.exe\b' }
        @{ Name = "Service teardown hook section"; Pattern = '(?m)^[ \t]*\[UninstallRun\]' }
    )
    $serviceRegistrationFound = @()
    foreach ($p in $forbiddenServicePatterns) {
        if ($codeOnly -match $p.Pattern) {
            $serviceRegistrationFound += $p.Name
        }
    }
    if ($serviceRegistrationFound.Count -gt 0) {
        throw "Installer script registers a Windows service, which is deferred to Beta (FAR-2347): $($serviceRegistrationFound -join ', '). Alpha must install the foreground-start shortcut and log the deferred notice instead."
    }
    Write-Output "  PASS: no Windows service registration ($($forbiddenServicePatterns.Count) checks)."

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
