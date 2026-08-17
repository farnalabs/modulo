#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Pre-commit check: detect Alembic migration number/revision collisions.

.DESCRIPTION
  Two Workers developing in parallel worktrees can independently create a
  migration with the same sequential number (e.g. both add "0062_*.py"),
  which doesn't show up as a git merge conflict (different filenames) but
  corrupts the migration chain — two files claiming the same numeric slot,
  or two files with the same `down_revision`, creating a branch in what
  should be a single linear history.

  This repo's history already has ~10 pre-existing pairs of colliding
  migration numbers/down_revisions from before this check existed, so it
  ONLY evaluates files that are staged/changed in the current commit —
  it flags a collision only when a file being committed right now shares
  a number, revision, or down_revision with another file (new or old).
  Pre-existing collisions between two files neither of which is part of
  this commit are left alone; fixing that backlog is separate, larger work.

  Intentional forks are exempt. A migration whose own `revision` id appears
  as a parent in any merge migration's tuple-form `down_revision` (e.g. the
  0037 fork, reconciled by a merge migration listing both 0037 revisions) is
  an intentional branch, not a collision - those files are excluded from the
  duplicate-number, duplicate-revision, and duplicate-down_revision checks.
  Everything else stays strict.

  It also runs `alembic heads` and prints a non-fatal WARNING if more than
  one head exists, purely for visibility.

.PARAMETER DiffRange
  Optional git diff range (e.g. "main...HEAD"). When given, the set of
  "changed" migration files is taken from `git diff --name-only <range>`
  instead of the staged files (`--cached`). Used by gate.ps1 to validate
  everything a worktree branch adds relative to main.

.EXAMPLE
  powershell -NoProfile -File devtools/harness/tools/check-migration-heads.ps1
  powershell -NoProfile -File devtools/harness/tools/check-migration-heads.ps1 -DiffRange "main...HEAD"
#>

param(
  [string]$DiffRange
)

$ErrorActionPreference = "Stop"
# Prefer the git repo containing the current directory (so this works from
# worktree branches, where the branch's new migration files live); fall back
# to the repo root (tools/ is one level below the repo root in this repository).
$cwdTop = git rev-parse --show-toplevel 2>$null
if ($LASTEXITCODE -eq 0 -and $cwdTop -and (Test-Path (Join-Path $cwdTop "backend/src/modulo/db/migrations/versions"))) {
  $RepoRoot = (Resolve-Path $cwdTop).Path
} else {
  $RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
}
$VersionsDir = Join-Path $RepoRoot "backend/src/modulo/db/migrations/versions"

if (-not (Test-Path -LiteralPath $VersionsDir)) {
  Write-Host "check-migration-heads: versions dir not found at $VersionsDir - skipping" -ForegroundColor Yellow
  exit 0
}

$files = Get-ChildItem -LiteralPath $VersionsDir -Filter "*.py" | Where-Object { $_.Name -ne "__init__.py" }

Push-Location $RepoRoot
if ($DiffRange) {
  $changedRelative = git diff --name-only --diff-filter=ACMR $DiffRange 2>&1
} else {
  $changedRelative = git diff --cached --name-only --diff-filter=ACMR 2>&1
}
Pop-Location
$changedNames = $changedRelative |
  Where-Object { $_ -match [regex]::Escape("backend/src/modulo/db/migrations/versions/") -and $_ -like "*.py" } |
  ForEach-Object { Split-Path $_ -Leaf }

if (-not $changedNames -or $changedNames.Count -eq 0) {
  if ($DiffRange) {
    # The branch/range touches no migration files - nothing to validate.
    # (Do NOT fall back to all files here: pre-existing collisions in history
    # would make every caller fail regardless of what the branch changed.)
    Write-Host "check-migration-heads: no migration files changed in '$DiffRange' - OK" -ForegroundColor Green
    exit 0
  }
  # Nothing staged under versions/ (e.g. running standalone, not via pre-commit) -
  # fall back to checking every file so the script is still useful ad hoc.
  $changedNames = $files.Name
}

$failed = $false

# ---- 0. Merge-parent revisions (intentional forks) ----
# A migration whose revision id appears as a parent in any merge migration's
# tuple-form down_revision (e.g. the 0037 fork reconciled by a merge migration
# that lists both 0037 revisions) is an intentional fork, not a collision.
# Collect those parent revision ids and the per-file revision map so the
# duplicate checks below can exempt them while staying strict for everything
# else.
$mergeParentRevisions = @{}
$fileRevisions = @{}
foreach ($f in $files) {
  $content = Get-Content -Raw -LiteralPath $f.FullName
  if ($content -match '(?m)^revision:\s*str\s*=\s*"([^"]+)"') {
    $fileRevisions[$f.Name] = $Matches[1]
  }
  # Tuple-form down_revision (merge migration): the parent list opens with '('
  # on the down_revision line. Slice from that line through the closing ')' and
  # collect every quoted revision id inside - those are the fork parents.
  if ($content -match '(?m)^down_revision\s*[:=].*\(') {
    $lines = $content -split "`r?`n"
    $start = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
      if ($lines[$i] -match '^down_revision\s*[:=]') { $start = $i; break }
    }
    if ($start -ge 0) {
      $block = ""
      for ($i = $start; $i -lt $lines.Count; $i++) {
        $block += $lines[$i] + "`n"
        if ($lines[$i] -match '\)') { break }
      }
      foreach ($m in [regex]::Matches($block, '"([^"]+)"')) {
        $mergeParentRevisions[$m.Groups[1].Value] = $true
      }
    }
  }
}

