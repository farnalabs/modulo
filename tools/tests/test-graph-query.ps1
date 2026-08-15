<#
.SYNOPSIS
  Pester tests for graph-query.ps1: -Uncovered lists entries with empty/missing
  bdd, -Impact lists downstream dependents, -Depends lists upstream prereqs.
#>

BeforeAll {
    $toolsDir = Join-Path $PSScriptRoot ".."
    $script:GraphQueryPath = Join-Path $toolsDir "graph-query.ps1"
    $script:RepoRoot = Resolve-Path (Join-Path $toolsDir "..")

    $script:TestDir = Join-Path ([IO.Path]::GetTempPath()) "modulo-graph-query-test-$(Get-Random)"
    New-Item -ItemType Directory -Path $TestDir -Force | Out-Null

    # Copy the query tool into a fixture repo rooted at TestDir.
    Copy-Item -LiteralPath $GraphQueryPath -Destination $TestDir
    Copy-Item -LiteralPath (Join-Path $toolsDir "product-map-metadata.ps1") -Destination $TestDir
    $testQuery = Join-Path $TestDir "graph-query.ps1"
    $originalContent = Get-Content -Raw -LiteralPath $GraphQueryPath
    $testContent = $originalContent -replace [regex]::Escape('$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")'), "`$repoRoot='$TestDir'"
    Set-Content -Path $testQuery -Value $testContent

    function New-FixtureEntry {
        param([string]$Name, [string]$Id, [string[]]$Bdd, [string[]]$Depends, [switch]$Inline)
        $front = "---`nid: $Id`nprd: 8.4`nstatus: partial`n"
        if ($Inline) {
            $front += "bdd: [$($Bdd -join ', ')]`n"
            $front += "depends-on: [$($Depends -join ', ')]`n"
        } else {
            if ($Bdd.Count -gt 0) {
                $front += "bdd:`n"
                foreach ($b in $Bdd) { $front += "  - $b`n" }
            } else {
                $front += "bdd: []`n"
            }
            if ($Depends.Count -gt 0) {
                $front += "depends-on:`n"
                foreach ($d in $Depends) { $front += "  - $d`n" }
            } else {
                $front += "depends-on: []`n"
            }
        }
        $front += "---`n# $Name`n"
        $dir = Join-Path (Join-Path $TestDir "docs/product-map") $Name
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Set-Content -Path (Join-Path $dir "$Name.md") -Value $front
    }

    function Invoke-GraphQuery {
        param([switch]$Uncovered, [string]$Impact, [string]$Depends)
        $splat = @{}
        if ($Uncovered) { $splat["Uncovered"] = $true }
        if ($Impact) { $splat["Impact"] = $Impact }
        if ($Depends) { $splat["Depends"] = $Depends }
        $output = & $testQuery @splat *>&1
        return @{ Output = $output; ExitCode = $LASTEXITCODE }
    }

    New-FixtureEntry -Name "covered" -Id "feat-covered" -Bdd @("backend/tests/bdd/features/x.feature") -Depends @()
    New-FixtureEntry -Name "uncovered" -Id "feat-uncovered" -Bdd @() -Depends @()
    New-FixtureEntry -Name "leaf" -Id "feat-leaf" -Bdd @() -Depends @("feat-covered")
    New-FixtureEntry -Name "inlinefeat" -Id "feat-inline" -Bdd @("backend/tests/bdd/features/inline.feature") -Depends @("feat-covered") -Inline
}

AfterAll {
    Remove-Item -LiteralPath $TestDir -Recurse -Force -ErrorAction SilentlyContinue
}

Describe "graph-query -Uncovered" {
    It "lists entries with empty or missing bdd coverage" {
        $result = Invoke-GraphQuery -Uncovered
        $result.ExitCode | Should -Be 1
        $result.Output | Select-String -SimpleMatch "feat-uncovered" | Should -Not -Be $null
    }

    It "does not list entries with bdd coverage" {
        $result = Invoke-GraphQuery -Uncovered
        $result.Output | Select-String -SimpleMatch "feat-covered" | Should -Be $null
    }

    It "parses inline-array bdd and does not flag the entry" {
        $result = Invoke-GraphQuery -Uncovered
        $result.Output | Select-String -SimpleMatch "feat-inline" | Should -Be $null
    }
}

