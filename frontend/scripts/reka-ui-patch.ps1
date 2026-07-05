$rekaDir = Join-Path $PSScriptRoot "..\node_modules\reka-ui\dist"
$dcts = Join-Path $rekaDir "index.d.cts"
$dts = Join-Path $rekaDir "index.d.ts"
if (-not (Test-Path -LiteralPath $rekaDir)) { Write-Host "reka-ui: not installed"; exit 0 }
if (Test-Path -LiteralPath $dts) { Write-Host "reka-ui: index.d.ts ok"; exit 0 }
if (-not (Test-Path -LiteralPath $dcts)) { Write-Host "reka-ui: no index.d.cts found"; exit 0 }
try {
  Copy-Item -LiteralPath $dcts -Destination $dts -ErrorAction Stop
  Write-Host "reka-ui: patched index.d.cts -> index.d.ts"
} catch {
  Write-Warning "reka-ui patch failed: $_"
}
