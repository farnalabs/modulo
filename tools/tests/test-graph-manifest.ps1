<#
.SYNOPSIS
  Pester tests for graph-manifest integration: graph-validate.ps1 calls
  validate-manifest.ps1, and all product_map refs in manifest resolve.
#>

BeforeAll {
    $toolsDir = Join-Path $PSScriptRoot ".."
    $script:GraphValidatorPath = Join-Path $toolsDir "graph-validate.ps1"
    $script:ManifestValidatorPath = Join-Path $toolsDir "validate-manifest.ps1"
    $script:RepoRoot = Resolve-Path (Join-Path $toolsDir "..")

    $script:TestDir = Join-Path $env:TEMP "modulo-graph-manifest-test-$(Get-Random)"
    New-Item -ItemType Directory -Path $TestDir -Force | Out-Null

    # Create a minimal router file
    $routerDir = Join-Path (Join-Path (Join-Path $TestDir "frontend") "src") "router"
    New-Item -ItemType Directory -Path $routerDir -Force | Out-Null
    Set-Content -Path (Join-Path $routerDir "index.ts") -Value @"
import { createRouter } from 'vue-router'
const router = createRouter({
  routes: [
    { path: '/', name: 'dashboard', component: {} },
    { path: '/users', name: 'admin-users', component: {} },
    { path: '/runs/:id', name: 'run-detail', component: {} },
  ]
})
export default router
"@

    # Create locales
    $localesDir = Join-Path (Join-Path (Join-Path $TestDir "frontend") "src") "locales"
    New-Item -ItemType Directory -Path $localesDir -Force | Out-Null
    Set-Content -Path (Join-Path $localesDir "en-US.js") -Value @"
export default {
  "nav": {
    "dashboard": "Dashboard",
    "users": "Users",
    "run_detail": "Run Detail"
  },
  "common": {
    "save": "Save",
    "cancel": "Cancel"
  }
}
"@

    # Create the minimum graph inputs and entries matching manifest product_map refs.
    $productMapDir = Join-Path (Join-Path $TestDir "docs") "product-map"
    New-Item -ItemType Directory -Path $productMapDir -Force | Out-Null
    Set-Content -Path (Join-Path $productMapDir "feat-dashboard.md") -Value "---`nid: feat-dashboard`nprd: N/A`nstatus: covered`n---"
    Set-Content -Path (Join-Path $productMapDir "feat-users.md") -Value "---`nid: feat-users`nprd: N/A`nstatus: covered`n---"
    Set-Content -Path (Join-Path $productMapDir "feat-runs.md") -Value "---`nid: feat-runs`nprd: N/A`nstatus: covered`n---"
    Set-Content -Path (Join-Path (Join-Path $TestDir "docs") "prd.md") -Value "# Fixture PRD"
    $routesDir = Join-Path (Join-Path (Join-Path (Join-Path (Join-Path $TestDir "backend") "src") "modulo") "api") "routes"
    New-Item -ItemType Directory -Path $routesDir -Force | Out-Null

    # Create Vue templates for element testid matching
    $vueDir = Join-Path (Join-Path (Join-Path $TestDir "frontend") "src") "views"
    New-Item -ItemType Directory -Path $vueDir -Force | Out-Null
    Set-Content -Path (Join-Path $vueDir "DashboardView.vue") -Value @'
<template>
  <div>
    <h1 data-testid="dashboard-title">Dashboard</h1>
  </div>
</template>
'@
    Set-Content -Path (Join-Path $vueDir "AdminUsersView.vue") -Value @'
<template>
  <div>
    <div data-testid="users-table">table</div>
  </div>
</template>
'@
    Set-Content -Path (Join-Path $vueDir "RunDetailView.vue") -Value @'
<template>
  <div data-testid="run-detail-container">run detail</div>
</template>
'@

    # Create a temporary copy of graph-validate.ps1 that uses our TestDir as repo root
    $script:TestGraphValidator = Join-Path $TestDir "graph-validate-test.ps1"
    $originalContent = Get-Content -Raw -LiteralPath $GraphValidatorPath
    $testContent = $originalContent -replace [regex]::Escape('$repoRoot=Resolve-Path (Join-Path $PSScriptRoot "..")'), "`$repoRoot='$TestDir'"
    Set-Content -Path $TestGraphValidator -Value $testContent
    Copy-Item -LiteralPath (Join-Path $toolsDir "product-map-metadata.ps1") -Destination $TestDir

    # Create a temporary copy of validate-manifest.ps1 that uses our TestDir
    $script:TestManifestValidator = Join-Path $TestDir "validate-manifest.ps1"
    $origManifestContent = Get-Content -Raw -LiteralPath $ManifestValidatorPath
    $testManifestContent = $origManifestContent -replace [regex]::Escape('$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")'), "`$repoRoot='$TestDir'"
    Set-Content -Path $TestManifestValidator -Value $testManifestContent

    # Helper to run graph-validate against a fixture manifest
    function Invoke-GraphValidator {
        param([string]$ManifestContent)
        $manifestPath = Join-Path (Join-Path (Join-Path $TestDir "frontend") "src") "manifest.yaml"
        $parent = Split-Path -Parent $manifestPath
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Set-Content -Path $manifestPath -Value $ManifestContent
        $output = & $TestGraphValidator *>&1
        $exitCode = $LASTEXITCODE
        return @{ Output = $output; ExitCode = $exitCode }
    }

    # Helper to run validate-manifest against a fixture manifest
    function Invoke-ManifestValidator {
        param([string]$ManifestContent)
        $manifestPath = Join-Path (Join-Path (Join-Path $TestDir "frontend") "src") "manifest.yaml"
        $parent = Split-Path -Parent $manifestPath
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Set-Content -Path $manifestPath -Value $ManifestContent
        $output = & $TestManifestValidator *>&1
        $exitCode = $LASTEXITCODE
        return @{ Output = $output; ExitCode = $exitCode }
    }

    # Shared well-formed manifest used by multiple tests
    $script:WellFormedManifest = @"
schema_version: 1
routes:
  /:
    name: dashboard
    testid: page-dashboard
    breadcrumb: Dashboard
    parent: null
    product_map: feat-dashboard
    i18n_key: nav.dashboard
    sidebar_group: core
    sidebar_order: 1
    type: page
  /users:
    name: admin-users
    testid: page-users
    breadcrumb: Users
    parent: null
    product_map: feat-users
    i18n_key: nav.users
    sidebar_group: core
    sidebar_order: 2
    type: list_page
  /runs/:id:
    name: run-detail
    testid: page-run-detail
    breadcrumb: Run Detail
    parent: null
    product_map: feat-runs
    i18n_key: nav.run_detail
    sidebar_group: core
    sidebar_order: 3
    type: detail_page
    pattern: /runs/:id
    dynamic_params:
      - id
elements:
  /:
    - testid: dashboard-title
      type: heading
      label: Dashboard Title
      dynamic_testid: false
  /users:
    - testid: users-table
      type: table
      label: Users Table
      dynamic_testid: false
"@
}

