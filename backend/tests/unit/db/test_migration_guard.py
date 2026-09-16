"""Unit tests for FAR-872 migration divergence guard.

Tests the ``migration_guard`` module: divergence detection when the DB has
applied revisions absent from the repo, and clean detection when all applied
revisions are present.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from modulo.db.migration_guard import (
    DivergenceCheckResult,
    _load_repo_revisions,
    check_migration_divergence,
)


class TestLoadRepoRevisions:
    """Loading revisions from the repo's migration tree."""

    def test_returns_set(self) -> None:
        revisions = _load_repo_revisions()
        assert isinstance(revisions, set)

    def test_contains_known_revision(self) -> None:
        """The repo's migration tree must contain at least one revision."""
        revisions = _load_repo_revisions()
        assert len(revisions) > 0


class _FakeWalk:
    """Minimal stand-in for an Alembic revision walk node."""

    def __init__(self, revision: str, down_revision: str | tuple[str, ...] | None = None) -> None:
        self.revision = revision
        self.down_revision = down_revision


class TestLoadRepoRevisionsEdgeCases:
    """FAR-872 fail-open + merge-tuple handling for the repo revision loader."""

    def test_merge_tuple_down_revisions_are_collected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.db.migration_guard as mg

        monkeypatch.setattr(mg, "_REPO_REVISIONS", None)
        walks = [
            _FakeWalk("0003", ("0001", "0002")),
            _FakeWalk("0001"),
        ]
        fake_script = MagicMock()
        fake_script.walk_revisions.return_value = walks
        with patch("alembic.script.ScriptDirectory.from_config", return_value=fake_script):
            revisions = _load_repo_revisions()
        assert revisions == {"0001", "0002", "0003"}

    def test_tree_load_failure_returns_empty_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.db.migration_guard as mg

        monkeypatch.setattr(mg, "_REPO_REVISIONS", None)
        with patch("alembic.script.ScriptDirectory.from_config", side_effect=RuntimeError("boom")):
            assert not _load_repo_revisions()


class TestCheckMigrationDivergence:
    """Divergence check between DB and repo."""

    def test_clean_when_applied_subset_of_repo(self) -> None:
        """No divergence when all applied revisions exist in the repo."""
        fake_repo = {"0001", "0002", "0003"}
        with patch(
            "modulo.db.migration_guard._load_repo_revisions",
            return_value=fake_repo,
        ):
            result = check_migration_divergence({"0001", "0002"})
        assert result.diverged is False
        assert not result.orphaned_revisions

    def test_diverged_when_applied_not_in_repo(self) -> None:
        """Divergence when DB has a revision the repo does not ship."""
        fake_repo = {"0001", "0002"}
        with patch(
            "modulo.db.migration_guard._load_repo_revisions",
            return_value=fake_repo,
        ):
            result = check_migration_divergence({"0001", "0002", "0099"})
        assert result.diverged is True
        assert "0099" in result.orphaned_revisions
        assert "not present in the repo tree" in result.detail

    def test_clean_when_applied_matches_repo_exactly(self) -> None:
        """No divergence when applied exactly matches repo revisions."""
        fake_repo = {"0001", "0002"}
        with patch(
            "modulo.db.migration_guard._load_repo_revisions",
            return_value=fake_repo,
        ):
            result = check_migration_divergence({"0001", "0002"})
        assert result.diverged is False

    def test_empty_applied_is_clean(self) -> None:
        """An empty DB (no applied revisions) is never diverged."""
        fake_repo = {"0001"}
        with patch(
            "modulo.db.migration_guard._load_repo_revisions",
            return_value=fake_repo,
        ):
            result = check_migration_divergence(set())
        assert result.diverged is False

    def test_repo_load_failure_returns_clean(self) -> None:
        """When repo tree cannot be loaded, divergence check is skipped (fail-open)."""
        with patch(
            "modulo.db.migration_guard._load_repo_revisions",
            return_value=set(),
        ):
            result = check_migration_divergence({"0001", "0099"})
        assert result.diverged is False
        assert "skipped" in result.detail

    def test_returns_typed_result(self) -> None:
        """Result is a DivergenceCheckResult dataclass."""
        fake_repo = {"0001"}
        with patch(
            "modulo.db.migration_guard._load_repo_revisions",
            return_value=fake_repo,
        ):
            result = check_migration_divergence({"0001"})
        assert isinstance(result, DivergenceCheckResult)
        assert hasattr(result, "diverged")
        assert hasattr(result, "db_revisions")
        assert hasattr(result, "repo_revisions")
        assert hasattr(result, "orphaned_revisions")
        assert hasattr(result, "detail")

    def test_multiple_orphans_reported(self) -> None:
        """Multiple orphaned revisions are all reported."""
        fake_repo = {"0001"}
        with patch(
            "modulo.db.migration_guard._load_repo_revisions",
            return_value=fake_repo,
        ):
            result = check_migration_divergence({"0001", "0098", "0099"})
        assert result.diverged is True
        assert result.orphaned_revisions == {"0098", "0099"}
