"""Repo-vs-DB migration divergence guard (FAR-872 deliverable B).

Compares the DB's ``alembic_version.version_num`` against the set of
revisions present in the repo's migration tree.  When the DB has applied a
revision the repo does NOT ship (divergence), the check logs at ERROR and
surfaces the finding on the ``/healthz/ready`` migration check — never
crashes, never blocks startup.

Rationale: a negative grep for a column name is not evidence of absence.
The organisations audit-columns migration was renumbered across commits
and eventually dropped from the repo, while prod DB had already applied
it.  PR #553 then aligned the ORM to that non-existent migration,
producing 38 anonymous ``UndefinedColumnError`` failures.  This guard
makes such drift visible before it causes runtime failures.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DivergenceCheckResult:
    """Typed result of the repo-vs-DB migration divergence check."""

    diverged: bool
    db_revisions: set[str]
    repo_revisions: set[str]
    orphaned_revisions: set[str]
    detail: str


def _resolve_alembic_ini() -> Path:
    """Locate ``backend/alembic.ini`` robustly regardless of process cwd.

    Mirrors ``modulo.api.routes.health._resolve_alembic_ini``.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "alembic.ini"
        if candidate.exists():
            return candidate
    return Path("alembic.ini")


def _load_repo_revisions() -> set[str]:
    """Load all revision IDs from the repo's migration tree.

    Uses Alembic's ``ScriptDirectory`` to parse each migration file's
    ``revision`` and ``down_revision`` attributes — more robust than
    regex-based text scanning.

    Returns an EMPTY set when the tree cannot be loaded (fail-open).
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        alembic_ini = _resolve_alembic_ini()
        alembic_cfg = Config(str(alembic_ini))
        alembic_cfg.set_main_option(
            "script_location",
            str(alembic_ini.parent / "src" / "modulo" / "db" / "migrations"),
        )
        script = ScriptDirectory.from_config(alembic_cfg)
        all_revisions: set[str] = set()
        for walk in script.walk_revisions():
            all_revisions.add(walk.revision)
            if walk.down_revision:
                # down_revision can be a tuple (merge migrations) or a string.
                if isinstance(walk.down_revision, (list, tuple)):
                    for dr in walk.down_revision:
                        if dr:
                            all_revisions.add(dr)
                else:
                    all_revisions.add(walk.down_revision)
        return all_revisions
    except Exception:
        _log.exception("migration_guard._load_repo_revisions failed")
        return set()


def check_migration_divergence(applied_revisions: set[str]) -> DivergenceCheckResult:
    """Check whether the DB has applied revisions absent from the repo.

    ``applied_revisions`` is the set of ``version_num`` values currently
    in the DB's ``alembic_version`` table (multiple rows possible for
    merge-point databases).

    Returns a typed result — never raises.
    """
    repo_revisions = _load_repo_revisions()
    if not repo_revisions:
        return DivergenceCheckResult(
            diverged=False,
            db_revisions=applied_revisions,
            repo_revisions=set(),
            orphaned_revisions=set(),
            detail="Could not load repo migration tree; divergence check skipped",
        )

    orphaned = applied_revisions - repo_revisions
    if not orphaned:
        return DivergenceCheckResult(
            diverged=False,
            db_revisions=applied_revisions,
            repo_revisions=repo_revisions,
            orphaned_revisions=set(),
            detail="All applied revisions are present in the repo migration tree",
        )

    detail = (
        f"Migration divergence detected: DB has applied revision(s) "
        f"not present in the repo tree: {', '.join(sorted(orphaned))}. "
        f"DB revisions: {', '.join(sorted(applied_revisions))}. "
        f"Repo head revisions: {', '.join(sorted(repo_revisions))}"
    )
    _log.error("migration_divergence_detected %s", detail)
    return DivergenceCheckResult(
        diverged=True,
        db_revisions=applied_revisions,
        repo_revisions=repo_revisions,
        orphaned_revisions=orphaned,
        detail=detail,
    )
