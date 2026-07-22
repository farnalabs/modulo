param([switch]$Fix)
$found = $false
Get-ChildItem -Path ".github/workflows" -Filter "*.yml" | ForEach-Object {
    $content = Get-Content $_.FullName -Raw
    if ($content -match '\u009D') {
        Write-Host "C1 control char U+009D found in $($_.Name)" -ForegroundColor Red
        $found = $true
        if ($Fix) {
            $fixed = $content -replace '\u009D', ''
            Set-Content -Path $_.FullName -Value $fixed -NoNewline
            Write-Host "  Fixed: removed C1 chars" -ForegroundColor Green
        }
    }
}
if ($found) { exit 1 }
Write-Host "No C1 control characters found in .github/workflows/" -ForegroundColor Green
exit 0