"""Unit tests for the ``modulo users`` CLI command group (FAR-680).

Exercises add / list / reset-password end-to-end against a hermetic
file-backed SQLite database (aiosqlite) — the same DB-direct style as
tests/unit/db/test_seed.py — through the real command resolution path
(Settings DATABASE_URL). Locks:

* creation reuses the promoted boot-seeder path (bcrypt hashes, membership),
* ``--admin`` grants the admin role in the primary organisation,
* the REPORTED role is the membership role actually stored (the seeder
  special-cases admin/admin@modulo.run to admin even without ``--admin``),
* duplicate-user refusal and unknown-user reset failure (exit code 1),
* passwords starting ``$2`` are refused before seeding (the seeder stores
  that prefix verbatim as a pre-computed bcrypt hash — an account that
  could never log in),
* break-glass accounts are refused for reset-password (admin-route
  parity — overwriting the hash breaks the break-glass CAS),
* MODULO_USERS guard: loud refusal without ``--force``, warning + proceed
  with it,
* reset-password signs out active sessions via the token-family helpers,
* launcher precedence: a state.json in the launcher data dir beats
  DATABASE_URL, and the tests NEVER touch a real launcher DB (the
  launcher default data dir is redirected to a bare tmp dir for every
  test — no state.json → the Settings path is used),
* ``users list`` is read-only: a launcher data dir missing secrets.json is
  a loud refusal with NO credential generation (no secrets.json written),
* passwords never appear in the command output.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import pytest
from click.testing import CliRunner
from sqlalchemy import select

import modulo.cli.main as cli_main
import modulo.cli.users as users_module
import modulo.launcher.entry as launcher_entry_module
import modulo.settings as settings_module
from modulo.launcher.config_source import LauncherConfigError
from modulo.launcher.state import STATE_FILENAME
from modulo.settings import get_settings

PASSWORD = "Sup3r-Secret!Pass"
NEW_PASSWORD = "N3w-Secret!Pass"

T = TypeVar("T")


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'users.db'}"


@pytest.fixture(autouse=True)
def isolated_launcher_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect the launcher default data dir to a bare tmp dir for every test.

    Without this the CLI resolves launcher-first: on any machine with
    ``~/.local/share/modulo/data/state.json`` the add/reset tests would
    create rows in the REAL bundled database. A bare dir has no state.json,
    so ``_resolve_database_url`` deterministically falls through to the
    Settings path.
    """
    root = tmp_path / "launcher-root"
    root.mkdir()
    monkeypatch.setattr(launcher_entry_module, "default_data_dir", lambda: root)
    return root