# Filter a file list down to the non-exempt (non merge-parent) files.
$nonExempt = {
  param($names)
  $names | Where-Object { -not ($fileRevisions[$_] -and $mergeParentRevisions.ContainsKey($fileRevisions[$_])) }
}

# ---- 1. Duplicate numeric prefixes ----
$byPrefix = @{}
foreach ($f in $files) {
  if ($f.Name -match '^(\d{4})_') {
    $prefix = $Matches[1]
    if (-not $byPrefix.ContainsKey($prefix)) { $byPrefix[$prefix] = @() }
    $byPrefix[$prefix] += $f.Name
  }
}
foreach ($prefix in $byPrefix.Keys) {
  $names = & $nonExempt $byPrefix[$prefix]
  $involvesChanged = $names | Where-Object { $changedNames -contains $_ }
  if ($names.Count -gt 1 -and $involvesChanged) {
    Write-Host "FAIL: duplicate migration number '$prefix' used by:" -ForegroundColor Red
    foreach ($name in $names) { Write-Host "  - $name" -ForegroundColor Red }
    Write-Host "  > Renumber the one you're adding to the next free sequential number and fix its down_revision." -ForegroundColor Yellow
    $failed = $true
  }
}

# ---- 2. Duplicate revision / down_revision strings ----
$revisions = @{}
$downRevisions = @{}
foreach ($f in $files) {
  $content = Get-Content -Raw -LiteralPath $f.FullName
  if ($content -match '(?m)^revision:\s*str\s*=\s*"([^"]+)"') {
    $rev = $Matches[1]
    if (-not $revisions.ContainsKey($rev)) { $revisions[$rev] = @() }
    $revisions[$rev] += $f.Name
  }
  if ($content -match '(?m)^down_revision:.*=\s*"([^"]+)"') {
    $down = $Matches[1]
    if (-not $downRevisions.ContainsKey($down)) { $downRevisions[$down] = @() }
    $downRevisions[$down] += $f.Name
  }
}
foreach ($rev in $revisions.Keys) {
  $names = & $nonExempt $revisions[$rev]
  $involvesChanged = $names | Where-Object { $changedNames -contains $_ }
  if ($names.Count -gt 1 -and $involvesChanged) {
    Write-Host "FAIL: duplicate revision id '$rev' declared in:" -ForegroundColor Red
    foreach ($name in $names) { Write-Host "  - $name" -ForegroundColor Red }
    $failed = $true
  }
}
foreach ($down in $downRevisions.Keys) {
  $names = & $nonExempt $downRevisions[$down]
  $involvesChanged = $names | Where-Object { $changedNames -contains $_ }
  if ($names.Count -gt 1 -and $involvesChanged) {
    Write-Host "FAIL: two migrations both declare down_revision '$down' - this is an unintended branch:" -ForegroundColor Red
    foreach ($name in $names) { Write-Host "  - $name" -ForegroundColor Red }
    Write-Host "  > The one you're adding needs to be rebased on top of the other (renumber + fix down_revision)." -ForegroundColor Yellow
    $failed = $true
  }
}

if ($failed) {
  Write-Host "`ncheck-migration-heads: FAILED - resolve the migration collisions above before committing." -ForegroundColor Red
  exit 1
}

# ---- 3. Non-fatal: multiple alembic heads ----
try {
  Push-Location (Join-Path $RepoRoot "backend")
  # `uv run alembic` hits "uv trampoline failed to canonicalize script path"
  # on some Windows setups; `python -m alembic` avoids the exe shim.
  $headsOutput = uv run python -m alembic heads 2>&1
  Pop-Location
  $headCount = ($headsOutput | Select-String -Pattern "\(head\)|\(effective head\)").Count
  if ($headCount -gt 1) {
    Write-Host "WARNING: alembic reports $headCount migration heads (expected 1):" -ForegroundColor Yellow
    $headsOutput | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
    Write-Host "  This is a non-fatal warning - existing multi-head history is tracked separately." -ForegroundColor Yellow
  }
} catch {
  Write-Host "check-migration-heads: could not run 'alembic heads' to check for multiple heads (non-fatal): $_" -ForegroundColor Yellow
}

Write-Host "check-migration-heads: OK - no migration number/revision collisions" -ForegroundColor Green
exit 0
