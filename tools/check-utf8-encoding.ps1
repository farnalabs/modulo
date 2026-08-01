param([switch]$Fix)
$found = $false
$extensions = @("*.py","*.toml","*.yml","*.yaml","*.json","*.md","*.cfg","*.ini","*.ps1","*.vue","*.ts","*.js")

# Scan only repo-tracked files (git ls-files) — never walk the filesystem
# recursively. A recursive byte-scan descends into .venv / node_modules
# (hundreds of MB) and stalls the pre-commit hook for 10+ minutes.
$tracked = git ls-files 2>$null
if (-not $tracked) {
    Write-Host "No tracked files to check" -ForegroundColor Green
    exit 0
}

foreach ($rel in $tracked) {
    $ext = "*" + [System.IO.Path]::GetExtension($rel)
    if ($extensions -notcontains $ext) { continue }
    $full = Join-Path (Get-Location) $rel
    if (-not (Test-Path -LiteralPath $full)) { continue }
    try {
        $bytes = [System.IO.File]::ReadAllBytes($full)
        if ($bytes.Length -ge 3) {
            # UTF-8 BOM (EF BB BF)
            if ($bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191) {
                $isWorkflow = $rel -match [regex]::Escape(".github/workflows/")
                if ($isWorkflow) {
                    Write-Host "BLOCKING: UTF-8 BOM in workflow file: $rel" -ForegroundColor Red
                    $found = $true
                } else {
                    Write-Host "Non-blocking BOM: $rel" -ForegroundColor DarkYellow
                }
                if ($Fix) {
                    $content = [System.IO.File]::ReadAllText($full)
                    [System.IO.File]::WriteAllText($full, $content, [System.Text.UTF8Encoding]::new($false))
                    Write-Host "  Fixed: removed UTF-8 BOM" -ForegroundColor Green
                }
            }
        }
        if ($bytes.Length -ge 2) {
            if ($bytes[0] -eq 255 -and $bytes[1] -eq 254) {
                Write-Host "UTF-16 LE BOM found: $rel" -ForegroundColor Red
                $found = $true
                if ($Fix) {
                    $content = [System.IO.File]::ReadAllText($full)
                    [System.IO.File]::WriteAllText($full, $content, [System.Text.UTF8Encoding]::new($false))
                    Write-Host "  Fixed: converted to UTF-8" -ForegroundColor Green
                }
            } elseif ($bytes[0] -eq 254 -and $bytes[1] -eq 255) {
                Write-Host "UTF-16 BE BOM found: $rel" -ForegroundColor Red
                $found = $true
                if ($Fix) {
                    $content = [System.IO.File]::ReadAllText($full)
                    [System.IO.File]::WriteAllText($full, $content, [System.Text.UTF8Encoding]::new($false))
                    Write-Host "  Fixed: converted to UTF-8" -ForegroundColor Green
                }
            }
        }
    } catch {
        # Skip files we can't read
    }
}
if ($found) {
    Write-Host "`nBlocking: workflow files with BOMs found. Use -Fix to auto-remove." -ForegroundColor Yellow
    exit 1
}
Write-Host "No blocking encoding issues found" -ForegroundColor Green
exit 0
