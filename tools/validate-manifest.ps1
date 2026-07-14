<#
.SYNOPSIS
  Validates frontend/src/manifest.yaml integrity. Every route name matches the
  Vue Router config, every static testid exists in a template, every product_map
  ref resolves, every i18n_key exists in en-US.js, no orphaned elements, no
  circular parent chains, and dynamic routes are fully specified.
  Exit code: 0 = clean, 1 = issues found.
#>
param([switch]$CI)
$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$ManifestPath = Join-Path (Join-Path (Join-Path $repoRoot "frontend") "src") "manifest.yaml"
$errors = 0

if (-not (Test-Path -LiteralPath $ManifestPath)) {
    Write-Host "ERROR: Manifest not found at $ManifestPath" -ForegroundColor Red
    exit 1
}

function Write-Err($msg) {
    $script:errors++
    if ($CI) {
        Write-Host "ERROR: $msg" -ForegroundColor Red
        exit 1
    } else {
        Write-Host "WARN: $msg" -ForegroundColor Yellow
    }
}

# Load manifest via Python (PyYAML) — the self-hosted runner has Python with deps
# Use a minimal cached environment instead of depending on whichever Python is
# first on PATH in the caller or pre-commit environment.
$jsonRaw = uv run --quiet --no-project --with pyyaml python -c "
import sys, json, yaml
with open(sys.argv[1], 'rb') as f:
    data = yaml.safe_load(f)
print(json.dumps(data))
" $ManifestPath 2>$null
if ($LASTEXITCODE -ne 0 -or -not $jsonRaw) {
    Write-Host "ERROR: Python YAML parsing failed. Ensure Python 3 + PyYAML is available." -ForegroundColor Red
    exit 1
}
$manifest = $jsonRaw | ConvertFrom-Json

# Helper: ensure routes is a hashtable or PSCustomObject we can enumerate
if ($manifest.routes -is [System.Management.Automation.PSCustomObject]) {
    $routeEntries = $manifest.routes.PSObject.Properties
} else {
    $routeEntries = $manifest.routes.GetEnumerator() | ForEach-Object { $_ }
}

# ---- Rule 1: Every route.name matches a Vue Router route name ----
Write-Host "Rule 1: Route names match router" -ForegroundColor Cyan
$routerNames = @()
$routerDir = Join-Path (Join-Path (Join-Path $repoRoot "frontend") "src") "router"
$routerFile = Get-ChildItem -Path $routerDir -Filter "*.ts" | Select-Object -First 1
if ($routerFile) {
    $routerNames = Select-String -Path $routerFile.FullName -Pattern "name:\s*'([a-z][a-z0-9-]*)'" | ForEach-Object { $_.Matches.Groups[1].Value }
}
foreach ($r in $routeEntries) {
    $routeName = if ($r.Value.name) { $r.Value.name } else { $null }
    if ($routeName -and ($routeName -notin $routerNames)) {
        Write-Err "Route '$routeName' ($($r.Name)) not found in router"
    }
}

# ---- Rule 2: Every static element.testid exists in Vue templates ----
Write-Host "Rule 2: Element testids exist in templates" -ForegroundColor Cyan
$vueRoot = Join-Path (Join-Path $repoRoot "frontend") "src"
$vueContents = Get-ChildItem -Path $vueRoot -Recurse -Filter "*.vue" | ForEach-Object {
    @{ Path = $_.FullName; Content = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 }
}
if ($manifest.elements -is [System.Management.Automation.PSCustomObject]) {
    $elementEntries = $manifest.elements.PSObject.Properties
} else {
    $elementEntries = $manifest.elements.GetEnumerator() | ForEach-Object { $_ }
}
foreach ($e in $elementEntries) {
    $routePath = $e.Name
    $elements = if ($e.Value -is [System.Management.Automation.PSCustomObject]) { $e.Value } else { @($e.Value) }
    foreach ($el in $elements) {
        if ($el.dynamic_testid -eq $true) { continue }
        $testid = $el.testid
        if (-not $testid) { continue }
        $found = $false
        foreach ($vf in $vueContents) {
            if ($vf.Content -match "data-testid=""$testid""" -or $vf.Content -match "setAttribute\('data-testid',\s*'$testid'\)" -or $vf.Content -match "\.dataset\.testid\s*=\s*'$testid'") {
                $found = $true
                break
            }
        }
        if (-not $found) {
            Write-Err "testid '$testid' on route $routePath not found in any template"
        }
    }
}

