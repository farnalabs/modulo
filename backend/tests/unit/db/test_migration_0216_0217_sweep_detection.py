"""Structural tests for the FAR-748 migrations 0216/0217 (no database).

Pins the chain (revision -> down_revision) so the pre-commit
check-migration-heads hook can never be ambushed by a rebase, and asserts
model/migration parity for both shipped schema changes:

* 0216 — the sweep-detection composite index
  (ix_audit_events_org_type_actor_time) is created idempotently (the repo's
  0128/0154/0155/0214 convention), dropped on downgrade, and declared on the
  AuditEvent model so create_all'd schemas and autogenerate stay in sync.
* 0217 — ``decided_by`` is added to ``hitl_claims`` (nullable — legacy
  decided rows carry NULL and are never backfilled with a fictitious actor),
  FK'd to accounts with ondelete SET NULL (the audit chain's semantics),
  indexed for the durable sweep detection query, and declared on the
  HitlClaim model.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"


def _load_migration(revision: str) -> ModuleType:
    path = _VERSIONS / f"{revision}.py"
    assert path.exists(), f"Migration file missing: {path}"
    spec = importlib.util.spec_from_file_location(f"migration_{revision}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _code(module: ModuleType) -> str:
    return (_VERSIONS / f"{module.revision}.py").read_text(encoding="utf-8")


class TestMigration0216AuditEventsSweepDetectionIndex:
    def test_chain_pinned(self) -> None:
        module = _load_migration("0216_audit_events_sweep_detection_index")
        assert module.revision == "0216_audit_events_sweep_detection_index"
        assert module.down_revision == "0215_drop_runs_blob_columns"
        assert module.branch_labels is None
        assert module.depends_on is None

    def test_upgrade_creates_the_composite_index_idempotently(self) -> None:
        code = _code(_load_migration("0216_audit_events_sweep_detection_index"))
        assert "CREATE INDEX IF NOT EXISTS ix_audit_events_org_type_actor_time" in code
        # The detection-aggregate column order (org, event_type, actor, time).
        assert "organisation_id, event_type, account_id, created_at" in code

    def test_downgrade_drops_the_index(self) -> None:
        code = _code(_load_migration("0216_audit_events_sweep_detection_index"))
        assert "DROP INDEX IF EXISTS ix_audit_events_org_type_actor_time;" in code

    def test_model_declares_the_composite_index(self) -> None:
        import sqlalchemy as sa

        from modulo.db.models.audit_event import AuditEvent

        index = next((i for i in AuditEvent.__table__.indexes if i.name == "ix_audit_events_org_type_actor_time"), None)
        assert index is not None, "AuditEvent must declare the 0216 composite index"
        assert [col.name for col in index.columns] == [
            "organisation_id",
            "event_type",
            "account_id",
            "created_at",
        ]
        assert isinstance(AuditEvent.__table__.c.account_id.type, (sa.Uuid,))


class TestMigration0217HitlClaimsDecidedBy:
    def test_chain_pinned(self) -> None:
        module = _load_migration("0217_hitl_claims_decided_by")
        assert module.revision == "0217_hitl_claims_decided_by"
        assert module.down_revision == "0216_audit_events_sweep_detection_index"
        assert module.branch_labels is None
        assert module.depends_on is None

    def test_upgrade_adds_nullable_fk_column_and_index(self) -> None:
        code = _code(_load_migration("0217_hitl_claims_decided_by"))
        assert 'sa.Column("decided_by", sa.Uuid(), nullable=True)' in code
        assert 'ondelete="SET NULL"' in code
        assert "CREATE INDEX IF NOT EXISTS ix_hitl_claims_decided_by" in code

    def test_downgrade_reverses_exactly_the_upgrade(self) -> None:
        code = _code(_load_migration("0217_hitl_claims_decided_by"))
        assert "DROP INDEX IF EXISTS ix_hitl_claims_decided_by;" in code
        assert 'op.drop_constraint("fk_hitl_claims_decided_by_accounts"' in code
        assert 'op.drop_column("hitl_claims", "decided_by")' in code

    def test_model_declares_the_column(self) -> None:
        from modulo.db.models.hitl_claim import HitlClaim

        assert "decided_by" in HitlClaim.__table__.columns
        column = HitlClaim.__table__.c.decided_by
        assert column.nullable is True
        fks = list(column.foreign_keys)
        assert len(fks) == 1
        assert fks[0].ondelete == "SET NULL"
        assert any(idx.name == "ix_hitl_claims_decided_by" for idx in HitlClaim.__table__.indexes)
