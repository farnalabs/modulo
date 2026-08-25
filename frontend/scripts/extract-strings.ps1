param(
  [string]$SourceDir = "src",
  [string]$OutputFile = "src/locales/en-US.json"
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$srcDir = Join-Path $projectRoot $SourceDir
$vueFiles = Get-ChildItem -Path $srcDir -Recurse -Filter "*.vue" |
  Where-Object { $_.FullName -notmatch 'node_modules' -and $_.FullName -notmatch '\\ui\\' }

Write-Host "Scanning $($vueFiles.Count) .vue files..." -ForegroundColor Cyan

$allTexts = [System.Collections.ArrayList]@()
[string[]]$dropList = @(
  '^[a-z0-9_\-\./]+$',           # identifiers
  '^[A-Z_]+$',                    # CONSTANTS
  '^[a-z]+$',                     # single lowercase words (CSS classes, attrs)
  '^\$\{.*\}$',                   # template literal expressions
  '^https?://',                   # URLs
  '^/api/',                       # API endpoints
  '^[\(\)\[\]{}]+$',             # brackets only
  '^[\.\,\;\:\!\?\-]+$',         # punctuation only
  '^[\d\.,%]+$',                 # numbers
  '^bg-[\w\-/]+$',               # Tailwind bg classes
  '^text-[\w\-/]+$',             # Tailwind text classes
  '^border-[\w\-/]+$',           # Tailwind border classes
  '^hover:',                      # Tailwind hover
  '^focus:',                      # Tailwind focus
  '^dark:',                       # Tailwind dark
  '^md:',                         # Tailwind responsive
  '^translate-',                  # Tailwind translate
  '^-translate-',                 # Tailwind negative translate
  '^rotate-',                     # Tailwind rotate
  '^scale-',                      # Tailwind scale
  '^gap-',                        # Tailwind gap
  '^p[xytrlbs]?-',               # Tailwind padding
  '^m[xytrlbs]?-',               # Tailwind margin
  '^w-',                          # Tailwind width
  '^h-',                          # Tailwind height
  '^min-',                        # Tailwind min-*
  '^max-',                        # Tailwind max-*
  '^flex',                        # Tailwind flex
  '^grid',                        # Tailwind grid
  '^items-',                      # Tailwind align
  '^justify-',                    # Tailwind justify
  '^self-',                       # Tailwind self
  '^rounded',                     # Tailwind rounded
  '^shadow',                      # Tailwind shadow
  '^opacity',                     # Tailwind opacity
  '^z-',                          # Tailwind z-index
  '^space-',                      # Tailwind space
  '^divide-',                     # Tailwind divide
  '^overflow',                    # Tailwind overflow
  '^truncate',                    # Tailwind truncate
  '^list-',                       # Tailwind list
  '^font-',                       # Tailwind font
  '^tracking-',                   # Tailwind tracking
  '^leading-',                    # Tailwind leading
  '^float-',                      # Tailwind float
  '^clear-',                      # Tailwind clear
  '^object-',                     # Tailwind object
  '^select-',                     # Tailwind select
  '^appearance',                  # Tailwind appearance
  '^indent-',                     # Tailwind indent
  '^col-',                        # Tailwind col/columns
  '^row-',                        # Tailwind row
  '^order-',                      # Tailwind order
  '^decoration-',                 # Tailwind decoration
  '^underline',                   # Tailwind underline
  '^uppercase',                   # Tailwind uppercase
  '^lowercase',                   # Tailwind lowercase
  '^capitalize',                  # Tailwind capitalize
  '^normal-',                     # Tailwind normal
  '^tab[s]?',                     # Tailwind tab
  '^box-',                        # Tailwind box
  '^block',                       # Tailwind block
  '^inline',                      # Tailwind inline
  '^table',                       # Tailwind table
  '^contents',                    # Tailwind contents
  '^hidden'                       # Tailwind hidden
)

function Is-UserText($t) {
  if ($t.Length -lt 3) { return $false }
  if ($t -match '^\{\{.*\}\}$') { return $false }

  foreach ($pat in $dropList) {
    if ($t -match $pat) { return $false }
  }

  # Must contain at least one letter
  if ($t -notmatch '[a-zA-Z]') { return $false }

  # Must look like natural language text
  # Either: has an uppercase letter, or is multi-word, or contains punctuation
  $hasUpper = [regex]::IsMatch($t, '[A-Z]')
  $isMultiWord = ($t -split '\s+').Count -ge 2
  $hasSentence = $t -match '[\.\!\?]$'
  $hasApostrophe = $t -match "'"
  $hasAmpersand = $t -match '&'

  if (-not ($hasUpper -or $isMultiWord -or $hasSentence -or $hasApostrophe -or $hasAmpersand)) {
    return $false
  }

  return $true
}

foreach ($file in $vueFiles) {
  $content = Get-Content -Path $file.FullName -Raw -Encoding UTF8
  $content = $content.TrimStart("`u{FEFF}")
  $relPath = $file.FullName.Substring($srcDir.Length + 1).Replace('\', '/')
  $relKey = $relPath -replace '\.vue$', ''

  # ── Template section ──
  $tm = [regex]::Match($content, '<template>(.*?)</template>', [Text.RegularExpressions.RegexOptions]::Singleline)
  if ($tm.Success) {
    $template = $tm.Groups[1].Value

    # Text nodes between HTML tags
    $textNodes = [regex]::Matches($template, '>([A-Z][^<]{2,})<')
    foreach ($node in $textNodes) {
      $t = $node.Groups[1].Value.Trim()
      if (Is-UserText $t) {
        [void]$allTexts.Add(@{text = $t; file = $relKey; source = 'text'})
      }
    }

    # Attributes: placeholder, aria-label, title, label
    $attrPats = @(
      'placeholder="([^"]+)"',
      'aria-label="([^"]+)"',
      'title="([^"]+)"'
    )
    foreach ($pat in $attrPats) {
      $matchResults = [regex]::Matches($template, $pat)
      foreach ($m in $matchResults) {
        $t = $m.Groups[1].Value.Trim()
        if (Is-UserText $t) {
          [void]$allTexts.Add(@{text = $t; file = $relKey; source = 'attr'})
        }
      }
    }

    # Template literals in Vue expressions (e.g. :placeholder="`Text ${var}`")
    $tlMatches = [regex]::Matches($template, '`([^`]+)`')
    foreach ($m in $tlMatches) {
      $full = $m.Groups[1].Value
      # Split by ${...} to get static text parts
      $parts = $full -split '\$\{[^}]+\}'
      foreach ($part in $parts) {
        $t = $part.Trim()
        if (Is-UserText $t) {
          [void]$allTexts.Add(@{text = $t; file = $relKey; source = 'template_literal'})
        }
      }
    }

    # {{ expr ? 'Text' : 'Text' }} patterns
    $ternMatches = [regex]::Matches($template, "'([A-Z][^']{2,})'")
    foreach ($m in $ternMatches) {
      $t = $m.Groups[1].Value.Trim()
      if ($t.Length -lt 3) { continue }
      if ($t -match '^[a-z]+$') { continue }
      [void]$allTexts.Add(@{text = $t; file = $relKey; source = 'ternary'})
    }
  }

  # ── Script section ──
  $sm = [regex]::Match($content, '<script[^>]*>(.*?)</script>', [Text.RegularExpressions.RegexOptions]::Singleline)
  if ($sm.Success) {
    $scriptContent = $sm.Groups[1].Value

    # Error/value message assignments
    $errPats = @(
      'error\.value\s*=\s*`([^`]+)`',
      'err\.value\s*=\s*`([^`]+)`',
      'passError\.value\s*=\s*`([^`]+)`',
      'passError\.value\s*=\s*"([^"]+)"',
      'passSuccess\.value\s*=\s*"([^"]+)"',
      'adderror\.value\s*=\s*`([^`]+)`',
      'editerror\.value\s*=\s*`([^`]+)`',
      'skillError\.value\s*=\s*`([^`]+)`'
    )
    foreach ($pat in $errPats) {
      $matchResults = [regex]::Matches($scriptContent, $pat)
      foreach ($m in $matchResults) {
        $val = $m.Groups[1].Value
        # For template literals, split by ${...}
        $parts = $val -split '\$\{[^}]+\}'
        foreach ($part in $parts) {
          $t = $part.Trim() -replace '^:\s*', ''
          if ($t.Length -ge 4 -and $t -match '[A-Z]') {
            [void]$allTexts.Add(@{text = $t; file = $relKey; source = 'error'})
          }
        }
      }
    }

    # Return/throw strings
    $retPats = @(
      'return\s+"([^"]{4,})"',
      'throw\s+"([^"]{4,})"',
      'throw new Error\("([^"]{4,})"\)'
    )
    foreach ($pat in $retPats) {
      $matchResults = [regex]::Matches($scriptContent, $pat)
      foreach ($m in $matchResults) {
        $t = $m.Groups[1].Value.Trim()
        if ($t.Length -ge 4 -and $t -match '[A-Z]') {
          [void]$allTexts.Add(@{text = $t; file = $relKey; source = 'return'})
        }
      }
    }

    # Ternary/judgment expressions with quoted strings
    $quotPats = @(
      "'([A-Z][^']{3,}?)'",
      '"([A-Z][^"]{3,}?)"'
    )
    foreach ($pat in $quotPats) {
      $matchResults = [regex]::Matches($scriptContent, $pat)
      foreach ($m in $matchResults) {
        $t = $m.Groups[1].Value.Trim()
        if ($t.Length -lt 3 -or $t.Length -gt 100) { continue }
        if ($t -match '^[a-z][a-z0-9_\-\./]+$') { continue }
        if ($t -match '^https?://') { continue }
        [void]$allTexts.Add(@{text = $t; file = $relKey; source = 'string'})
      }
    }
  }
}

Write-Host "Raw extractions: $($allTexts.Count)" -ForegroundColor Cyan

# ── Deduplicate ──
$unique = [Ordered]@{}
foreach ($item in $allTexts) {
  $t = $item.text
  if (-not $unique.Contains($t)) { $unique[$t] = $item }
}

Write-Host "Unique strings: $($unique.Count)" -ForegroundColor Cyan

# ── Common string map ──
$commonKeys = @{
  'Save' = 'common.save'
  'Cancel' = 'common.cancel'
  'Delete' = 'common.delete'
  'Edit' = 'common.edit'
  'Create' = 'common.create'
  'Add' = 'common.add'
  'Remove' = 'common.remove'
  'Close' = 'common.close'
  'Loading' = 'common.loading'
  'Loading...' = 'common.loading_ellipsis'
  'Saving' = 'common.saving'
  'Saving...' = 'common.saving_ellipsis'
  'Search' = 'common.search'
  'Filter' = 'common.filter'
  'Submit' = 'common.submit'
  'Reset' = 'common.reset'
  'Back' = 'common.back'
  'Next' = 'common.next'
  'Confirm' = 'common.confirm'
  'Name' = 'common.name'
  'Email' = 'common.email'
  'Password' = 'common.password'
  'Role' = 'common.role'
  'Status' = 'common.status'
  'Type' = 'common.type'
  'Description' = 'common.description'
  'Actions' = 'common.actions'
  'Active' = 'common.active'
  'Inactive' = 'common.inactive'
  'Error' = 'common.error'
  'Success' = 'common.success'
  'Warning' = 'common.warning'
  'Info' = 'common.info'
  'Enabled' = 'common.enabled'
  'Disabled' = 'common.disabled'
  'All' = 'common.all'
  'None' = 'common.none'
  'Unknown' = 'common.unknown'
  'Optional' = 'common.optional'
  'Required' = 'common.required'
  'Save Changes' = 'common.save_changes'
  'Are you sure?' = 'common.are_you_sure'
  'Search...' = 'common.search_ellipsis'
  'No results' = 'common.no_results'
  'No data' = 'common.no_data'
}

$output = @{}
$seenInCommon = @{}

# Add common strings
$outCommon = @{}
foreach ($kv in $commonKeys.GetEnumerator()) {
  if ($unique.ContainsKey($kv.Key)) {
    $parts = $kv.Value -split '\.'
    if ($parts.Length -eq 2) {
      $outCommon[$parts[1]] = $kv.Key
    }
    $seenInCommon[$kv.Key] = $true
  }
}
if ($outCommon.Count -gt 0) {
  $output['common'] = $outCommon
}

# Group remaining strings by file path
$byFile = @{}
foreach ($kv in $unique.GetEnumerator()) {
  if ($seenInCommon.ContainsKey($kv.Key)) { continue }
  $firstFile = $kv.Value.file

  # Determine category
  if ($firstFile -match '^views/(.+?)(/|$)') {
    $category = "views.views_$($Matches[1])"
  } elseif ($firstFile -match '^components/(.+?)(/|$)') {
    $category = $firstFile -replace '/', '.'
  } else {
    $category = $firstFile -replace '/', '.'
  }
  $category = $category -replace '\.\.', '.' -replace '\.vue', ''
  # Remove 'index' suffix
  $category = $category -replace '\.index$', ''
  $category = $category -replace '^views\.views_', 'views.'

  if (-not $byFile.ContainsKey($category)) { $byFile[$category] = @{} }

  $text = $kv.Key
  $key = $text -replace '[^a-zA-Z0-9\s]', '' -replace '\s+', '_' -replace '_+', '_' -replace '^_|_$', ''
  $key = $key.Substring(0, [Math]::Min($key.Length, 60)).ToLower()

  # Ensure uniqueness within category
  $counter = 1
  $origKey = $key
  while ($byFile[$category].ContainsKey($key)) {
    $key = "${origKey}_$counter"
    $counter++
  }
  $byFile[$category][$key] = $text
}

# Merge into output structure
foreach ($kv in $byFile.GetEnumerator()) {
  $path = $kv.Key -split '\.'
  $current = $output
  $pathParts = @()
  foreach ($part in $path) {
    $pathParts += $part
    if (-not $current.ContainsKey($part)) { $current[$part] = @{} }
    $current = $current[$part]
  }
  foreach ($sk in $kv.Value.Keys) {
    $current[$sk] = $kv.Value[$sk]
  }
}

# Write output
$json = $output | ConvertTo-Json -Depth 20 -Compress:$false
$outputPath = Join-Path $projectRoot $OutputFile
$null = New-Item -ItemType Directory -Path (Split-Path -Parent $outputPath) -Force
$json | Set-Content -Path $outputPath -Encoding UTF8

Write-Host "Done! Wrote $outputPath" -ForegroundColor Green
Write-Host "  Common strings: $($outCommon.Count)" -ForegroundColor Yellow
Write-Host "  File-based groups: $($byFile.Count)" -ForegroundColor Yellow
Write-Host "  Total unique strings: $($unique.Count)" -ForegroundColor Yellow