@pytest.fixture(autouse=True)
def db_env(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point Settings' DATABASE_URL at the hermetic SQLite file for every test."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    get_settings.cache_clear()
    yield db_url
    settings_module._pinned_env_file = None
    settings_module._first_boot_guard = None
    get_settings.cache_clear()


def _hermetic_tables() -> list[Any]:
    """The four needed tables as HERMETIC COPIES with integer boolean defaults.

    sqlite renders the Account model's string server_defaults literally
    (``DEFAULT 'false'`` → stored TEXT), and SQLAlchemy's str-to-boolean
    result processing reads TEXT 'false' back as Python True — which would
    make every CLI-created account look break-glass. Integer defaults
    (0/1) round-trip through sqlite correctly, keeping the harness faithful
    to Postgres-native booleans. The real metadata is never mutated.
    """
    import sqlalchemy as sa

    from modulo.db.models.base import Base

    wanted = {"accounts", "organisations", "org_memberships", "token_families"}
    hermetic = sa.MetaData()
    tables = []
    for table in Base.metadata.sorted_tables:
        if table.name not in wanted:
            continue
        copy = table.to_metadata(hermetic)
        if table.name == "accounts":
            for column_name, integer_default in (
                ("active", "1"),
                ("must_change_password", "0"),
                ("is_system_admin", "0"),
                ("is_break_glass", "0"),
            ):
                copy.c[column_name].server_default = sa.DefaultClause(sa.text(integer_default))
        tables.append(copy)
    return tables


async def _setup_tables_and_org(db_url: str) -> None:
    import sqlalchemy as sa

    from modulo.db.models.organisation import Organisation

    tables = _hermetic_tables()
    engine = create_engine_for(db_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: sa.MetaData().create_all(sync_conn, tables=tables))
            await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
        maker = async_session_maker(engine)
        async with maker() as session, session.begin():
            session.add(Organisation(name="Primary", slug="primary"))
    finally:
        await engine.dispose()


def create_engine_for(db_url: str):
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(db_url)


def async_session_maker(engine: Any):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(engine, expire_on_commit=False)


def run_db[T](coroutine_factory: Callable[[], Awaitable[T]]) -> T:
    """Run a DB coroutine to completion with a private event loop."""
    test_loop = asyncio.new_event_loop()
    try:
        outcome = test_loop.run_until_complete(coroutine_factory())
    finally:
        test_loop.close()
    return outcome


@pytest.fixture
def seeded_db(db_url: str) -> str:
    """Create the four needed tables and the primary organisation row."""
    run_db(lambda: _setup_tables_and_org(db_url))
    return db_url


async def _fetch_account(db_url: str, email: str) -> Any:
    from modulo.db.models.account import Account

    engine = create_engine_for(db_url)
    try:
        maker = async_session_maker(engine)
        async with maker() as session:
            return (await session.execute(select(Account).where(Account.email == email))).scalar_one()
    finally:
        await engine.dispose()


def _invoke(*args: str, input: str | None = None):
    return CliRunner().invoke(cli_main.cli, ["users", *args], input=input)


class TestUsersAdd:
    def test_add_creates_user_through_boot_seeder_path(self, seeded_db: str) -> None:
        from modulo.auth.passwords import verify_password

        result = _invoke("add", "ops@example.com", "--password", PASSWORD)
        assert result.exit_code == 0, result.output
        assert "Created user ops@example.com (runner in organisation 'Primary')" in result.output
        assert PASSWORD not in result.output

        def read() -> Any:
            async def go() -> Any:
                from modulo.db.models.account import Account
                from modulo.db.models.org_membership import OrgMembership

                engine = create_engine_for(seeded_db)
                try:
                    maker = async_session_maker(engine)
                    async with maker() as session:
                        account = (
                            await session.execute(select(Account).where(Account.email == "ops@example.com"))
                        ).scalar_one()
                        membership = (
                            await session.execute(select(OrgMembership).where(OrgMembership.account_id == account.id))
                        ).scalar_one()
                        return membership.role, verify_password(PASSWORD, account.password_hash)
                finally:
                    await engine.dispose()

            return go()

        role, verified = run_db(read)
        assert role == "runner"
        assert verified is True

    def test_add_admin_grants_admin_role(self, seeded_db: str) -> None:
        from modulo.db.models.org_membership import OrgMembership

        result = _invoke("add", "chief@example.com", "--admin", "--password", PASSWORD)
        assert result.exit_code == 0, result.output
        assert "Created user chief@example.com (admin in organisation 'Primary')" in result.output

        account = run_db(lambda: _fetch_account(seeded_db, "chief@example.com"))
        membership = run_db(lambda: _org_membership(seeded_db, account.id, OrgMembership))
        assert membership.role == "admin"

    def test_add_prompts_for_password_when_omitted(self, seeded_db: str) -> None:
        prompt_input = f"{PASSWORD}\n{PASSWORD}\n"
        result = _invoke("add", "prompted@example.com", input=prompt_input)
        assert result.exit_code == 0, result.output
        assert "Created user prompted@example.com (runner in organisation 'Primary')" in result.output
        assert PASSWORD not in result.output
        assert PASSWORD not in result.output

    def test_add_refuses_existing_user(self, seeded_db: str) -> None:
        first = _invoke("add", "dup@example.com", "--password", PASSWORD)
        assert first.exit_code == 0, first.output
        second = _invoke("add", "dup@example.com", "--password", PASSWORD)
        assert second.exit_code == 1
        assert "already exists" in second.output

    def test_add_rejects_weak_password(self, seeded_db: str) -> None:
        result = _invoke("add", "weak@example.com", "--password", "12345678")
        assert result.exit_code == 1
        assert "too weak" in result.output.lower()

    def test_add_refuses_bcrypt_hash_password(self, seeded_db: str) -> None:
        bcrypt_shaped = "$2b$12$" + "a" * 51
        result = _invoke("add", "hashy@example.com", "--password", bcrypt_shaped)
        assert result.exit_code == 1
        assert "$2" in result.output
        assert "never log in" in result.output
        # nothing was seeded
        assert _account_exists(seeded_db, "hashy@example.com") is False

    def test_add_reports_the_actually_stored_role_for_seeder_admin_special_case(self, seeded_db: str) -> None:
        # The seeder special-cases admin/admin@modulo.run to admin WITHOUT
        # --admin — the printed role must come from the stored membership.
        result = _invoke("add", "admin@modulo.run", "--password", PASSWORD)
        assert result.exit_code == 0, result.output
        assert "Created user admin@modulo.run (admin in organisation 'Primary')" in result.output
        assert _account_role(seeded_db, "admin@modulo.run") == "admin"


class TestUsersList:
    def test_list_shows_created_users_with_org(self, seeded_db: str) -> None:
        assert _invoke("add", "a@example.com", "--admin", "--password", PASSWORD).exit_code == 0
        assert _invoke("add", "b@example.com", "--password", PASSWORD).exit_code == 0

        result = _invoke("list")
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        header = lines[0]
        assert "email" in header
        assert "admin" in header
        assert "last_login" in header
        assert "org" in header
        a_row = next(line for line in lines if line.startswith("a@example.com"))
        b_row = next(line for line in lines if line.startswith("b@example.com"))
        assert " never" in a_row
        assert " yes" in a_row
        assert a_row.rstrip().endswith("Primary")
        assert " no" in b_row
        assert b_row.rstrip().endswith("Primary")
        assert PASSWORD not in result.output

    def test_list_annotates_org_less_rows(self, seeded_db: str) -> None:
        from modulo.auth.passwords import hash_password
        from modulo.db.models.account import Account

        assert _invoke("add", "member@example.com", "--password", PASSWORD).exit_code == 0

        def make_org_less_account() -> None:
            async def go() -> None:
                engine = create_engine_for(seeded_db)
                try:
                    maker = async_session_maker(engine)
                    async with maker() as session, session.begin():
                        session.add(
                            Account(
                                email="orphan@example.com",
                                display_name="orphan",
                                password_hash=hash_password(PASSWORD),
                                auth_provider="local",
                            )
                        )
                finally:
                    await engine.dispose()

            return go()

        run_db(make_org_less_account)
        result = _invoke("list")
        assert result.exit_code == 0, result.output
        orphan_row = next(line for line in result.output.splitlines() if line.startswith("orphan@example.com"))
        assert "no membership in Primary" in orphan_row

    def test_list_on_empty_org_reports_no_members(self, seeded_db: str) -> None:
        result = _invoke("list")
        assert result.exit_code == 0, result.output
        assert "no members" in result.output
        assert "Primary" in result.output


class TestListReadOnly:
    def test_launcher_inputs_readonly_reads_without_creating(self, tmp_path: Path) -> None:
        from modulo.launcher.secrets_file import LauncherSecrets
        from modulo.launcher.state import LauncherState, save_state

        data_dir = tmp_path / "launcher"
        data_dir.mkdir()
        secrets = LauncherSecrets(postgres_password="pg-pw", redis_password="redis-pw", state_hmac_key=bytes(range(32)))
        # Hand-write the secrets file — load_or_create would refuse Windows
        # and is exactly the generating side effect this loader must avoid.
        payload = {
            "postgres_password": secrets.postgres_password,
            "redis_password": secrets.redis_password,
            "state_hmac_key": secrets.state_hmac_key_hex,
        }
        (data_dir / "secrets.json").write_text(json.dumps(payload), encoding="utf-8")
        save_state(
            LauncherState(postgres_port=15432, redis_port=16379, api_port=18000),
            data_dir / STATE_FILENAME,
            secrets.state_hmac_key,
        )

        state, loaded = users_module._launcher_inputs_readonly(data_dir)
        assert loaded.postgres_password == "pg-pw"
        assert loaded.redis_password == "redis-pw"
        assert state.postgres_port == 15432

    def test_launcher_inputs_readonly_refuses_missing_files_and_creates_nothing(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(LauncherConfigError, match=r"no state\.json"):
            users_module._launcher_inputs_readonly(empty)
        assert not (empty / "secrets.json").exists()

        state_only = tmp_path / "state-only"
        state_only.mkdir()
        (state_only / STATE_FILENAME).write_text("{}", encoding="utf-8")
        with pytest.raises(LauncherConfigError, match=r"no secrets\.json"):
            users_module._launcher_inputs_readonly(state_only)
        assert not (state_only / "secrets.json").exists()

    def test_list_cli_refuses_secrets_free_launcher_dir_without_generating(self, isolated_launcher_root: Path) -> None:
        # state.json present + secrets.json absent: the CLI must refuse
        # loudly instead of GENERATING credentials (and must write nothing).
        data_dir = isolated_launcher_root / "partial"
        data_dir.mkdir()
        (data_dir / STATE_FILENAME).write_text("{}", encoding="utf-8")
        result = _invoke("list", "--data-dir", str(data_dir))
        assert result.exit_code == 1
        assert "secrets.json" in result.output
        assert not (data_dir / "secrets.json").exists()


class TestUsersResetPassword:
    def test_reset_changes_hash_and_signs_out_sessions(self, seeded_db: str) -> None:
        from modulo.auth.passwords import verify_password
        from modulo.db.crud.token_family import create_family
        from modulo.db.models.organisation import Organisation

        assert _invoke("add", "rotates@example.com", "--password", PASSWORD).exit_code == 0
        account = run_db(lambda: _fetch_account(seeded_db, "rotates@example.com"))

        def make_family() -> Any:
            async def go() -> Any:
                engine = create_engine_for(seeded_db)
                try:
                    maker = async_session_maker(engine)
                    async with maker() as session, session.begin():
                        org = (await session.execute(select(Organisation))).scalar_one()
                        return await create_family(session, account.id, org.id)
                finally:
                    await engine.dispose()

            return go()

        run_db(make_family)

        result = _invoke("reset-password", "rotates@example.com", "--password", NEW_PASSWORD)
        assert result.exit_code == 0, result.output
        assert "1 active session" in result.output
        assert PASSWORD not in result.output
        assert NEW_PASSWORD not in result.output

        reloaded = run_db(lambda: _fetch_account(seeded_db, "rotates@example.com"))
        assert verify_password(NEW_PASSWORD, reloaded.password_hash)
        assert not verify_password(PASSWORD, reloaded.password_hash)

    def test_reset_refuses_break_glass_accounts(self, seeded_db: str) -> None:
        from modulo.auth.passwords import hash_password, verify_password
        from modulo.db.models.account import Account

        def make_break_glass() -> None:
            async def go() -> None:
                engine = create_engine_for(seeded_db)
                try:
                    maker = async_session_maker(engine)
                    async with maker() as session, session.begin():
                        session.add(
                            Account(
                                email="escape@example.com",
                                display_name="escape",
                                password_hash=hash_password(PASSWORD),
                                auth_provider="local",
                                is_break_glass=True,
                                break_glass_expires_at=datetime.now(UTC) + timedelta(days=1),
                            )
                        )
                finally:
                    await engine.dispose()

            return go()

        run_db(make_break_glass)
        result = _invoke("reset-password", "escape@example.com", "--password", NEW_PASSWORD)
        assert result.exit_code == 1
        assert "break-glass" in result.output
        assert "orphan" in result.output
        # the hash is untouched
        account = run_db(lambda: _fetch_account(seeded_db, "escape@example.com"))
        assert verify_password(PASSWORD, account.password_hash)

    def test_reset_unknown_user_fails(self, seeded_db: str) -> None:
        result = _invoke("reset-password", "ghost@example.com", "--password", NEW_PASSWORD)
        assert result.exit_code == 1
        assert "no user with email ghost@example.com" in result.output

    def test_reset_password_help_states_the_residual_access_token_window(self) -> None:
        result = _invoke("reset-password", "--help")
        assert result.exit_code == 0
        # Help output is line-wrapped — normalize whitespace before matching.
        normalized = " ".join(result.output.split())
        assert "until it expires" in normalized


class TestModuloUsersGuard:
    def test_add_refuses_without_force_when_modulo_users_set(
        self, seeded_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODULO_USERS", "admin@example.com:seeded-password")
        get_settings.cache_clear()
        result = _invoke("add", "admin@example.com", "--password", PASSWORD)
        assert result.exit_code == 1
        assert "MODULO_USERS" in result.output
        assert "--force" in result.output

    def test_reset_refuses_without_force_when_modulo_users_set(
        self, seeded_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODULO_USERS", "admin@example.com:seeded-password")
        get_settings.cache_clear()
        result = _invoke("reset-password", "ghost@example.com", "--password", NEW_PASSWORD)
        assert result.exit_code == 1
        assert "MODULO_USERS" in result.output

    def test_force_proceeds_with_loud_warning(self, seeded_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_USERS", "other@example.com:seeded-password")
        get_settings.cache_clear()
        result = _invoke("add", "forced@example.com", "--password", PASSWORD, "--force")
        assert result.exit_code == 0, result.output
        assert "MODULO_USERS is set" in result.output
        assert "Created user forced@example.com" in result.output
        assert "NOT reverted" in result.output
        assert PASSWORD not in result.output


class TestLauncherDbPrecedence:
    def test_default_data_dir_is_isolated_in_tests(self, isolated_launcher_root: Path) -> None:
        # The isolation redirect leaves the launcher root WITHOUT state.json,
        # so every default-path invocation takes the Settings branch.
        assert (isolated_launcher_root / STATE_FILENAME).exists() is False

    def test_state_json_beats_database_url(
        self, seeded_db: str, isolated_launcher_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launcher_url = f"sqlite+aiosqlite:///{isolated_launcher_root / 'launcher.db'}"
        run_db(lambda: _setup_tables_and_org(launcher_url))
        _make_state_json(isolated_launcher_root)
        monkeypatch.setattr(users_module, "_launcher_url_for", lambda _data_dir, **_kwargs: launcher_url)

        result = _invoke("add", "precedence@example.com", "--password", PASSWORD)
        assert result.exit_code == 0, result.output
        assert "Created user precedence@example.com" in result.output

        # The write landed on the LAUNCHER-resolved DB...
        assert _account_exists(launcher_url, "precedence@example.com") is True
        # ...and NOT on the Settings (DATABASE_URL) one.
        assert _account_exists(seeded_db, "precedence@example.com") is False


class TestRegistration:
    def test_users_is_registered_on_the_top_level_group(self) -> None:
        assert "users" in cli_main.cli.commands

    def test_users_help_survives(self) -> None:
        result = _invoke("--help")
        assert result.exit_code == 0
        for command in ("add", "list", "reset-password"):
            assert command in result.output


async def _org_membership(db_url: str, account_id: uuid.UUID, model: Any) -> Any:
    engine = create_engine_for(db_url)
    try:
        maker = async_session_maker(engine)
        async with maker() as session:
            return (await session.execute(select(model).where(model.account_id == account_id))).scalar_one()
    finally:
        await engine.dispose()


def _make_state_json(launcher_root: Path) -> None:
    """Plant a state.json marker (existence-only — the real composition is patched)."""
    (launcher_root / STATE_FILENAME).write_text("{}", encoding="utf-8")


async def _account_exists_or_not(db_url: str, email: str) -> bool:
    from modulo.db.models.account import Account

    engine = create_engine_for(db_url)
    try:
        maker = async_session_maker(engine)
        async with maker() as session:
            existing = (await session.execute(select(Account).where(Account.email == email))).scalar_one_or_none()
            return existing is not None
    finally:
        await engine.dispose()


def _account_exists(db_url: str, email: str) -> bool:
    return run_db(lambda: _account_exists_or_not(db_url, email))


def _account_role(db_url: str, email: str) -> str:
    from modulo.db.models.account import Account
    from modulo.db.models.org_membership import OrgMembership

    async def go() -> str:
        engine = create_engine_for(db_url)
        try:
            maker = async_session_maker(engine)
            async with maker() as session:
                account = (await session.execute(select(Account).where(Account.email == email))).scalar_one()
                membership = (
                    await session.execute(select(OrgMembership).where(OrgMembership.account_id == account.id))
                ).scalar_one()
                return str(membership.role)
        finally:
            await engine.dispose()

    return run_db(go)
