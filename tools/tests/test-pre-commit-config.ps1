Describe "Pre-commit command portability" {
    BeforeAll {
        $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
        $config = Get-Content -Raw -LiteralPath (Join-Path $repoRoot ".pre-commit-config.yaml")
    }

    It "runs import-linter through the backend project environment" {
        $config | Should -Match '(?m)^\s*entry:\s*uv --directory backend run lint-imports\s*$'
    }

    It "does not wrap uv hooks in Bash" {
        $config | Should -Not -Match '(?m)^\s*entry:\s*(?:/bin/)?bash\b[^\r\n]*\buv\b'
    }

    It "runs the migration collision check through a worktree-aware wrapper" {
        $config | Should -Match '(?m)^\s*entry:\s*powershell -NoProfile -File tools/run-check-migration-heads\.ps1\s*$'
        Test-Path -LiteralPath (Join-Path $repoRoot "tools/run-check-migration-heads.ps1") | Should -BeTrue
    }
}