AfterAll {
    Remove-Item -LiteralPath $TestDir -Recurse -Force -ErrorAction SilentlyContinue
}

Describe "graph-validate.ps1 calls validate-manifest.ps1" {

    It "calls validate-manifest and passes when manifest is valid" {
        $result = Invoke-GraphValidator $WellFormedManifest
        $result.Output | Select-String -SimpleMatch "Manifest validation" | Should -Not -Be $null
        $result.ExitCode | Should -Be 0
    }

    It "fails when manifest has a missing route name" {
        $badManifest = @"
schema_version: 1
routes:
  /bogus:
    name: does-not-exist
    testid: page-bogus
    breadcrumb: Bogus
    parent: null
    product_map: null
    i18n_key: nav.dashboard
    sidebar_group: core
    sidebar_order: 1
    type: page
elements: {}
"@
        $result = Invoke-GraphValidator $badManifest
        $result.Output | Select-String -SimpleMatch "Manifest validation" | Should -Not -Be $null
        $result.ExitCode | Should -Be 1
    }
}

Describe "Product map refs in manifest resolve" {

    It "passes when all product_map refs exist" {
        $result = Invoke-ManifestValidator $WellFormedManifest
        $result.ExitCode | Should -Be 0
    }

    It "fails when a product_map ref has no corresponding entry" {
        $badManifest = @"
schema_version: 1
routes:
  /:
    name: dashboard
    testid: page-dashboard
    breadcrumb: Dashboard
    parent: null
    product_map: feat-nonexistent
    i18n_key: nav.dashboard
    sidebar_group: core
    sidebar_order: 1
    type: page
elements: {}
"@
        $result = Invoke-ManifestValidator $badManifest
        $result.ExitCode | Should -Be 1
        $result.Output | Select-String -SimpleMatch "feat-nonexistent" | Should -Not -Be $null
    }

    It "passes when product_map is null" {
        $nullMapManifest = @"
schema_version: 1
routes:
  /:
    name: dashboard
    testid: page-dashboard
    breadcrumb: Dashboard
    parent: null
    product_map: null
    i18n_key: nav.dashboard
    sidebar_group: core
    sidebar_order: 1
    type: page
elements: {}
"@
        $result = Invoke-ManifestValidator $nullMapManifest
        $result.ExitCode | Should -Be 0
    }
}
