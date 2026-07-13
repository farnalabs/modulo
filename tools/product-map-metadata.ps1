function Get-ProductMapPrdReferences {
    param([Parameter(Mandatory)][string]$Frontmatter)
    $references = @()
    $inPrdList = $false
    foreach ($line in ($Frontmatter -split '\r?\n')) {
        if ($line -match '^prd:\s*(.*)$') {
            $inPrdList = $true
            $value = $Matches[1].Trim()
            if ($value) {
                $references += $value -split ',' | ForEach-Object { $_.Trim().Trim('"').Trim("'") }
                $inPrdList = $false
            }
            continue
        }
        if ($inPrdList -and $line -match '^\s+-\s+(.+?)\s*$') {
            $references += $Matches[1].Trim().Trim('"').Trim("'")
            continue
        }
        if ($inPrdList -and $line -match '^[A-Za-z][A-Za-z0-9-]*:') { break }
    }
    return @($references | Where-Object { $_ })
}

function Get-ProductMapPrdSections {
    param([Parameter(Mandatory)][AllowEmptyString()][string[]]$Lines)
    $sections = @{}
    $coverageSections = @{}
    $names = @{}
    foreach ($line in $Lines) {
        if ($line -match '^(#{2,6})\s+(\d+(?:\.\d+)*[a-z]?)(?:\.)?(?:\s+(.+))?$') {
            $level = $Matches[1].Length
            $section = $Matches[2]
            $sections[$section] = $true
            $names[$section] = $Matches[3].Trim()
            if ($level -le 3) { $coverageSections[$section] = $true }
        }
    }
    return @{ Sections = $sections; CoverageSections = $coverageSections; Names = $names }
}
