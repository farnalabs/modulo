"""FAR-2281: migration 0346 — scope-aware ``ck_org_api_keys_role`` round-trip.

Executes the migration against an in-memory SQLite engine (the 0305 portable
CHECK template). Proves:

* **Widening** — ``admin`` is accepted only when ``scope='user'``; an
  org-scoped admin row is still rejected.
* **Existing vocabulary preserved** — operator/runner remain accepted for both
  scopes (a strict widening, so every pre-existing row is valid).
* **Downgrade narrows loudly** — a table holding an admin user-scoped row
  refuses the narrowing rather than silently stranding the role.
* **Model parity** — the ORM CHECK carries the same predicate as the migration.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"

_REVISION = "0346_org_api_keys_role_scope"


def _load_migration() -> ModuleType:
    path = _VERSIONS / f"{_REVISION}.py"
    assert path.exists(), f"Migration file missing: {path}"
    spec = importlib.util.spec_from_file_location(f"migration_{_REVISION}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scaffold(conn: sa.Connection) -> None:
    """The pre-0346 ``org_api_keys`` shape (flat operator/runner CHECK)."""
    conn.execute(
        sa.text(
            "CREATE TABLE org_api_keys ("
            "id INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL, "
            "role TEXT NOT NULL, "
            "scope TEXT NOT NULL DEFAULT 'org', "
            "account_id INTEGER NOT NULL, "
            "CONSTRAINT ck_org_api_keys_role CHECK (role IN ('operator', 'runner')), "
            "CONSTRAINT ck_org_api_keys_scope CHECK (scope IN ('org', 'user'))"
            ")"
        )
    )


def _run(engine: sa.Engine, module: ModuleType, fn: str) -> None:
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            getattr(module, fn)()


def _insert_key(conn: sa.Connection, *, role: str, scope: str, key_id: int = 1) -> None:
    conn.execute(
        sa.text("INSERT INTO org_api_keys (id, name, role, scope, account_id) VALUES (:id, 'k', :role, :scope, 2)"),
        {"id": key_id, "role": role, "scope": scope},
    )


@pytest.fixture
def sqlite_engine() -> Iterator[sa.Engine]:
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool)
    yield engine
    engine.dispose()


class TestWidenedRoleScopeCheck:
    def test_admin_user_key_is_accepted(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        with sqlite_engine.begin() as conn:
            _insert_key(conn, role="admin", scope="user")
        with sqlite_engine.connect() as conn:
            assert conn.execute(sa.text("SELECT role FROM org_api_keys")).scalar_one() == "admin"

    def test_admin_org_key_is_still_rejected(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        with pytest.raises(sa.exc.IntegrityError), sqlite_engine.begin() as conn:
            _insert_key(conn, role="admin", scope="org")

    @pytest.mark.parametrize("role", ["operator", "runner"])
    @pytest.mark.parametrize("scope", ["org", "user"])
    def test_operator_and_runner_stay_valid_for_both_scopes(
        self, sqlite_engine: sa.Engine, role: str, scope: str
    ) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        with sqlite_engine.begin() as conn:
            _insert_key(conn, role=role, scope=scope)
        with sqlite_engine.connect() as conn:
            assert conn.execute(sa.text("SELECT COUNT(*) FROM org_api_keys")).scalar_one() == 1

    def test_unknown_role_is_rejected(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        _run(sqlite_engine, _load_migration(), "upgrade")
        with pytest.raises(sa.exc.IntegrityError), sqlite_engine.begin() as conn:
            _insert_key(conn, role="viewer", scope="user")


class TestDowngradeNarrows:
    def test_downgrade_restores_flat_prohibition(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        migration = _load_migration()
        _run(sqlite_engine, migration, "upgrade")
        with sqlite_engine.begin() as conn:
            _insert_key(conn, role="operator", scope="user")
        _run(sqlite_engine, migration, "downgrade")
        # The operator row survives; an admin row would now be rejected.
        with pytest.raises(sa.exc.IntegrityError), sqlite_engine.begin() as conn:
            _insert_key(conn, role="admin", scope="user", key_id=2)

    def test_downgrade_fails_loudly_on_an_existing_admin_row(self, sqlite_engine: sa.Engine) -> None:
        with sqlite_engine.begin() as conn:
            _scaffold(conn)
        migration = _load_migration()
        _run(sqlite_engine, migration, "upgrade")
        with sqlite_engine.begin() as conn:
            _insert_key(conn, role="admin", scope="user")
        with pytest.raises(sa.exc.IntegrityError):
            _run(sqlite_engine, migration, "downgrade")


class TestModelParity:
    def test_model_check_matches_the_migration_predicate(self) -> None:
        from modulo.db.models.api_key import OrgApiKey

        checks = {c.name: str(c.sqltext) for c in OrgApiKey.__table__.constraints if hasattr(c, "sqltext")}
        role_check = " ".join(checks["ck_org_api_keys_role"].split())
        assert role_check == "role IN ('operator', 'runner') OR (role = 'admin' AND scope = 'user')"

    def test_migration_predicate_matches_the_model(self) -> None:
        module = _load_migration()
        assert module._ROLE_SCOPE_WIDENED == ("role IN ('operator', 'runner') OR (role = 'admin' AND scope = 'user')")