Describe "graph-query -Uncovered on a clean repo" {
    It "exits 0 when every entry has bdd coverage" {
        $cleanDir = Join-Path ([IO.Path]::GetTempPath()) "modulo-graph-query-clean-$(Get-Random)"
        New-Item -ItemType Directory -Path $cleanDir -Force | Out-Null
        try {
            Copy-Item -LiteralPath $GraphQueryPath -Destination $cleanDir
            Copy-Item -LiteralPath (Join-Path $toolsDir "product-map-metadata.ps1") -Destination $cleanDir
            $cleanQuery = Join-Path $cleanDir "graph-query.ps1"
            $cleanContent = Get-Content -Raw -LiteralPath $GraphQueryPath
            $cleanTestContent = $cleanContent -replace [regex]::Escape('$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")'), "`$repoRoot='$cleanDir'"
            Set-Content -Path $cleanQuery -Value $cleanTestContent
            $cleanEntryDir = Join-Path (Join-Path $cleanDir "docs") "product-map"
            New-Item -ItemType Directory -Path $cleanEntryDir -Force | Out-Null
            $cleanFront = "---`nid: feat-clean`nprd: 8.4`nstatus: partial`nbdd:`n  - backend/tests/bdd/features/x.feature`n---"
            Set-Content -Path (Join-Path $cleanEntryDir "clean.md") -Value $cleanFront
            $output = & $cleanQuery -Uncovered *>&1
            $output | Select-String -SimpleMatch "None - every entry has bdd coverage" | Should -Not -Be $null
            $LASTEXITCODE | Should -Be 0
        } finally {
            Remove-Item -LiteralPath $cleanDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe "graph-query -Impact" {
    It "lists downstream dependents of a feat id" {
        $result = Invoke-GraphQuery -Impact "feat-covered"
        $result.ExitCode | Should -Be 0
        $result.Output | Select-String -SimpleMatch "feat-leaf" | Should -Not -Be $null
    }

    It "parses inline-array depends-on when finding dependents" {
        $result = Invoke-GraphQuery -Impact "feat-covered"
        $result.ExitCode | Should -Be 0
        $result.Output | Select-String -SimpleMatch "feat-inline" | Should -Not -Be $null
    }

    It "reports an unknown id with exit 1" {
        $result = Invoke-GraphQuery -Impact "feat-unknown"
        $result.ExitCode | Should -Be 1
        $result.Output | Select-String -SimpleMatch "not a known" | Should -Not -Be $null
    }
}

Describe "graph-query -Depends" {
    It "lists upstream dependencies of a feat id" {
        $result = Invoke-GraphQuery -Depends "feat-leaf"
        $result.ExitCode | Should -Be 0
        $result.Output | Select-String -SimpleMatch "feat-covered" | Should -Not -Be $null
    }

    It "parses inline-array depends-on when listing dependencies" {
        $result = Invoke-GraphQuery -Depends "feat-inline"
        $result.ExitCode | Should -Be 0
        $result.Output | Select-String -SimpleMatch "feat-covered" | Should -Not -Be $null
    }

    It "exits 1 for a known id with no dependencies (matches -Impact convention)" {
        $result = Invoke-GraphQuery -Depends "feat-covered"
        $result.ExitCode | Should -Be 1
        $result.Output | Select-String -SimpleMatch "None." | Should -Not -Be $null
    }
}

Describe "graph-query argument contract" {
    It "rejects passing both -Impact and -Depends" {
        $result = Invoke-GraphQuery -Impact "feat-covered" -Depends "feat-leaf"
        $result.ExitCode | Should -Be 1
        $result.Output | Select-String -SimpleMatch "only one of -Impact or -Depends" | Should -Not -Be $null
    }
}
