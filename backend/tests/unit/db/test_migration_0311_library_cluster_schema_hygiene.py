"""Structural tests for migration 0311_library_cluster_schema_hygiene (FAR-1758).

No database needed — the upgrade/downgrade assertions run against a mocked
``op``, the chain assertions against Alembic's script directory, and the
model-parity assertions against ORM metadata.

Lenses:

* **Chain** — 0311 revises ``0310_library_cluster_db_hardening``; the
  migrations directory has exactly one head
  (``0317_drop_retired_pipeline_diff_rollback_flag``, which chains onto
  ``0316_policy_gate_evaluation_mode``, which chains onto
  ``0315_service_accounts_lifecycle``, which chains onto
  ``0314_external_agents_mcp_servers``, which chains onto
  ``0313_collection_install_drop_installed_entities``, and is pinned by all sibling chain
  tests); the revision id matches the filename; neither ``branch_labels`` nor
  ``depends_on`` is set.
* **Upgrade** — drops the two redundant indexes, then adds the
  ``publishers.trust_tier`` CHECK and the ``library_primitives.version_group_id``
  self-FK (each ``NOT VALID`` then ``VALIDATE``-d, each gated on its OWN
  existence so a partially-applied run is repaired rather than skipped), then
  installs the idempotent same-org tenant trigger.
* **Downgrade** — exact reverse: trigger, FK, CHECK, then recreate both
  indexes.
* **No string-formatted DDL** — ``migration-fstring-sql`` blocks f-string
  ``op.execute`` in this directory (the rule's exclude window ends at 0247).
* **Model parity** — the ORM carries the SAME objects the migration writes, so
  ``create_all``'d schemas and ``alembic upgrade`` stay in lockstep and
  ``test_initial_migration.test_migrated_schema_matches_orm_metadata`` sees no
  drift: ``Publisher`` declares the CHECK and no longer indexes
  ``public_key_hex``; ``LibraryPrimitive.version_group_id`` declares the
  SET-NULL self-FK (index kept); ``CollectionInstallEntity`` still declares no
  redundant single-column index over the PK's leading column.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

from alembic.script import ScriptDirectory

from modulo.db.models.collection_install import CollectionInstallEntity
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.publisher import Publisher

_MIGRATION_REVISION = "0311_library_cluster_schema_hygiene"
_MIGRATION_DOWN_REVISION = "0310_library_cluster_db_hardening"
_HEAD_MIGRATION = "0320_evidence_redaction_rules"

_DROP_INSTALL_ENTITY_INDEX = "ix_collection_install_entity_install_id"
_DROP_PUBLISHER_INDEX = "ix_publishers_public_key_hex"
_TRUST_TIER_CHECK = "ck_publishers_trust_tier"
_VERSION_GROUP_FK = "library_primitives_version_group_id_fkey"
_TENANT_TRIGGER = "trg_library_primitives_version_group_id_tenant"

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


class _Recorder:
    """Captures ``op.execute`` SQL, plus the add/drop helpers for parity."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        self.sql.append(str(stmt))
        return MagicMock()


def _run(step: str, *, constraints_exist: bool = False) -> _Recorder:
    """Run ``upgrade()``/``downgrade()`` against a mocked ``op``.

    ``constraints_exist`` drives the inspector guard: ``False`` models a
    database that has NOT yet received the CHECK/FK (the normal upgrade path),
    ``True`` a database where they already landed (the re-run path).
    """
    module = _load_migration()
    rec = _Recorder()
    with (
        patch.object(module, "op", rec),
        patch.object(module, "_constraint_exists", return_value=constraints_exist),
    ):
        getattr(module, step)()
    return rec


def _joined(rec: _Recorder) -> str:
    return "\n".join(rec.sql)


