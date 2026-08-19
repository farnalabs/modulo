"""Tests for TOCTOU hardening (#1376, #1105).

Covers:
- RateLimitConflictError domain exception
- ErrorNotificationRule model has deleted_at column and partial unique index
- Migration 0117 file exists and is well-formed
"""

from __future__ import annotations

import uuid
from pathlib import Path

from modulo.core.exceptions import RateLimitConflictError

_MIGRATION_FILE = (
    Path(__file__).parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / "0117_toctou_hardening.py"
)


# ---------------------------------------------------------------------------
# RateLimitConflictError
# ---------------------------------------------------------------------------


class TestRateLimitConflictError:
    def test_stores_attributes(self) -> None:
        pid = uuid.uuid4()
        exc = RateLimitConflictError(pipeline_id=pid, rate_limit_key="key:abc")
        assert exc.pipeline_id == pid
        assert exc.rate_limit_key == "key:abc"
        assert "key:abc" in str(exc)

    def test_defaults_are_none(self) -> None:
        exc = RateLimitConflictError()
        assert exc.pipeline_id is None
        assert exc.rate_limit_key is None


# ---------------------------------------------------------------------------
# ErrorNotificationRule — deleted_at column + index
# ---------------------------------------------------------------------------


class TestErrorNotificationRuleDeletedAt:
    def test_model_has_deleted_at_column(self) -> None:
        from modulo.db.models.error_notification_rule import ErrorNotificationRule

        col = ErrorNotificationRule.__table__.c.deleted_at
        assert col.nullable is True

    def test_partial_unique_index_exists(self) -> None:
        from modulo.db.models.error_notification_rule import ErrorNotificationRule

        index_names = [idx.name for idx in ErrorNotificationRule.__table__.indexes]
        assert "uq_enr_org_active" in index_names


# ---------------------------------------------------------------------------
# Migration file exists and chains correctly
# ---------------------------------------------------------------------------


class TestMigration0117:
    def test_migration_file_exists(self) -> None:
        assert _MIGRATION_FILE.exists(), f"Migration not found at {_MIGRATION_FILE}"

    def test_migration_chain(self) -> None:
        content = _MIGRATION_FILE.read_text()
        assert 'revision: str = "0117_toctou_hardening"' in content
        assert 'down_revision: str | None = "0116_guardrail_trust_pr_b"' in content

    def test_migration_has_both_indexes(self) -> None:
        content = _MIGRATION_FILE.read_text()
        assert "uq_enr_org_active" in content
        assert "uq_runs_pipeline_rate_limit_key" in content
        assert "deleted_at IS NULL" in content
        assert "rate_limit_key IS NOT NULL" in content
