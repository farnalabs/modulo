<#
.SYNOPSIS
  Product map graph queries.

  Queries the product map graph:
    --uncovered             list entries that need attention (empty/missing bdd)
    --impact feat-<id>      list entries that depend on <id> (downstream dependents)
    --depends feat-<id>     list entries that <id> depends on (upstream prereqs)

  Exit code: 0 = clean, 1 = nothing matched (for --impact/--depends when the
  id is unknown or has no dependents).
#>
param(
    [switch]$Uncovered,
    [string]$Impact,
    [string]$Depends
)
$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$productMap = Join-Path $repoRoot "docs/product-map"

$entries = @()
Get-ChildItem -Recurse -Filter "*.md" -LiteralPath $productMap | Where-Object { $_.Name -ne "_index.md" } | ForEach-Object {
    $c = Get-Content -Raw -Encoding UTF8 -LiteralPath $_.FullName
    if ($c -notmatch '(?s)^---[\r\n]+(.+?)[\r\n]+---') { return }
    $fm = $Matches[1]
    $id = if ($fm -match '(?m)^id:\s*(\S+)') { $Matches[1] } else { $null }
    $bdd = @()
    if ($fm -match '(?m)^bdd:\s*(.+?)[\r\n]') {
        $bList = $Matches[1].Trim()
        if ($bList -match '^\[') { $bdd = $bList -replace '[\[\]" ]', '' -split ',' }
    }
    if ($fm -match '(?m)^bdd:\s*\n((?:\s+- .+\n?)+)') {
        $bBlock = $Matches[1] -split '\n' | ForEach-Object { ($_ -replace '^\s*-\s*', '' -replace '"', '' -replace "'", '' -replace '#.*', '').Trim() } | Where-Object { $_ }
        if ($bBlock) { $bdd = @($bdd) + $bBlock }
    }
    $dep = @()
    if ($fm -match '(?m)^depends-on:\s*\[(.*?)\]') { $dep = $Matches[1] -replace ' ', '' -split ',' }
    if ($fm -match '(?m)^depends-on:\s*\n((?:\s+- .+\n?)+)') {
        $depBlock = $Matches[1] -split '\n' | ForEach-Object { ($_ -replace '^\s*-\s*', '' -replace '"', '' -replace "'", '' -replace '#.*', '').Trim() } | Where-Object { $_ }
        $dep = @($dep + $depBlock) | Where-Object { $_ }
    }
    $entries += @{ id = $id; bdd = @($bdd | Where-Object { $_ }); depends = @($dep | Where-Object { $_ }); path = $_.FullName; name = $_.Name }
}

if ($Uncovered) {
    Write-Host "Entries needing attention (empty or missing bdd coverage):" -ForegroundColor Cyan
    $uncoveredEntries = @($entries | Where-Object { -not $_.id -or -not $_.bdd -or $_.bdd.Count -eq 0 } | Sort-Object name)
    if ($uncoveredEntries.Count -eq 0) {
        Write-Host "  None - every entry has bdd coverage." -ForegroundColor Green
        exit 0
    }
    foreach ($e in $uncoveredEntries) {
        Write-Host "  $($e.name) ($($e.id))"
    }
    Write-Host "$($uncoveredEntries.Count) entry(ies) need attention." -ForegroundColor Yellow
    exit 1
}

if ($Impact -or $Depends) {
    if ($Impact) {
        $target = $Impact
        Write-Host "Downstream dependents of ${target}:" -ForegroundColor Cyan
        $dependents = @($entries | Where-Object { $_.depends -contains $target } | Sort-Object id)
        if ($dependents.Count -eq 0) {
            if (-not ($entries.id -contains $target)) {
                Write-Host "  '$target' is not a known product map id." -ForegroundColor Red
            } else {
                Write-Host "  None." -ForegroundColor Green
            }
            exit 1
        }
        foreach ($e in $dependents) { Write-Host "  $($e.id)" }
        exit 0
    }
    if ($Depends) {
        $target = $Depends
        Write-Host "Upstream dependencies of ${target}:" -ForegroundColor Cyan
        $entry = $entries | Where-Object { $_.id -eq $target }
        if (-not $entry) {
            Write-Host "  '$target' is not a known product map id." -ForegroundColor Red
            exit 1
        }
        if ($entry.depends.Count -eq 0) {
            Write-Host "  None." -ForegroundColor Green
            exit 0
        }
        foreach ($d in $entry.depends) { Write-Host "  $d" }
        exit 0
    }
}

Write-Host "Usage: graph-query.ps1 -Uncovered | -Impact <feat-id> | -Depends <feat-id>" -ForegroundColor Yellow
exit 1