class TestChain:
    def test_single_head_is_0317_drop_retired_pipeline_diff_rollback_flag(self) -> None:
        heads = ScriptDirectory(str(_VERSIONS.parent)).get_heads()
        assert heads == [_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_down_revision_is_0310_library_cluster_db_hardening(self) -> None:
        assert _load_migration().down_revision == _MIGRATION_DOWN_REVISION

    def test_revision_id_matches_filename(self) -> None:
        assert _load_migration().revision == _MIGRATION_REVISION

    def test_no_branch_labels_or_depends_on(self) -> None:
        module = _load_migration()
        assert module.branch_labels is None
        assert module.depends_on is None


class TestUpgrade:
    def test_drops_both_redundant_indexes(self) -> None:
        joined = _joined(_run("upgrade"))
        assert f"DROP INDEX IF EXISTS {_DROP_INSTALL_ENTITY_INDEX}" in joined
        assert f"DROP INDEX IF EXISTS {_DROP_PUBLISHER_INDEX}" in joined

    def test_does_not_drop_the_uq_publishers_org_key_lookup(self) -> None:
        """The org+key unique is what still serves get_publisher_by_key."""
        joined = _joined(_run("upgrade"))
        assert "uq_publishers_org_key" not in joined

    def test_adds_the_trust_tier_check_not_valid_then_validate(self) -> None:
        rec = _run("upgrade", constraints_exist=False)
        add = next(s for s in rec.sql if "ADD CONSTRAINT" in s and _TRUST_TIER_CHECK in s)
        assert "NOT VALID" in add, add
        assert "trust_tier IN ('green', 'amber')" in add, add
        assert f"ALTER TABLE publishers VALIDATE CONSTRAINT {_TRUST_TIER_CHECK}" in rec.sql, rec.sql

    def test_skips_the_trust_tier_check_when_already_present(self) -> None:
        rec = _run("upgrade", constraints_exist=True)
        joined = _joined(rec)
        assert _TRUST_TIER_CHECK not in joined, joined

    def test_adds_the_version_group_self_fk_not_valid_then_validate(self) -> None:
        rec = _run("upgrade", constraints_exist=False)
        add = next(s for s in rec.sql if "ADD CONSTRAINT" in s and _VERSION_GROUP_FK in s)
        assert "FOREIGN KEY (version_group_id) REFERENCES library_primitives(id)" in add, add
        assert "ON DELETE SET NULL" in add, add
        assert "NOT VALID" in add, add
        assert f"ALTER TABLE library_primitives VALIDATE CONSTRAINT {_VERSION_GROUP_FK}" in rec.sql, rec.sql

    def test_skips_the_self_fk_when_already_present(self) -> None:
        rec = _run("upgrade", constraints_exist=True)
        joined = _joined(rec)
        assert _VERSION_GROUP_FK not in joined, joined

    def test_installs_the_idempotent_same_org_tenant_trigger(self) -> None:
        rec = _run("upgrade")
        trigger = next(s for s in rec.sql if "pg_trigger" in s)
        assert "IF NOT EXISTS (SELECT 1 FROM pg_trigger" in trigger, trigger
        assert f"tgname='{_TENANT_TRIGGER}'" in trigger, trigger
        assert "BEFORE INSERT OR UPDATE OF version_group_id, organisation_id" in trigger, trigger
        assert "ON public.library_primitives" in trigger, trigger
        assert "enforce_same_organisation('library_primitives', 'version_group_id')" in trigger, trigger
        assert "DO $$" in trigger, trigger

    def test_trigger_is_installed_every_run(self) -> None:
        """The trigger has its own pg_trigger guard, so no inspector gate."""
        rec = _run("upgrade", constraints_exist=True)
        assert any("pg_trigger" in s for s in rec.sql), rec.sql


class TestDowngrade:
    def test_drops_trigger_then_fk_then_check(self) -> None:
        rec = _run("downgrade")
        assert rec.sql[0].startswith("DROP TRIGGER IF EXISTS"), rec.sql[0]
        assert _TENANT_TRIGGER in rec.sql[0]
        assert f"DROP CONSTRAINT IF EXISTS {_VERSION_GROUP_FK}" in rec.sql[1]
        assert f"DROP CONSTRAINT IF EXISTS {_TRUST_TIER_CHECK}" in rec.sql[2]

    def test_recreates_both_dropped_indexes(self) -> None:
        joined = _joined(_run("downgrade"))
        assert f"CREATE INDEX IF NOT EXISTS {_DROP_INSTALL_ENTITY_INDEX} ON collection_install_entity (install_id)" in (
            joined
        )
        assert f"CREATE INDEX IF NOT EXISTS {_DROP_PUBLISHER_INDEX} ON publishers (public_key_hex)" in joined

    def test_downgrade_emits_exactly_five_statements(self) -> None:
        """trigger, FK, CHECK, and the two index recreations — nothing extra
        and nothing missing (a sixth statement would be an unreviewed side
        effect on a rollback)."""
        rec = _run("downgrade")
        assert len(rec.sql) == 5, rec.sql


class TestMigrationSource:
    def test_no_string_formatted_ddl(self) -> None:
        """``migration-fstring-sql`` blocks f-string DDL in this directory."""
        assert "op.execute(f" not in _source()
        assert "text(f" not in _source()

    def test_never_edits_shipped_revisions(self) -> None:
        source = _source()
        assert "0109_schema_teams_library.py" not in source
        assert "0207_collection_install_tracking.py" not in source
        assert "0005_v2_features_system.py" not in source


class TestModelParity:
    # -- publishers.trust_tier CHECK -------------------------------------
    def test_publisher_declares_the_trust_tier_check(self) -> None:
        checks = {c.name: c for c in Publisher.__table__.constraints if getattr(c, "name", None)}
        assert _TRUST_TIER_CHECK in checks, f"Publisher must declare {_TRUST_TIER_CHECK}, got {sorted(checks)}"
        expression = str(checks[_TRUST_TIER_CHECK].sqltext)
        assert "trust_tier IN ('green', 'amber')" in expression, expression

    def test_publisher_check_matches_the_migration_text(self) -> None:
        """The model CHECK and the migration CHECK must carry the SAME clause —
        a drift here is exactly the model/migration divergence that
        ``tests/integration/test_initial_migration.py`` would surface."""
        model_sql = str(
            next(c for c in Publisher.__table__.constraints if getattr(c, "name", None) == _TRUST_TIER_CHECK).sqltext
        )
        migration_sql = next(s for s in _run("upgrade").sql if "ADD CONSTRAINT" in s and _TRUST_TIER_CHECK in s)
        assert "trust_tier IN ('green', 'amber')" in model_sql, model_sql
        assert "trust_tier IN ('green', 'amber')" in migration_sql, migration_sql

    # -- publishers.public_key_hex index drop ----------------------------
    def test_publisher_no_longer_indexes_public_key_hex(self) -> None:
        """``index=True`` must be gone: otherwise create_all rebuilds the
        index migration 0311 just dropped."""
        column = Publisher.__table__.c.public_key_hex
        assert not column.index, "public_key_hex must not carry a standalone index"
        names = {i.name for i in Publisher.__table__.indexes}
        assert _DROP_PUBLISHER_INDEX not in names, names

    def test_publisher_keeps_the_org_key_unique_that_serves_the_lookup(self) -> None:
        names = {i.name for i in Publisher.__table__.indexes}
        constraints = {c.name for c in Publisher.__table__.constraints if getattr(c, "name", None)}
        assert "uq_publishers_org_key" in names | constraints

    # -- library_primitives.version_group_id self-FK ---------------------
    def test_version_group_id_declares_the_set_null_self_fk(self) -> None:
        column = LibraryPrimitive.__table__.c.version_group_id
        fks = list(column.foreign_keys)
        assert len(fks) == 1, f"expected exactly one FK on version_group_id, got {len(fks)}"
        assert fks[0].target_fullname == "library_primitives.id", fks[0].target_fullname
        assert fks[0].ondelete == "SET NULL", fks[0].ondelete

    def test_version_group_id_keeps_its_lookup_index(self) -> None:
        """0310 created ``ix_library_primitives_version_group_id``; the FK
        addition must not drop it (the version listing depends on it)."""
        column = LibraryPrimitive.__table__.c.version_group_id
        assert column.index, "version_group_id must stay indexed"
        names = {i.name for i in LibraryPrimitive.__table__.indexes}
        assert "ix_library_primitives_version_group_id" in names, names

    # -- collection_install_entity redundant index ------------------------
    def test_install_entity_does_not_index_the_pk_leading_column(self) -> None:
        """The PK leads with ``install_id``, so an extra single-column index on
        it is duplication. The ORM must not declare one (0207 only ever created
        it in the DB, which 0311 drops)."""
        column = CollectionInstallEntity.__table__.c.install_id
        assert not column.index, "install_id must not carry a standalone index"
        names = {i.name for i in CollectionInstallEntity.__table__.indexes}
        assert _DROP_INSTALL_ENTITY_INDEX not in names, names

    def test_install_entity_pk_still_leads_with_install_id(self) -> None:
        pk = CollectionInstallEntity.__table__.primary_key
        assert [c.name for c in pk.columns] == ["install_id", "entity_type", "entity_id"]
