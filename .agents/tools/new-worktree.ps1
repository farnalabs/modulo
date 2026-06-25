param(
    [Parameter(Mandatory=$true)]
    [string]$Branch
)

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$WorktreeDir = Join-Path -Path $RepoRoot -ChildPath ".agents\worktrees"
$WorktreePath = Join-Path -Path $WorktreeDir -ChildPath $Branch

if (Test-Path $WorktreePath) {
    Write-Output "WORKTREE_EXISTS:$WorktreePath"
    exit 0
}

if (-not (Test-Path $WorktreeDir)) {
    New-Item -ItemType Directory -Path $WorktreeDir -Force | Out-Null
}

$BranchExists = git -C $RepoRoot branch --list $Branch
if (-not $BranchExists) {
    git -C $RepoRoot branch $Branch main
}

git -C $RepoRoot worktree add $WorktreePath $Branch
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to create worktree for branch $Branch"
    exit 1
}

Write-Output "WORKTREE_CREATED:$WorktreePath"
