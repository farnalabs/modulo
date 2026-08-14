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
    $testQuery = Join-Path $TestDir "graph-query.ps1"
    $originalContent = Get-Content -Raw -LiteralPath $GraphQueryPath
    $testContent = $originalContent -replace [regex]::Escape('$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")'), "`$repoRoot='$TestDir'"
    Set-Content -Path $testQuery -Value $testContent

    function New-FixtureEntry {
        param([string]$Name, [string]$Id, [string[]]$Bdd, [string[]]$Depends)
        $front = "---`nid: $Id`nprd: 8.4`nstatus: partial`n"
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
}

Describe "graph-query -Impact" {
    It "lists downstream dependents of a feat id" {
        $result = Invoke-GraphQuery -Impact "feat-covered"
        $result.ExitCode | Should -Be 0
        $result.Output | Select-String -SimpleMatch "feat-leaf" | Should -Not -Be $null
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
}
