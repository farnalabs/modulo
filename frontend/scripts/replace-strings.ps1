param(
  [string]$SourceDir = "src",
  [string]$LocaleFile = "src/locales/en-US.json",
  [switch]$DryRun
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$srcDir = Join-Path $projectRoot $SourceDir
$localePath = Join-Path $projectRoot $LocaleFile

Write-Host "Reading en-US.json..." -ForegroundColor Cyan

$raw = Get-Content -Path $localePath -Raw -Encoding UTF8 | ConvertFrom-Json

function Flatten-Json($obj, $prefix) {
  $result = @{}
  foreach ($prop in $obj.PSObject.Properties) {
    $key = $prop.Name
    $path = if ($prefix) { "$prefix.$key" } else { $key }
    $val = $prop.Value
    if ($val -is [string]) {
      $result[$val] = $path
    } elseif ($val -is [PSCustomObject]) {
      $nested = Flatten-Json $val $path
      foreach ($kv in $nested.GetEnumerator()) {
        $result[$kv.Key] = $kv.Value
      }
    }
  }
  return $result
}

$flatMap = Flatten-Json $raw ""
Write-Host "Loaded $($flatMap.Count) string mappings" -ForegroundColor Cyan

# ── Process .vue files ──
$vueFiles = Get-ChildItem -Path $srcDir -Recurse -Filter "*.vue" |
  Where-Object { $_.FullName -notmatch 'node_modules' -and $_.FullName -notmatch '\\ui\\' }

$totalReplacements = 0
$filesModified = 0

foreach ($file in $vueFiles) {
  $content = Get-Content -Path $file.FullName -Raw -Encoding UTF8
  $relPath = $file.FullName.Substring($srcDir.Length + 1).Replace('\', '/')
  $modified = $false

  # Only process <template> section
  $tm = [regex]::Match($content, '<template>(.*?)</template>', [Text.RegularExpressions.RegexOptions]::Singleline)
  if (-not $tm.Success) { continue }
  $template = $tm.Groups[1].Value
  $origTemplate = $template

  # Find all text nodes: >Text here<
  # Updated regex to handle whitespace between > and text
  $textNodes = [regex]::Matches($template, '>[ \t]*([A-Z][^<]{3,}?)[ \t]*<')

  foreach ($node in $textNodes) {
    $text = $node.Groups[1].Value.Trim()

    # Skip Vue expressions
    if ($text -match '^\{\{.*\}\}$') { continue }
    if ($text -match '^[a-z][a-z0-9_]*$') { continue } # lowercase single word

    # Look up in the flat map (try exact first, then with common prefix)
    $keyPath = $null
    if ($flatMap.ContainsKey($text)) {
      $keyPath = $flatMap[$text]
    }

    if (-not $keyPath) { continue }

    # Replace the text node, preserving whitespace
    $escaped = [regex]::Escape($node.Groups[0].Value)
    $replacement = ">{{ `$t('$keyPath') }}<"
    $newCount = ($template -split [regex]::Escape($node.Groups[0].Value)).Count - 1
    $template = $template -replace $escaped, $replacement
    $totalReplacements += $newCount
    $modified = $true
  }

  # ── Replace placeholder="Text" with :placeholder="$t('key')" ──
  $placeholderNodes = [regex]::Matches($template, 'placeholder="([^"]{3,})"')
  foreach ($node in $placeholderNodes) {
    $text = $node.Groups[1].Value.Trim()
    $keyPath = $flatMap[$text]
    if (-not $keyPath) { continue }

    $escaped = [regex]::Escape($node.Groups[0].Value)
    $template = $template -replace $escaped, "`:placeholder=`"`$t('$keyPath')`""
    $totalReplacements++
    $modified = $true
  }

  # ── Replace aria-label="Text" with :aria-label="$t('key')" ──
  $ariaNodes = [regex]::Matches($template, 'aria-label="([^"]{3,})"')
  foreach ($node in $ariaNodes) {
    $text = $node.Groups[1].Value.Trim()
    $keyPath = $flatMap[$text]
    if (-not $keyPath) { continue }

    $escaped = [regex]::Escape($node.Groups[0].Value)
    $template = $template -replace $escaped, "`:aria-label=`"`$t('$keyPath')`""
    $totalReplacements++
    $modified = $true
  }

  # ── Replace title="Text" with :title="$t('key')" ──
  $titleNodes = [regex]::Matches($template, '(?<!:)title="([^"]{3,})"')
  foreach ($node in $titleNodes) {
    $text = $node.Groups[1].Value.Trim()
    $keyPath = $flatMap[$text]
    if (-not $keyPath) { continue }

    $escaped = [regex]::Escape($node.Groups[0].Value)
    $template = $template -replace $escaped, "`:title=`"`$t('$keyPath')`""
    $totalReplacements++
    $modified = $true
  }

  if ($modified) {
    $content = $content.Substring(0, $tm.Index) + '<template>' + $template + '</template>' + $content.Substring($tm.Index + $tm.Length)
    if ($DryRun) {
      Write-Host "  [DRY RUN] Would modify: $relPath ($($totalReplacements) replacements)" -ForegroundColor Yellow
    } else {
      Set-Content -Path $file.FullName -Value $content -Encoding UTF8 -NoNewline
      Write-Host "  Modified: $relPath" -ForegroundColor Green
    }
    $filesModified++
  }
}

Write-Host "`nDone!" -ForegroundColor Green
Write-Host "  Files modified: $filesModified" -ForegroundColor Yellow
Write-Host "  Total replacements: $totalReplacements" -ForegroundColor Yellow
