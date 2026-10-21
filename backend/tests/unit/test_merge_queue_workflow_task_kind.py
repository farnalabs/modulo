"""Structural gate: merge-queue.yml must classify conflict-class Branch Fixer
dispatches with the structural ``taskKind`` marker pr-conflict-reconcile.yml
already sends (FAR-2033).

Only a live merge-queue tick with a real conflict can prove the field reaches
the Branch Fixer, and CI never exercises that path. This test is the gate that
keeps the payload honest: the two conflict-class dispatches (merge-conflict and
migration-collision) must carry ``taskKind: conflict``, while the genuine
CI-failure dispatch must keep omitting it — an absent marker is what selects
the CI-failure profile.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "merge-queue.yml"

CONFLICT_STEP = "- name: Notify fixer of conflicts"
MIGRATION_STEP = "- name: Notify fixer of migration collisions (PRs excluded from the batch)"
CI_FAILURE_STEP = "- name: Notify branch-fixer on CI failure"


def _raw() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow() -> dict:
    return yaml.safe_load(_raw())


def _step_block(marker: str) -> str:
    """Raw text of one step, up to the next step at the same indentation.

    Scoping to the step matters: all three dispatches carry a ``phase``, so a
    file-wide substring check would pass even if only one of them gained the
    marker.
    """
    raw = _raw()
    start = raw.index(marker)
    rest = raw[start + len(marker) :]
    end = rest.find("\n      - name: ")
    return rest if end == -1 else rest[:end]


def test_workflow_file_exists_and_parses() -> None:
    assert WORKFLOW.exists(), f"Workflow file missing: {WORKFLOW}"
    wf = _workflow()
    assert isinstance(wf, dict), "YAML root is not a mapping"


def test_merge_conflict_dispatch_carries_conflict_task_kind() -> None:
    """FAR-2033: 13 budget-hit runs carried ``phase=merge-conflict`` and no
    ``taskKind``, so the receiver had to guess the task kind and the conflict
    work inherited the CI-failure profile."""
    block = _step_block(CONFLICT_STEP)
    assert '--arg phase "merge-conflict"' in block, "merge-conflict phase marker missing"
    assert '--arg taskKind "conflict"' in block, "merge-conflict dispatch omits --arg taskKind (FAR-2033)"
    assert "taskKind: $taskKind" in block, "merge-conflict jq object omits the taskKind key (FAR-2033)"


def test_migration_collision_dispatch_carries_conflict_task_kind() -> None:
    """FAR-2033: 8 budget-hit runs carried ``phase=migration-collision`` with no
    ``taskKind``. A migration-number collision is conflict-class work
    (renumber + rebase/merge main), not a generic CI-failure fix."""
    block = _step_block(MIGRATION_STEP)
    assert '--arg phase "migration-collision"' in block, "migration-collision phase marker missing"
    assert '--arg taskKind "conflict"' in block, "migration-collision dispatch omits --arg taskKind (FAR-2033)"
    assert "taskKind: $taskKind" in block, "migration-collision jq object omits the taskKind key (FAR-2033)"


def test_ci_failure_dispatch_deliberately_omits_task_kind() -> None:
    """The CI-failure dispatch must keep no ``taskKind``: an absent marker is
    what selects the CI-failure retry ladder. Adding it would change the
    receiving profile for genuine CI failures — FAR-2025/FAR-2032 territory."""
    block = _step_block(CI_FAILURE_STEP)
    assert '--arg phase "ci-failure"' in block, "ci-failure phase marker missing"
    assert "--arg taskKind" not in block, "CI-failure dispatch must not set a taskKind (FAR-2033)"
    assert "taskKind: $taskKind" not in block, "CI-failure jq object must not carry a taskKind (FAR-2033)"
