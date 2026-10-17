"""Unit tests for migration 0315_service_accounts_lifecycle (FAR-1478).

Structural + SQLite-model contract (no Postgres / Testcontainers needed —
the real trigger + DDL are exercised against real Postgres in
``tests/integration/db/test_migration_0315_service_accounts_lifecycle.py``):

* **Revision chain** — 0315 revises ``0314_external_agents_mcp_servers``
  (which revises ``0312_memberships_active_lookups``); the
  migrations directory has exactly one head (``0315_service_accounts_lifecycle``,
  pinned by all sibling chain tests), the revision id matches the filename, and
  neither ``branch_labels`` nor ``depends_on`` is set.
* **Source shape** — the upgrade adds ``accounts.decommissioned_at`` + the
  ``ck_accounts_decommissioned_service`` CHECK, creates the
  ``enforce_service_role_ceiling`` trigger function (plpgsql, SECURITY INVOKER,
  ERRCODE 23514) and the idempotent ``trg_org_memberships_service_role_ceiling``
  BEFORE INSERT OR UPDATE OF role trigger, plus the
  ``enforce_account_kind_immutable`` function + idempotent
  ``trg_accounts_kind_immutable`` BEFORE UPDATE OF kind trigger (accounts.kind
  is a CREATE-time discriminator — no kind transition, human->service or
  service->human, is ever legal); the downgrade drops all six in reverse.
* **Model parity** — ``Account`` declares the same column and CHECK name +
  expression, so ``create_all``'d schemas and migrated ones agree (the
  database.md rule: a migration must update the model in the SAME change).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic.script import ScriptDirectory

from modulo.db.models.account import Account

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0315_service_accounts_lifecycle"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"
_DOWN_REVISION = "0314_external_agents_mcp_servers"
_HEAD_MIGRATION = "0317_drop_retired_pipeline_diff_rollback_flag"

_CHECK_NAME = "ck_accounts_decommissioned_service"
_CHECK_PREDICATE = "decommissioned_at IS NULL OR kind = 'service'"
_TRIGGER_FUNCTION = "enforce_service_role_ceiling"
_TRIGGER_NAME = "trg_org_memberships_service_role_ceiling"
_KIND_IMMUTABLE_FUNCTION = "enforce_account_kind_immutable"
_KIND_IMMUTABLE_TRIGGER = "trg_accounts_kind_immutable"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    return _MIGRATION_PATH.read_text(encoding="utf-8")


class TestChain:
    def test_single_head_is_0317_drop_retired_pipeline_diff_rollback_flag(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0314_external_agents_mcp_servers(self) -> None:
        assert _load_migration().down_revision == _DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_NAME

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestSourceShape:
    def test_upgrade_adds_column_and_check(self) -> None:
        source = _source()
        assert 'sa.Column("decommissioned_at", sa.DateTime(timezone=True), nullable=True)' in source
        assert _CHECK_NAME in source
        assert _CHECK_PREDICATE in source

    def test_upgrade_creates_security_invoker_trigger_function(self) -> None:
        source = _source()
        assert "CREATE OR REPLACE FUNCTION public." in source
        assert _TRIGGER_FUNCTION in source
        assert "SECURITY DEFINER" not in source
        assert "service principals cannot hold the admin role" in source
        assert "ERRCODE = '23514'" in source
        assert "SELECT kind INTO target_kind FROM public.accounts" in source

    def test_upgrade_creates_idempotent_row_trigger(self) -> None:
        source = _source()
        assert "IF NOT EXISTS (SELECT 1 FROM pg_trigger" in source
        assert "CREATE TRIGGER" in source
        assert _TRIGGER_NAME in source
        assert "BEFORE INSERT OR UPDATE OF role ON public.org_memberships" in source
        assert "FOR EACH ROW EXECUTE FUNCTION public." in source
        assert "); END IF; END $$;" in source

    def test_upgrade_creates_kind_immutability_trigger(self) -> None:
        """accounts.kind is a CREATE-time discriminator — no kind flip is legal.

        The role-ceiling trigger reads the CURRENT accounts.kind, so without
        this trigger a raw-SQL human->service flip on an admin-membership
        holder would leave the admin membership intact (ADR 056/058's
        structurally-impossible state). The BEFORE UPDATE OF kind trigger
        refuses ANY kind transition with ERRCODE 23514.
        """
        source = _source()
        assert _KIND_IMMUTABLE_FUNCTION in source
        assert _KIND_IMMUTABLE_TRIGGER in source
        assert "BEFORE UPDATE OF kind ON public.accounts" in source
        assert "account kind is immutable" in source
        assert "ERRCODE = '23514'" in source
        assert "NEW.kind IS DISTINCT FROM OLD.kind" in source
        assert "SECURITY DEFINER" not in source

    def test_upgrade_installs_both_triggers(self) -> None:
        calls: list[str] = []

        class _OpRecorder:
            def execute(self, sql: str) -> None:
                calls.append(str(sql))

            def add_column(self, *args: object, **kwargs: object) -> None:
                calls.append("add_column")

            def create_check_constraint(self, *args: object, **kwargs: object) -> None:
                calls.append("create_check_constraint")

        import unittest.mock as mock

        module = _load_migration()
        with mock.patch.object(module, "op", _OpRecorder()):
            module.upgrade()
        joined = " | ".join(calls)
        assert _TRIGGER_NAME in joined
        assert _KIND_IMMUTABLE_TRIGGER in joined
        assert _KIND_IMMUTABLE_FUNCTION in joined

    def test_downgrade_drops_trigger_function_check_and_column(self) -> None:
        module = _load_migration()
        calls: list[str] = []

        class _OpRecorder:
            def execute(self, sql: str) -> None:
                calls.append(str(sql))

            def drop_constraint(self, name: str, table: str, **kwargs: object) -> None:
                calls.append(f"drop_constraint:{name}:{table}")

            def drop_column(self, table: str, name: str) -> None:
                calls.append(f"drop_column:{table}:{name}")

        import unittest.mock as mock

        with mock.patch.object(module, "op", _OpRecorder()):
            module.downgrade()

        joined = " | ".join(calls)
        assert _TRIGGER_NAME in joined
        assert _TRIGGER_FUNCTION in joined
        assert _KIND_IMMUTABLE_TRIGGER in joined
        assert _KIND_IMMUTABLE_FUNCTION in joined
        assert f"drop_constraint:{_CHECK_NAME}:accounts" in joined
        assert "drop_column:accounts:decommissioned_at" in joined


class TestModelParity:
    def test_model_declares_column(self) -> None:
        col = Account.__table__.c["decommissioned_at"]
        assert col.nullable is True

    def test_model_declares_matching_check(self) -> None:
        checks = {c.name: str(c.sqltext) for c in Account.__table__.constraints if hasattr(c, "sqltext")}
        assert _CHECK_NAME in checks
        assert "decommissioned_at IS NULL" in checks[_CHECK_NAME]
        assert "kind = 'service'" in checks[_CHECK_NAME]
