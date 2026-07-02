param(
  [string]$LocaleFile = "src/locales/en-US.json",
  [string]$OutputFile = "src/locales/debug.json"
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$localePath = Join-Path $projectRoot $LocaleFile
$outputPath = Join-Path $projectRoot $OutputFile

Write-Host "Reading en-US.json..." -ForegroundColor Cyan

$raw = Get-Content -Path $localePath -Raw -Encoding UTF8
$obj = $raw | ConvertFrom-Json

function Wrap-Values($node, $prefix) {
  if ($node -is [PSCustomObject]) {
    $result = @{}
    foreach ($prop in $node.PSObject.Properties) {
      $path = if ($prefix) { "$prefix.$($prop.Name)" } else { $prop.Name }
      if ($prop.Value -is [string]) {
        $result[$prop.Name] = "[==$path==]"
      } elseif ($prop.Value -is [PSCustomObject]) {
        $result[$prop.Name] = Wrap-Values $prop.Value $path
      } else {
        $result[$prop.Name] = $prop.Value
      }
    }
    return [PSCustomObject]$result
  }
  return $node
}

$debugObj = Wrap-Values $obj ""

$json = $debugObj | ConvertTo-Json -Depth 20
Set-Content -Path $outputPath -Value $json -Encoding UTF8

Write-Host "Done! Created $outputPath" -ForegroundColor Green
Write-Host "To use: add 'debug' to SUPPORTED_LOCALES in src/i18n/index.ts" -ForegroundColor Yellow
Write-Host "Then switch the app to debug locale. Any visible raw English" -ForegroundColor Yellow
Write-Host "text (not wrapped in [==...==]) is missing from en-US.json." -ForegroundColor Yellow
