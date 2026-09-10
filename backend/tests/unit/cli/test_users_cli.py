"""Unit tests for the ``modulo users`` CLI command group (FAR-680).

Exercises add / list / reset-password end-to-end against a hermetic
file-backed SQLite database (aiosqlite) — the same DB-direct style as
tests/unit/db/test_seed.py — through the real command resolution path
(Settings DATABASE_URL). Locks:

* creation reuses the promoted boot-seeder path (bcrypt hashes, membership),
* ``--admin`` grants the admin role in the primary organisation,
* duplicate-user refusal and unknown-user reset failure (exit code 1),
* MODULO_USERS guard: loud refusal without ``--force``, warning + proceed
  with it,
* reset-password signs out active sessions via the token-family helpers,
* passwords never appear in the command output.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

import pytest
from click.testing import CliRunner
from sqlalchemy import select

import modulo.cli.main as cli_main
import modulo.settings as settings_module
from modulo.settings import get_settings

PASSWORD = "Sup3r-Secret!Pass"
NEW_PASSWORD = "N3w-Secret!Pass"

T = TypeVar("T")


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'users.db'}"


@pytest.fixture(autouse=True)
def db_env(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point Settings' DATABASE_URL at the hermetic SQLite file for every test."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    get_settings.cache_clear()
    yield db_url
    settings_module._pinned_env_file = None
    settings_module._first_boot_guard = None
    get_settings.cache_clear()


async def _setup_tables_and_org(db_url: str) -> None:
    from modulo.db.models.base import Base
    from modulo.db.models.organisation import Organisation

    wanted = {"accounts", "organisations", "org_memberships", "token_families"}
    tables = [t for t in Base.metadata.sorted_tables if t.name in wanted]
    engine = create_engine_for(db_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
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
        assert "Created user ops@example.com (runner)" in result.output
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
        assert "Created user chief@example.com (admin)" in result.output

        account = run_db(lambda: _fetch_account(seeded_db, "chief@example.com"))
        membership = run_db(lambda: _org_membership(seeded_db, account.id, OrgMembership))
        assert membership.role == "admin"

    def test_add_prompts_for_password_when_omitted(self, seeded_db: str) -> None:
        prompt_input = f"{PASSWORD}\n{PASSWORD}\n"
        result = _invoke("add", "prompted@example.com", input=prompt_input)
        assert result.exit_code == 0, result.output
        assert "Created user prompted@example.com (runner)" in result.output
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


class TestUsersList:
    def test_list_shows_created_users(self, seeded_db: str) -> None:
        assert _invoke("add", "a@example.com", "--admin", "--password", PASSWORD).exit_code == 0
        assert _invoke("add", "b@example.com", "--password", PASSWORD).exit_code == 0

        result = _invoke("list")
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        header = lines[0]
        assert "email" in header
        assert "admin" in header
        assert "last_login" in header
        a_row = next(line for line in lines if line.startswith("a@example.com"))
        b_row = next(line for line in lines if line.startswith("b@example.com"))
        assert a_row.rstrip().endswith("never")
        assert " yes" in a_row
        assert " no" in b_row
        assert PASSWORD not in result.output

    def test_list_on_empty_org_reports_no_members(self, seeded_db: str) -> None:
        result = _invoke("list")
        assert result.exit_code == 0, result.output
        assert "no members" in result.output


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

    def test_reset_unknown_user_fails(self, seeded_db: str) -> None:
        result = _invoke("reset-password", "ghost@example.com", "--password", NEW_PASSWORD)
        assert result.exit_code == 1
        assert "no user with email ghost@example.com" in result.output


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
        assert PASSWORD not in result.output


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
