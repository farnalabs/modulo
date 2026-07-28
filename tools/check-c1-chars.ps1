param([switch]$Fix)
$found = $false

# Check 1: UTF-8 BOM
Get-ChildItem -Path ".github/workflows" -Filter "*.yml" | ForEach-Object {
    $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191) {
        Write-Host "UTF-8 BOM found in $($_.Name)" -ForegroundColor Red
        $found = $true
        if ($Fix) {
            $content = [System.IO.File]::ReadAllText($_.FullName)
            [System.IO.File]::WriteAllText($_.FullName, $content, [System.Text.UTF8Encoding]::new($false))
            Write-Host "  Fixed: removed UTF-8 BOM" -ForegroundColor Green
        }
    }
}

# Check 2: C1 control characters (U+0080-U+009F) - never valid in workflow files
Get-ChildItem -Path ".github/workflows" -Filter "*.yml" | ForEach-Object {
    $content = Get-Content $_.FullName -Raw
    for ($i = 0x80; $i -le 0x9F; $i++) {
        $char = [char]$i
        if ($content.Contains($char)) {
            Write-Host "C1 control char U+$('{0:X4}' -f $i) found in $($_.Name)" -ForegroundColor Red
            $found = $true
            if ($Fix) {
                $fixed = $content -replace $char, ''
                Set-Content -Path $_.FullName -Value $fixed -NoNewline
                Write-Host "  Fixed: removed C1 char U+$('{0:X4}' -f $i)" -ForegroundColor Green
            }
        }
    }
}

# Check 3: Non-ASCII characters in workflow files (should be pure ASCII)
# Exclude legitimate Unicode that sometimes appears (em dash U+2014, en dash U+2013,
# checkmark U+2705, heavy checkmark U+2714, right arrow U+2192)
$allowlist = @(0x2014, 0x2013, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2026, 0x2705, 0x2713, 0x2714, 0x2192)
Get-ChildItem -Path ".github/workflows" -Filter "*.yml" | ForEach-Object {
    $content = Get-Content $_.FullName -Raw
    $lines = $content -split "`r`n|`n"
    for ($lineNum = 0; $lineNum -lt $lines.Count; $lineNum++) {
        $line = $lines[$lineNum]
        for ($col = 0; $col -lt $line.Length; $col++) {
            $cp = [int]$line[$col]
            if ($cp -gt 0x7E -and $allowlist -notcontains $cp) {
                Write-Host "Non-ASCII char U+$('{0:X4}' -f $cp) at $($_.Name):$($lineNum+1):$($col+1)" -ForegroundColor Red
                Write-Host "  Context: ...$($line.Substring([Math]::Max(0,$col-20), [Math]::Min(60,$line.Length-[Math]::Max(0,$col-20))))..." -ForegroundColor DarkYellow
                $found = $true
            }
        }
    }
}

if ($found) {
    Write-Host "`nWorkflow files should contain only ASCII characters. Non-ASCII chars can cause GitHub Actions parser failures." -ForegroundColor Yellow
    exit 1
}
Write-Host "No C1 control characters or BOMs found in .github/workflows/" -ForegroundColor Green
exit 0
