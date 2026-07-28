param([switch]$Fix)
$found = $false
$extensions = @("*.py","*.toml","*.yml","*.yaml","*.json","*.md","*.cfg","*.ini","*.ps1","*.vue","*.ts","*.js")
foreach ($ext in $extensions) {
    Get-ChildItem -Recurse -Filter $ext | Where-Object { -not $_.PSIsContainer } | ForEach-Object {
        try {
            $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
            if ($bytes.Length -ge 3) {
                # UTF-8 BOM (EF BB BF)
                if ($bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191) {
                    $isWorkflow = $_.FullName -match [regex]::Escape(".github\workflows\")
                    if ($isWorkflow) {
                        Write-Host "BLOCKING: UTF-8 BOM in workflow file: $($_.FullName)" -ForegroundColor Red
                        $found = $true
                    } else {
                        Write-Host "Non-blocking BOM: $($_.FullName)" -ForegroundColor DarkYellow
                    }
                    if ($Fix) {
                        $content = [System.IO.File]::ReadAllText($_.FullName)
                        [System.IO.File]::WriteAllText($_.FullName, $content, [System.Text.UTF8Encoding]::new($false))
                        Write-Host "  Fixed: removed UTF-8 BOM" -ForegroundColor Green
                    }
                }
            }
            if ($bytes.Length -ge 2) {
                if ($bytes[0] -eq 255 -and $bytes[1] -eq 254) {
                    Write-Host "UTF-16 LE BOM found: $($_.FullName)" -ForegroundColor Red
                    $found = $true
                    if ($Fix) {
                        $content = [System.IO.File]::ReadAllText($_.FullName)
                        [System.IO.File]::WriteAllText($_.FullName, $content, [System.Text.UTF8Encoding]::new($false))
                        Write-Host "  Fixed: converted to UTF-8" -ForegroundColor Green
                    }
                } elseif ($bytes[0] -eq 254 -and $bytes[1] -eq 255) {
                    Write-Host "UTF-16 BE BOM found: $($_.FullName)" -ForegroundColor Red
                    $found = $true
                    if ($Fix) {
                        $content = [System.IO.File]::ReadAllText($_.FullName)
                        [System.IO.File]::WriteAllText($_.FullName, $content, [System.Text.UTF8Encoding]::new($false))
                        Write-Host "  Fixed: converted to UTF-8" -ForegroundColor Green
                    }
                }
            }
        } catch {
            # Skip files we can't read
        }
    }
}
if ($found) {
    Write-Host "`nBlocking: workflow files with BOMs found. Use -Fix to auto-remove." -ForegroundColor Yellow
    exit 1
}
Write-Host "No blocking encoding issues found" -ForegroundColor Green
exit 0
