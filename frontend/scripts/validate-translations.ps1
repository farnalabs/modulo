param(
  [string]$NavPath = (Join-Path $PSScriptRoot "..\src\config\navigation.ts"),
  [string]$LocalesPath = (Join-Path $PSScriptRoot "..\src\locales\en-US.js")
)

$navContent = Get-Content -LiteralPath $NavPath -Raw
$localesContent = Get-Content -LiteralPath $LocalesPath -Raw

# Extract all routeLabelKeyMap values (the i18n keys referenced by route names)
$routeKeyMatches = [regex]::Matches($navContent, "'components\.SidebarNav\.([^']+)'")
$referencedKeys = @{}
foreach ($m in $routeKeyMatches) {
  $key = $m.Groups[1].Value
  if ($key -like 'item_*') {
    $referencedKeys[$key] = $true
  }
}

# Extract all SidebarNav item keys defined in en-US.js
$definedKeyMatches = [regex]::Matches($localesContent, '"item_([^"]+)"')
$definedKeys = @{}
foreach ($m in $definedKeyMatches) {
  $definedKeys["item_" + $m.Groups[1].Value] = $true
}

$missing = @()
foreach ($key in $referencedKeys.Keys) {
  if (-not $definedKeys.ContainsKey($key)) {
    $missing += $key
  }
}

if ($missing.Count -eq 0) {
  Write-Host "All routeLabelKeyMap entries have matching SidebarNav keys in en-US.js" -ForegroundColor Green
  exit 0
} else {
  Write-Host "Missing SidebarNav keys in en-US.js:" -ForegroundColor Red
  foreach ($key in $missing | Sort-Object) {
    Write-Host "  components.SidebarNav.$key" -ForegroundColor Yellow
  }
  exit 1
}
