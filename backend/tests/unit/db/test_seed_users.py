"""Unit tests for the promoted MODULO_USERS seeder (FAR-671).

``modulo.db.seed.seed_modulo_users`` / ``seed_modulo_user`` /
``rehash_existing_user`` are the single implementation (promoted verbatim from
``modulo.api.main``); the API keeps thin aliases as its boot-path/test seam.
These tests exercise the promoted functions directly through the
``modulo.api.dependencies`` seams — the same seam shape the pre-promotion
tests patched.
"""

import os
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# Minimal env so importing ``modulo.api.main`` (which builds the lazy engine
# from Settings at import time) works outside the tests/unit/api conftest.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)

import pytest

import modulo.api.main as main_module
from modulo.db.seed import rehash_existing_user, seed_modulo_user, seed_modulo_users
from modulo.settings import Settings

_VALID_32 = "a" * 32
_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def _make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": _VALID_32,
        "fernet_key": _FERNET_KEY,
        "fernet_key_old": "",
        "modulo_admin_password": "testpass",
        "redis_url": "redis://localhost:6379/0",
        "modulo_public_url": "http://localhost:8000",
        "watchdog_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _result(
    *,
    scalar_one_or_none: object = None,
) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    return result


def _mock_session(result: MagicMock | None = None) -> AsyncMock:
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=MagicMock(dialect=MagicMock(name="postgresql")))
    session.info = {}
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(return_value=result if result is not None else _result())
    return session


def _session_with_results(results: list[MagicMock]) -> AsyncMock:
    session = _mock_session()
    queue = list(results)

    async def _execute(*_a: object, **_kw: object) -> MagicMock:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _patch_db_seams(monkeypatch: pytest.MonkeyPatch, session: AsyncMock) -> None:
    """Patch the engine/factory seams the API boot wrapper resolves lazily."""
    engine = MagicMock()
    factory_cm = MagicMock()
    factory_cm.__aenter__ = AsyncMock(return_value=session)
    factory_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=factory_cm)
    monkeypatch.setattr("modulo.api.dependencies.get_or_create_engine", lambda _settings: engine)
    monkeypatch.setattr("modulo.api.dependencies.get_or_create_session_factory", lambda _engine: factory)
    return factory


def _mock_factory(session: AsyncMock) -> MagicMock:
    """Session factory whose ``factory()`` yields the given mock session."""
    factory_cm = MagicMock()
    factory_cm.__aenter__ = AsyncMock(return_value=session)
    factory_cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=factory_cm)


@pytest.mark.anyio
async def test_seed_modulo_users_empty_users_string() -> None:
    session = _mock_session()
    await seed_modulo_users(_mock_factory(session), "")
    assert session.add.call_count == 0


@pytest.mark.anyio
async def test_seed_modulo_users_no_org() -> None:
    session = _mock_session(_result(scalar_one_or_none=None))
    await seed_modulo_users(_mock_factory(session), "admin:secret1")
    assert session.add.call_count == 0


@pytest.mark.anyio
async def test_seed_modulo_users_new_user(monkeypatch: pytest.MonkeyPatch) -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    session = _session_with_results([_result(scalar_one_or_none=org), _result(scalar_one_or_none=None)])
    monkeypatch.setattr("modulo.auth.passwords.hash_password", lambda pw: "$2b$12$fakehash")
    await seed_modulo_users(_mock_factory(session), "user@example.com:secret1")
    assert session.add.call_count == 2


@pytest.mark.anyio
async def test_seed_modulo_users_seeds_each_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    session = _session_with_results(
        [_result(scalar_one_or_none=org), _result(scalar_one_or_none=None), _result(scalar_one_or_none=None)]
    )
    monkeypatch.setattr("modulo.auth.passwords.hash_password", lambda pw: "$2b$12$fakehash")
    await seed_modulo_users(_mock_factory(session), "a@example.com:pw1,b@example.com:pw2")
    assert session.add.call_count == 4  # account + membership per entry


@pytest.mark.anyio
async def test_seed_modulo_user_admin_email_gets_admin_role(monkeypatch: pytest.MonkeyPatch) -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(_result(scalar_one_or_none=None))
    monkeypatch.setattr("modulo.auth.passwords.hash_password", lambda pw: "$2b$12$fakehash")
    await seed_modulo_user(session, org, "admin:secret1")
    added_membership = session.add.call_args_list[1].args[0]
    assert added_membership.role == "admin"


