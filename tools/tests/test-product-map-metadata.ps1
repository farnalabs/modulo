BeforeAll {
    . (Join-Path (Join-Path $PSScriptRoot "..") "product-map-metadata.ps1")
}

Describe "Product map PRD metadata parsing" {
    It "parses scalar comma-separated references" {
        (@(Get-ProductMapPrdReferences "id: example`nprd: 6.2, 12`nstatus: partial") -join ',') | Should -Be "6.2,12"
    }

    It "parses block-list references" {
        $frontmatter = "id: example`nprd:`n  - 9.2`n  - 9.4`nstatus: partial"
        (@(Get-ProductMapPrdReferences $frontmatter) -join ',') | Should -Be "9.2,9.4"
    }

    It "preserves an explicit N/A reference" {
        (@(Get-ProductMapPrdReferences "id: example`nprd: N/A`nstatus: partial") -join ',') | Should -Be "N/A"
    }

    It "recognises nested numbered PRD headings" {
        $metadata = Get-ProductMapPrdSections @("## 8. Features", "### 8.25 Error Tracking", "#### 8.25.1 Frontend Monitoring")
        $metadata.Sections.ContainsKey("8.25.1") | Should -Be $true
        $metadata.Names["8.25.1"] | Should -Be "Frontend Monitoring"
    }
}
