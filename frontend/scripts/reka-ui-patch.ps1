$ErrorActionPreference = "Stop"

$rekaDir = Join-Path $PSScriptRoot "..\node_modules\reka-ui\dist"
$dcts = Join-Path $rekaDir "index.d.cts"
$dts = Join-Path $rekaDir "index.d.ts"

if (-not (Test-Path -LiteralPath $rekaDir)) {
  Write-Host "reka-ui not installed — skipping type patch"
  exit 0
}

if (Test-Path -LiteralPath $dts)) {
  Write-Host "reka-ui: index.d.ts already exists — OK"
  exit 0
}

if (-not (Test-Path -LiteralPath $dcts)) {
  Write-Warning "reka-ui: index.d.cts not found at $dcts — reka-ui may have changed its type packaging"
  exit 1
}

try {
  Copy-Item -LiteralPath $dcts -Destination $dts -ErrorAction Stop
  Write-Host "reka-ui: copied index.d.cts → index.d.ts — OK"
} catch {
  Write-Error "reka-ui: failed to copy index.d.cts → index.d.ts: $_"
  exit 1
}
