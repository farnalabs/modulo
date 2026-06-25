param(
    [Parameter(Position=0)]
    [ValidateSet("list", "show", "start", "complete", "block")]
    [string]$Command = "list",
    [Parameter(Position=1)]
    [string]$TaskId,
    [Parameter(Position=2)]
    [string]$Evidence
)

$planPath = Join-Path -Path (git rev-parse --show-toplevel 2>$null) -ChildPath "../harness/delivery/delivery-plan.json"
if (-not (Test-Path $planPath)) {
    $planPath = Join-Path -Path $env:USERPROFILE -ChildPath "Dropbox/Modulo/harness/delivery/delivery-plan.json"
}

$plan = Get-Content -Raw -LiteralPath $planPath | ConvertFrom-Json

switch ($Command) {
    "list" {
        Write-Output "=== All Tasks ==="
        $plan.tasks | Sort-Object phaseName, id | Format-Table id, status, phaseName, dependsOn, effortEstimateMinutes
    }
    "show" {
        if (-not $TaskId) { Write-Error "Usage: task.ps1 show <taskId>"; exit 1 }
        $task = $plan.tasks | Where-Object id -eq $TaskId
        if (-not $task) { Write-Error "Task $TaskId not found"; exit 1 }
        Write-Output "=== $($task.id) ==="
        Write-Output "Status:     $($task.status)"
        Write-Output "Phase:      $($task.phaseName)"
        Write-Output "Estimate:   $($task.effortEstimateMinutes) min"
        Write-Output "Depends on: $($task.dependsOn -join ', ')"
        Write-Output "Notes:      $($task.notes)"
        if ($task.completedAt) { Write-Output "Completed:  $($task.completedAt)" }
        if ($task.verificationEvidence) { Write-Output "Evidence:   $($task.verificationEvidence.evidence)" }
    }
    "start" {
        if (-not $TaskId) { Write-Error "Usage: task.ps1 start <taskId>"; exit 1 }
        $task = $plan.tasks | Where-Object id -eq $TaskId
        if (-not $task) { Write-Error "Task $TaskId not found"; exit 1 }
        if ($task.status -ne "pending") { Write-Error "Task $TaskId is already '$($task.status)'"; exit 1 }

        # Check dependencies
        $unmet = @()
        foreach ($depId in $task.dependsOn) {
            $dep = $plan.tasks | Where-Object id -eq $depId
            if (-not $dep -or $dep.status -ne "completed") { $unmet += $depId }
        }
        if ($unmet.Count -gt 0) {
            Write-Error "Unmet dependencies: $($unmet -join ', ')"
            exit 1
        }

        # Check working tree is clean
        $status = git status --porcelain 2>$null
        if ($status) {
            Write-Error "Working tree is not clean. Commit or stash changes first before starting a task."
            Write-Output $status
            exit 1
        }

        Write-Output "Starting task $TaskId..."
        Write-Output "ACTION REQUIRED: Update delivery-plan.json: set '$TaskId.status' to 'in_progress'"
    }
    "complete" {
        if (-not $TaskId) { Write-Error "Usage: task.ps1 complete <taskId> -Evidence '...'"; exit 1 }
        # Check working tree is clean
        $status = git status --porcelain 2>$null
        if ($status) {
            Write-Error "Working tree is not clean. Commit all changes before marking complete."
            Write-Output $status
            exit 1
        }
        Write-Output "ACTION REQUIRED: Update delivery-plan.json for '$TaskId':"
        Write-Output "  - Set status to 'completed'"
        Write-Output "  - Set completedAt to current timestamp"
        if ($Evidence) { Write-Output "  - Set verificationEvidence.evidence to '$Evidence'" }
        Write-Output ""
        Write-Output "Also: create a worktree or branch for the next in_progress task BEFORE starting work."
    }
    "block" {
        if (-not $TaskId) { Write-Error "Usage: task.ps1 block <taskId> -Evidence '...'"; exit 1 }
        Write-Output "ACTION REQUIRED: Update delivery-plan.json for '$TaskId':"
        Write-Output "  - Set status to 'blocked'"
        if ($Evidence) { Write-Output "  - Set verificationEvidence.evidence to '$Evidence'" }
    }
}