@pytest.mark.anyio
async def test_seed_modulo_user_runner_role(monkeypatch: pytest.MonkeyPatch) -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(_result(scalar_one_or_none=None))
    monkeypatch.setattr("modulo.auth.passwords.hash_password", lambda pw: "$2b$12$fakehash")
    await seed_modulo_user(session, org, "user@example.com:secret1")
    added_membership = session.add.call_args_list[1].args[0]
    assert added_membership.role == "runner"


@pytest.mark.anyio
async def test_seed_modulo_user_skips_entries_without_colon() -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(_result(scalar_one_or_none=None))
    await seed_modulo_user(session, org, "no-colon-entry")
    await seed_modulo_user(session, org, ":password-only")
    await seed_modulo_user(session, org, "   ")
    assert session.add.call_count == 0


@pytest.mark.anyio
async def test_seed_modulo_user_existing_hashed_noop() -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    existing = SimpleNamespace(id=uuid.uuid4(), password_hash="$2b$12$alreadyhashed")
    session = _mock_session(_result(scalar_one_or_none=existing))
    await seed_modulo_user(session, org, "user@example.com:secret1")
    assert session.add.call_count == 0


@pytest.mark.anyio
async def test_seed_modulo_user_plaintext_gets_hashed(monkeypatch: pytest.MonkeyPatch) -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(_result(scalar_one_or_none=None))
    monkeypatch.setattr("modulo.auth.passwords.hash_password", lambda pw: f"$2b$12$hashed({pw})")
    await seed_modulo_user(session, org, "admin:secret1")
    added_account = session.add.call_args_list[0].args[0]
    assert added_account.password_hash.startswith("$2b$12$hashed(secret1)")


@pytest.mark.anyio
async def test_seed_modulo_user_bcrypt_hash_passthrough() -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(_result(scalar_one_or_none=None))
    pw_hash = "$2b$12$alreadyhashedvalue"
    await seed_modulo_user(session, org, f"user@example.com:{pw_hash}")
    added_account = session.add.call_args_list[0].args[0]
    assert added_account.password_hash == pw_hash


@pytest.mark.anyio
async def test_rehash_existing_user_updates_role_for_admin_email() -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    existing = SimpleNamespace(id=uuid.uuid4(), password_hash=None)
    membership = SimpleNamespace(role="runner")
    session = _mock_session(_result(scalar_one_or_none=membership))
    await rehash_existing_user(session, org, existing, "admin", "$2b$12$newhash")
    assert existing.password_hash == "$2b$12$newhash"
    assert membership.role == "admin"


@pytest.mark.anyio
async def test_rehash_existing_user_creates_missing_membership() -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    existing = SimpleNamespace(id=uuid.uuid4(), password_hash=None)
    session = _mock_session(_result(scalar_one_or_none=None))
    await rehash_existing_user(session, org, existing, "ops@example.com", "$2b$12$newhash")
    added = session.add.call_args_list[0].args[0]
    assert added.role == "runner"
    assert added.account_id == existing.id


@pytest.mark.anyio
async def test_rehash_existing_user_keeps_runner_role() -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    existing = SimpleNamespace(id=uuid.uuid4(), password_hash="old")
    membership = SimpleNamespace(role="runner")
    session = _mock_session(_result(scalar_one_or_none=membership))
    await rehash_existing_user(session, org, existing, "ops@example.com", "$2b$12$newhash")
    assert membership.role == "runner"
    assert session.add.call_count == 0


def test_main_seam_aliases_are_the_promoted_functions() -> None:
    """The API wrappers are the promoted functions themselves (zero drift)."""
    assert main_module._seed_modulo_user is seed_modulo_user
    assert main_module._rehash_existing_user is rehash_existing_user


@pytest.mark.anyio
async def test_main_users_wrapper_resolves_factory_and_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """main._seed_modulo_users resolves the API-layer factory and delegates."""
    called: dict[str, Any] = {}

    async def _fake_seeder(factory: Any, modulo_users: str) -> None:
        called["factory"] = factory
        called["modulo_users"] = modulo_users

    monkeypatch.setattr(main_module, "seed_modulo_users", _fake_seeder)
    session = _mock_session()
    factory = _patch_db_seams(monkeypatch, session)
    settings = _make_settings(modulo_users="x:y")
    await main_module._seed_modulo_users(settings)
    assert called["factory"] is factory
    assert called["modulo_users"] == "x:y"


@pytest.mark.anyio
async def test_main_users_wrapper_skips_empty_users_without_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty MODULO_USERS returns before any engine/factory resolution."""

    async def _fail(*_a: object, **_kw: object) -> None:
        raise AssertionError("seeder must not run for an empty MODULO_USERS")

    monkeypatch.setattr(main_module, "seed_modulo_users", _fail)
    settings = _make_settings(modulo_users="")
    await main_module._seed_modulo_users(settings)
