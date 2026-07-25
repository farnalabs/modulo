param([switch]$Fix)
$found = $false
$extensions = @("*.py","*.toml","*.yml","*.yaml","*.json","*.md","*.cfg","*.ini","*.ps1","*.vue","*.ts","*.js")
foreach ($ext in $extensions) {
    Get-ChildItem -Recurse -Filter $ext | Where-Object { -not $_.PSIsContainer } | ForEach-Object {
        try {
            $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
            if ($bytes.Length -ge 2) {
                # UTF-16 LE BOM
                if ($bytes[0] -eq 255 -and $bytes[1] -eq 254) {
                    Write-Host "UTF-16 LE BOM found: $($_.FullName)" -ForegroundColor Red
                    $found = $true
                    if ($Fix) {
                        $content = [System.IO.File]::ReadAllText($_.FullName)
                        [System.IO.File]::WriteAllText($_.FullName, $content, [System.Text.UTF8Encoding]::new($false))
                        Write-Host "  Fixed: converted to UTF-8" -ForegroundColor Green
                    }
                }
                # UTF-16 BE BOM
                elseif ($bytes[0] -eq 254 -and $bytes[1] -eq 255) {
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
if ($found) { exit 1 }
Write-Host "No UTF-16 encoded files found - all files are valid UTF-8" -ForegroundColor Green
exit 0
