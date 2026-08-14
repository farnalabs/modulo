function ConvertFrom-ProductMapFrontmatter {
    param([Parameter(Mandatory)][string]$Frontmatter, [string]$Path, [string]$Name)
    $id = if ($Frontmatter -match '(?m)^id:\s*(\S+)') { $Matches[1] } else { $null }
    $bdd = @()
    if ($Frontmatter -match '(?m)^bdd:\s*(.+?)[\r\n]') {
        $bList = $Matches[1].Trim()
        if ($bList -match '^\[') { $bdd = $bList -replace '[\[\]" ]', '' -split ',' }
    }
    if ($Frontmatter -match '(?m)^bdd:\s*\n((?:\s+- .+\n?)+)') {
        $bBlock = $Matches[1] -split '\n' | ForEach-Object { ($_ -replace '^\s*-\s*', '' -replace '"', '' -replace "'", '' -replace '#.*', '').Trim() } | Where-Object { $_ }
        if ($bBlock) { $bdd = @($bdd) + $bBlock }
    }
    $dep = @()
    if ($Frontmatter -match '(?m)^depends-on:\s*\[(.*?)\]') { $dep = $Matches[1] -replace ' ', '' -split ',' }
    if ($Frontmatter -match '(?m)^depends-on:\s*\n((?:\s+- .+\n?)+)') {
        $depBlock = $Matches[1] -split '\n' | ForEach-Object { ($_ -replace '^\s*-\s*', '' -replace '"', '' -replace "'", '' -replace '#.*', '').Trim() } | Where-Object { $_ }
        $dep = @($dep + $depBlock) | Where-Object { $_ }
    }
    return @{
        id = $id
        bdd = @($bdd | Where-Object { $_ })
        depends = @($dep | Where-Object { $_ })
        path = $Path
        name = $Name
    }
}

function Get-ProductMapEntries {
    param([Parameter(Mandatory)][string]$ProductMapDir)
    $entries = @()
    Get-ChildItem -Recurse -Filter "*.md" -LiteralPath $ProductMapDir | Where-Object { $_.Name -ne "_index.md" } | ForEach-Object {
        $c = Get-Content -Raw -Encoding UTF8 -LiteralPath $_.FullName
        if ($c -notmatch '(?s)^---[\r\n]+(.+?)[\r\n]+---') { return }
        $entries += ConvertFrom-ProductMapFrontmatter -Frontmatter $Matches[1] -Path $_.FullName -Name $_.Name
    }
    return ,$entries
}

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