# ---- Rule 3: Every product_map references a file in docs/product-map/ ----
Write-Host "Rule 3: Product map refs exist" -ForegroundColor Cyan
$productMapDir = Join-Path (Join-Path $repoRoot "docs") "product-map"
$productMapFiles = Get-ChildItem -Path $productMapDir -Recurse -Filter "*.md" | ForEach-Object { $_.BaseName }
foreach ($r in $routeEntries) {
    $pm = $r.Value.product_map
    if ($pm -and ($pm -notin $productMapFiles)) {
        Write-Err "product_map '$pm' on $($r.Name) not found"
    }
}

# ---- Rule 4: Every i18n_key exists in en-US.js ----
Write-Host "Rule 4: i18n keys exist" -ForegroundColor Cyan
$i18nFile = Join-Path (Join-Path (Join-Path (Join-Path $repoRoot "frontend") "src") "locales") "en-US.js"
$topLevelKeys = @()
if (Test-Path -LiteralPath $i18nFile) {
    $i18nContent = Get-Content -LiteralPath $i18nFile -Raw -Encoding UTF8
    $topLevelKeys = [regex]::Matches($i18nContent, '(?m)^\s{2}"(\w+)":\s*\{') | ForEach-Object { $_.Groups[1].Value }
}
foreach ($r in $routeEntries) {
    $ik = $r.Value.i18n_key
    if ($ik) {
        $topKey = ($ik -split '\.')[0]
        if ($topKey -notin $topLevelKeys) {
            Write-Err "i18n_key '$ik' on $($r.Name): top-level namespace '$topKey' not found in en-US.js"
        }
    }
}

# ---- Rule 5: No orphaned elements ----
Write-Host "Rule 5: No orphaned elements" -ForegroundColor Cyan
$routePaths = @()
foreach ($r in $routeEntries) { $routePaths += $r.Name }
foreach ($e in $elementEntries) {
    if ($e.Name -notin $routePaths) {
        Write-Err "Elements block for '$($e.Name)' has no matching route"
    }
}

# ---- Rule 6: No circular parent chains ----
Write-Host "Rule 6: No circular parents" -ForegroundColor Cyan
function Check-Parent($path, $chain) {
    if ($chain.Count -gt 20) {
        Write-Err "Circular parent chain: $($chain -join ' -> ')"
        return
    }
    $route = $manifest.routes
    if ($route -is [System.Management.Automation.PSCustomObject]) {
        $routeObj = $route.$path
    } else {
        $routeObj = $route[$path]
    }
    if (-not $routeObj -or -not $routeObj.parent) { return }
    $parentVal = $routeObj.parent
    if ($parentVal -in $chain) {
        Write-Err "Circular parent: $($chain -join ' -> ') -> $parentVal"
        return
    }
    Check-Parent $parentVal ($chain + $parentVal)
}
foreach ($r in $routeEntries) {
    Check-Parent $r.Name @($r.Name)
}

# ---- Rule 7: Dynamic routes have pattern and dynamic_params ----
Write-Host "Rule 7: Dynamic routes complete" -ForegroundColor Cyan
foreach ($r in $routeEntries) {
    if ($r.Value.type -eq 'detail_page') {
        if (-not $r.Value.pattern) {
            Write-Err "Dynamic route '$($r.Name)' missing pattern"
        }
        if (-not $r.Value.dynamic_params) {
            Write-Err "Dynamic route '$($r.Name)' missing dynamic_params"
        }
    }
}

if ($errors -gt 0) {
    Write-Host "$errors validation errors" -ForegroundColor Red
    exit 1
}
Write-Host "All rules passed!" -ForegroundColor Green
