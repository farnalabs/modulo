[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [string]$Version,

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$productRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..\Product")
$backendPyproject = Join-Path $productRoot "backend\pyproject.toml"
$frontendPackage = Join-Path $productRoot "frontend\package.json"
$licenseFile = Join-Path $productRoot "LICENSE"

$changeDate = (Get-Date).AddYears(3).ToString("yyyy-MM-dd")

function Log($msg) {
    Write-Host "[release] $msg"
}

function Warn($msg) {
    Write-Host "[release] WARNING: $msg" -ForegroundColor Yellow
}

# --- Validate version format ---
if ($Version -notmatch '^\d+\.\d+\.\d+') {
    throw "Version must be semver (e.g. 1.2.3), got: $Version"
}
$tag = "v$Version"
Log "Preparing release $tag (Change Date: $changeDate)"

if ($DryRun) {
    Log "DRY RUN — no files or git changes will be made"
}

# --- 1. Update LICENSE ---
Log "Updating LICENSE (version + Change Date)..."
if (-not (Test-Path -LiteralPath $licenseFile)) {
    Warn "LICENSE not found at $licenseFile — skipping"
} elseif (-not $DryRun) {
    $content = Get-Content -Raw -Encoding UTF8 -LiteralPath $licenseFile
    if ($content -match '(The Licensed Work is Modulo, version )\S+') {
        $content = $content -replace '(The Licensed Work is Modulo, version )\S+', "`$1$Version"
    } else {
        Warn "Could not find version pattern in LICENSE"
    }
    if ($content -match '(Change Date: )\S+') {
        $content = $content -replace '(Change Date: )\S+', "`$1$changeDate"
    } else {
        Warn "Could not find Change Date pattern in LICENSE"
    }
    Set-Content -Encoding UTF8 -NoNewline -LiteralPath $licenseFile -Value $content
    Log "  LICENSE updated to version $Version, Change Date $changeDate"
}

# --- 2. Update backend/pyproject.toml ---
Log "Updating backend/pyproject.toml version..."
if (-not (Test-Path -LiteralPath $backendPyproject)) {
    Warn "backend/pyproject.toml not found — skipping"
} elseif (-not $DryRun) {
    $content = Get-Content -Raw -Encoding UTF8 -LiteralPath $backendPyproject
    if ($content -match 'version = "\S+"') {
        $content = $content -replace 'version = "\S+"', "version = `"$Version`""
        Set-Content -Encoding UTF8 -NoNewline -LiteralPath $backendPyproject -Value $content
        Log "  backend/pyproject.toml updated to $Version"
    } else {
        Warn "Could not find version in backend/pyproject.toml"
    }
}

# --- 3. Update frontend/package.json ---
Log "Updating frontend/package.json version..."
if (-not (Test-Path -LiteralPath $frontendPackage)) {
    Warn "frontend/package.json not found — skipping"
} elseif (-not $DryRun) {
    $content = Get-Content -Raw -Encoding UTF8 -LiteralPath $frontendPackage
    if ($content -match '"version": "\S+"') {
        $content = $content -replace '"version": "\S+"', "`"version`": `"$Version`""
        Set-Content -Encoding UTF8 -NoNewline -LiteralPath $frontendPackage -Value $content
        Log "  frontend/package.json updated to $Version"
    } else {
        Warn "Could not find version in frontend/package.json"
    }
}

# --- 4. Update docs/prd.md version header ---
$prdFile = Join-Path $productRoot "docs\prd.md"
Log "Updating PRD version header..."
if (-not (Test-Path -LiteralPath $prdFile)) {
    Warn "$prdFile not found — skipping"
} elseif (-not $DryRun) {
    $content = Get-Content -Raw -Encoding UTF8 -LiteralPath $prdFile
    $today = (Get-Date).ToString("yyyy-MM-dd")
    if ($content -match '\*\*Version\*\*: \S+') {
        $content = $content -replace '\*\*Version\*\*: \S+', "**Version**: $Version"
    }
    if ($content -match '\*\*Date\*\*: \S+') {
        $content = $content -replace '\*\*Date\*\*: \S+', "**Date**: $today"
    }
    Set-Content -Encoding UTF8 -NoNewline -LiteralPath $prdFile -Value $content
    Log "  PRD version header updated to $Version ($today)"
}

# --- 5. Git tag ---
if (-not $DryRun) {
    Push-Location $productRoot
    try {
        $existing = git tag -l "$tag"
        if ($existing) {
            Warn "Tag $tag already exists — skipping tag creation"
        } else {
            git add LICENSE backend/pyproject.toml frontend/package.json docs/prd.md
            git commit -m "release: $tag"
            git tag -a "$tag" -m "Modulo $tag"
            Log "Committed and tagged $tag"
        }
    } finally {
        Pop-Location
    }
}

# --- 6. Placeholder: Docker Hub publish ---
# TODO: docker buildx build --platform linux/amd64,linux/arm64 -t farnalabs/modulo:$tag -t farnalabs/modulo:latest .
# TODO: docker push farnalabs/modulo:$tag && docker push farnalabs/modulo:latest
Log "[PLACEHOLDER] Docker Hub publish — not yet implemented"

# --- 7. Placeholder: GitHub release ---
# TODO: gh release create $tag --title "Modulo $tag" --notes "See changelog in docs/prd.md"
Log "[PLACEHOLDER] GitHub release — not yet implemented"

# --- 8. Placeholder: npm publish (frontend lib if applicable) ---
Log "[PLACEHOLDER] npm publish — not yet implemented"

# --- 9. Push ---
Log "Run 'git push origin main --tags' to push the release"

if ($DryRun) {
    Log "DRY RUN complete — no changes made"
} else {
    Log "Release $tag prepared. Review the commit and tag, then push."
}
