<#
.SYNOPSIS
  Pester tests for validate-manifest.ps1. Tests use temporary fixture files
  to avoid mutating the real manifest.yaml.
#>

BeforeAll {
    $script:ValidatorPath = Join-Path $PSScriptRoot ".." "validate-manifest.ps1"
    $script:RepoRoot = Resolve-Path (Join-Path $PSScriptRoot ".." "..")

    # Create a temp directory for test fixtures
    $script:TestDir = Join-Path $env:TEMP "modulo-manifest-test-$(Get-Random)"
    New-Item -ItemType Directory -Path $TestDir -Force | Out-Null

    # Create a minimal router file
    $routerDir = Join-Path $TestDir "frontend" "src" "router"
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
    $localesDir = Join-Path $TestDir "frontend" "src" "locales"
    New-Item -ItemType Directory -Path $localesDir -Force | Out-Null
    Set-Content -Path (Join-Path $localesDir "en-US.js") -Value @"
export default {
  "nav": {
    "dashboard": "Dashboard",
    "users": "Users"
  },
  "common": {
    "save": "Save",
    "cancel": "Cancel"
  }
}
"@

    # Create product-map directory with a stub
    $productMapDir = Join-Path $TestDir "docs" "product-map"
    New-Item -ItemType Directory -Path $productMapDir -Force | Out-Null
    Set-Content -Path (Join-Path $productMapDir "feat-users.md") -Value "---`nid: feat-users`n---"

    # Create a Vue template for element testid matching
    $vueDir = Join-Path $TestDir "frontend" "src" "views"
    New-Item -ItemType Directory -Path $vueDir -Force | Out-Null
    Set-Content -Path (Join-Path $vueDir "DashboardView.vue") -Value @'
<template>
  <div>
    <h1 data-testid="dashboard-title">Dashboard</h1>
    <div data-testid="dashboard-metrics-overview">metrics</div>
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

    # Helper to run the validator against a fixture manifest
    function Invoke-Validator($manifestContent) {
        $manifestPath = Join-Path $TestDir "frontend" "src" "manifest.yaml"
        $parent = Split-Path -Parent $manifestPath
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Set-Content -Path $manifestPath -Value $manifestContent

        # Temporarily mock the $PSScriptRoot by creating a wrapper that overrides the path
        $output = & $ValidatorPath 2>&1
        $exitCode = $LASTEXITCODE
        return @{ Output = $output; ExitCode = $exitCode }
    }
}

AfterAll {
    Remove-Item -LiteralPath $TestDir -Recurse -Force -ErrorAction SilentlyContinue
}

Describe "validate-manifest.ps1" {

    It "passes a well-formed manifest" {
        $result = Invoke-Validator @"
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
  /users:
    name: admin-users
    testid: page-users
    breadcrumb: Users
    parent: null
    product_map: null
    i18n_key: nav.users
    sidebar_group: core
    sidebar_order: 2
    type: list_page
elements:
  /:
    - testid: dashboard-title
      type: heading
      label: Dashboard Title
      dynamic_testid: false
    - testid: dashboard-metrics-overview
      type: section
      label: Metrics
      dynamic_testid: false
  /users:
    - testid: users-table
      type: table
      label: Users Table
      dynamic_testid: false
"@
        $result.ExitCode | Should -Be 0
    }

    It "catches a missing route name" {
        $result = Invoke-Validator @"
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
"@
        $result.ExitCode | Should -Be 1
        $result.Output | Select-String -SimpleMatch "does-not-exist" | Should -Not -Be $null
    }

    It "catches a missing element testid" {
        $result = Invoke-Validator @"
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
elements:
  /:
    - testid: nonexistent-element
      type: button
      label: Ghost
      dynamic_testid: false
"@
        $result.ExitCode | Should -Be 1
        $result.Output | Select-String -SimpleMatch "nonexistent-element" | Should -Not -Be $null
    }

    It "catches orphaned elements" {
        $result = Invoke-Validator @"
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
elements:
  /orphan-route:
    - testid: dashboard-title
      type: heading
      label: Orphan
      dynamic_testid: false
"@
        $result.ExitCode | Should -Be 1
        $result.Output | Select-String -SimpleMatch "orphan-route" | Should -Not -Be $null
    }

    It "catches circular parent chain" {
        $result = Invoke-Validator @"
schema_version: 1
routes:
  /a:
    name: dashboard
    testid: page-a
    breadcrumb: A
    parent: /b
    product_map: null
    i18n_key: nav.dashboard
    sidebar_group: core
    sidebar_order: 1
    type: page
  /b:
    name: admin-users
    testid: page-b
    breadcrumb: B
    parent: /a
    product_map: null
    i18n_key: nav.users
    sidebar_group: core
    sidebar_order: 2
    type: list_page
"@
        $result.ExitCode | Should -Be 1
        $result.Output | Select-String -SimpleMatch "Circular" | Should -Not -Be $null
    }

}
