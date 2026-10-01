"""FAR-1257: migration 0265 - the review-window columns.

Adds ``hitl_claims.terminalize_at`` (the absolute deadline stamped at fire
time) and ``pipelines.hitl_review_window_seconds`` (the per-pipeline override).

Lenses:

* **Chain** - 0265 chains onto ``0264_pipelines_max_autonomy_ge_default``;
  0266_guardrail_policy_gate_sweep (FAR-1107) chains onto 0265,
  0267_notification_hot_query_indexes chains onto 0266,
  0268_webhook_lookup_expiry_indexes chains onto 0267,
  0269_webhook_dedup_check_constraints chains onto 0268,
  0270_pipeline_snapshots_max_autonomy_ge_default chains onto 0269,
  0271_org_api_keys_revocation_sweep_indexes chains onto 0270,
  0272_oauth_client_revoke_lookup_indexes chains onto 0271, and
  0273_runs_dispatch_phase chains onto 0272 as the
  current single linear head.
* **Structure (mocked ``op``)** - upgrade adds BOTH columns existence-gated,
  adds the ``ck_pipelines_hitl_review_window`` CHECK (NOT VALID then VALIDATE on
  Postgres; batch mode on SQLite) and creates the partial sweep index;
  downgrade reverses all three.
* **Model parity** - ``Pipeline.__table_args__`` declares the same CHECK name
  and the same 60..604800 envelope, so ``test_initial_migration``'s
  ORM-vs-migrated-DB parity check cannot diverge. ``HitlClaim`` declares
  ``terminalize_at`` as a nullable timezone-aware DateTime.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, DateTime

from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.pipeline import Pipeline

_MIGRATION_REVISION = "0265_hitl_review_window"
_MIGRATION_DOWN_REVISION = "0264_pipelines_max_autonomy_ge_default"
_HEAD_MIGRATION = "0273_runs_dispatch_phase"
_CHECK_CONSTRAINT = "ck_pipelines_hitl_review_window"
_SWEEP_INDEX = "ix_hitl_claims_terminalize_sweep"
_ENVELOPE = (60, 604800)

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_REVISION}.py"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_REVISION}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    return _MIGRATION_PATH.read_text(encoding="utf-8")


def _run(what: str, dialect: str) -> MagicMock:
    """Run *what* with a mocked ``op`` whose bind reports *dialect*."""
    module = _load_migration()
    bind = MagicMock()
    bind.dialect.name = dialect
    op = MagicMock()
    op.get_bind.return_value = bind
    # batch_alter_table must behave as a context manager yielding a recorder.
    batch = MagicMock()
    op.batch_alter_table.return_value.__enter__.return_value = batch
    op.batch_alter_table.return_value.__exit__.return_value = False
    with patch.object(module, "op", op):
        getattr(module, what)()
    op.batch = batch  # type: ignore[attr-defined]
    return op


def _executed_sql(op: MagicMock) -> list[str]:
    return [call.args[0] for call in op.execute.call_args_list]


class TestChain:
    def test_single_head_is_0263(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0262(self) -> None:
        assert _load_migration().down_revision == _MIGRATION_DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_REVISION


class TestUpgradePostgres:
    def test_adds_both_columns_existence_gated(self) -> None:
        joined = "\n".join(_executed_sql(_run("upgrade", "postgresql")))
        assert "ALTER TABLE hitl_claims ADD COLUMN IF NOT EXISTS terminalize_at" in joined
        assert '"hitl_review_window_seconds" integer' in joined
        assert "timestamp with time zone" in joined

    def test_check_is_added_not_valid_then_validated(self) -> None:
        executed = _executed_sql(_run("upgrade", "postgresql"))
        add = next(ddl for ddl in executed if "ADD CONSTRAINT" in ddl)
        validate = next(ddl for ddl in executed if "VALIDATE CONSTRAINT" in ddl)
        assert f"conname='{_CHECK_CONSTRAINT}'" in add
        assert "conrelid = 'public.pipelines'::regclass" in add
        assert "NOT VALID;" in add
        assert f"conname='{_CHECK_CONSTRAINT}'" in validate
        assert "NOT convalidated" in validate

    def test_check_envelopes_null_or_60_to_604800(self) -> None:
        joined = "\n".join(_executed_sql(_run("upgrade", "postgresql")))
        assert "hitl_review_window_seconds IS NULL OR" in joined
        assert f"hitl_review_window_seconds BETWEEN {_ENVELOPE[0]} AND {_ENVELOPE[1]}" in joined

    def test_sweep_index_is_partial_over_open_unclaimed_claims(self) -> None:
        op = _run("upgrade", "postgresql")
        assert op.create_index.call_count == 1
        args, kwargs = op.create_index.call_args
        assert args[0] == _SWEEP_INDEX
        assert args[1] == "hitl_claims"
        assert args[2] == ["terminalize_at"]
        assert kwargs["if_not_exists"] is True
        where = str(kwargs["postgresql_where"])
        assert "decision IS NULL" in where
        assert "account_id IS NULL" in where


class TestUpgradeSqlite:
    def test_columns_go_through_batch_alter_table(self) -> None:
        op = _run("upgrade", "sqlite")
        names = [getattr(call.args[0], "name", None) for call in op.batch.add_column.call_args_list]
        assert "terminalize_at" in names
        assert "hitl_review_window_seconds" in names

    def test_check_created_in_batch_mode(self) -> None:
        op = _run("upgrade", "sqlite")
        assert op.batch.create_check_constraint.call_count == 1
        args, _kwargs = op.batch.create_check_constraint.call_args
        assert args[0] == _CHECK_CONSTRAINT
        assert "IS NULL OR" in args[1]
        assert f"BETWEEN {_ENVELOPE[0]} AND {_ENVELOPE[1]}" in args[1]

    def test_index_created_on_sqlite_too(self) -> None:
        op = _run("upgrade", "sqlite")
        assert op.create_index.call_count == 1
        assert op.create_index.call_args.args[0] == _SWEEP_INDEX


class TestDowngrade:
    def test_postgres_downgrade_reverses_everything(self) -> None:
        op = _run("downgrade", "postgresql")
        joined = "\n".join(_executed_sql(op))
        assert f"DROP CONSTRAINT {_CHECK_CONSTRAINT}" in joined
        assert 'DROP COLUMN IF EXISTS "hitl_review_window_seconds"' in joined
        assert "DROP COLUMN IF EXISTS terminalize_at" in joined
        assert op.drop_index.call_count == 1
        assert op.drop_index.call_args.kwargs.get("if_exists") is True

    def test_sqlite_downgrade_drops_through_batch_mode(self) -> None:
        op = _run("downgrade", "sqlite")
        assert op.drop_index.call_count == 1
        dropped = [call.args[0] for call in op.batch.drop_constraint.call_args_list]
        assert _CHECK_CONSTRAINT in dropped
        cols = [call.args[0] for call in op.batch.drop_column.call_args_list]
        assert "hitl_review_window_seconds" in cols
        assert "terminalize_at" in cols


class TestModelParity:
    def _model_check(self) -> CheckConstraint:
        checks = [c for c in Pipeline.__table_args__ if isinstance(c, CheckConstraint)]
        match = next((c for c in checks if c.name == _CHECK_CONSTRAINT), None)
        assert match is not None, f"Pipeline.__table_args__ missing {_CHECK_CONSTRAINT}"
        return match

    def test_migration_and_model_declare_the_same_check_name(self) -> None:
        assert _CHECK_CONSTRAINT in _source()
        assert self._model_check().name == _CHECK_CONSTRAINT

    def test_pipeline_check_carries_the_null_arm_and_envelope(self) -> None:
        sqltext = str(self._model_check().sqltext)
        assert "hitl_review_window_seconds IS NULL OR" in sqltext
        assert f"BETWEEN {_ENVELOPE[0]} AND {_ENVELOPE[1]}" in sqltext

    def test_hitl_claim_declares_terminalize_at(self) -> None:
        column = HitlClaim.__table__.columns["terminalize_at"]
        assert column.nullable is True
        assert isinstance(column.type, DateTime)
        assert column.type.timezone is True

    def test_pipeline_column_is_nullable(self) -> None:
        # NULL = "no override", the middle layer of the chain — NOT NULL would
        # make every pre-0263 row undeployable.
        assert Pipeline.__table__.columns["hitl_review_window_seconds"].nullable is True

    def test_envelope_is_the_shipped_one(self) -> None:
        assert _ENVELOPE == (60, 604800)
