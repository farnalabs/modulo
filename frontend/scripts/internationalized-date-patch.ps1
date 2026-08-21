$mjs = "node_modules\@internationalized\date\dist\index.mjs"
if (-not (Test-Path $mjs)) {
  $js = "node_modules\@internationalized\date\dist\index.js"
  if (Test-Path $js) {
    Copy-Item $js $mjs
    Write-Host "@internationalized/date: index.mjs patched from index.js"
  }
}
