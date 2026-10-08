"""Unit tests for the promoted MODULO_USERS seeder (FAR-671).

``modulo.db.seed.seed_modulo_users`` / ``seed_modulo_user`` /
``rehash_existing_user`` are the single implementation (promoted verbatim from
``modulo.api.main``); the API keeps thin aliases as its boot-path/test seam.
These tests exercise the promoted functions directly through the
``modulo.api.dependencies`` seams — the same seam shape the pre-promotion
tests patched. ``modulo.api.main`` is imported lazily inside the tests that
need it (Settings is constructed at import time): the minimal env is provided
by an autouse monkeypatch fixture, so the mutation is scoped to each test and
reverted instead of leaking into the whole pytest process.
"""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.db.seed import rehash_existing_user, seed_modulo_user, seed_modulo_users
from modulo.settings import Settings

_VALID_32 = "a" * 32
_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


@pytest.fixture(autouse=True)
def _scoped_bootstrap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal env so a lazy ``modulo.api.main`` import can build Settings.

    Scoped + reverted per test (the former module-level
    ``os.environ.setdefault`` leaked a synthetic DATABASE_URL into every test
    in the process).
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", _VALID_32)
    monkeypatch.setenv("FERNET_KEY", "a" * 32)


@pytest.fixture(autouse=True)
def audit_append_spy(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Spy the audit append the seeder invokes (FAR-1561).

    Left in place the real append would run against the mocked session (and its
    ``session.add`` accounting, which these tests assert on), so it is replaced
    here and asserted on directly — the wiring assertions live in the new
    ``test_seed_modulo_user`` / ``test_rehash_existing_user`` tests below.
    """
    spy = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr("modulo.core.audit_logger.append_audit_event", spy)
    return spy


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
    import modulo.api.main as main_module

    assert main_module._seed_modulo_user is seed_modulo_user
    assert main_module._rehash_existing_user is rehash_existing_user


@pytest.mark.anyio
async def test_main_users_wrapper_resolves_factory_and_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """main._seed_modulo_users resolves the API-layer factory and delegates."""
    import modulo.api.main as main_module

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
    import modulo.api.main as main_module

    async def _fail(*_a: object, **_kw: object) -> None:
        raise AssertionError("seeder must not run for an empty MODULO_USERS")

    monkeypatch.setattr(main_module, "seed_modulo_users", _fail)
    settings = _make_settings(modulo_users="")
    await main_module._seed_modulo_users(settings)


# ---------------------------------------------------------------------------
# Boot-seed audit append (FAR-1561)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_seed_modulo_user_records_system_actor_audit(
    audit_append_spy: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A created account + role grant lands on the org's audit chain with the
    SYSTEM actor and an honest ``boot_seed`` actor source."""
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(_result(scalar_one_or_none=None))
    monkeypatch.setattr("modulo.auth.passwords.hash_password", lambda pw: "$2b$12$fakehash")

    await seed_modulo_user(session, org, "admin:secret1")

    audit_append_spy.assert_awaited_once()
    kwargs = audit_append_spy.await_args.kwargs
    assert kwargs["org_id"] == org.id
    assert kwargs["event_type"] == "user_seeded"
    assert kwargs["actor_user_id"] is None
    assert kwargs["resource_type"] == "user"
    payload = kwargs["payload_json"]
    assert payload["actor"] == "system"
    assert payload["actor_source"] == "boot_seed"
    assert payload["email"] == "admin"
    assert payload["role"] == "admin"


@pytest.mark.anyio
async def test_seed_modulo_user_records_runner_grant_as_runner_role(
    audit_append_spy: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-admin seed records the runner role it actually granted."""
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(_result(scalar_one_or_none=None))
    monkeypatch.setattr("modulo.auth.passwords.hash_password", lambda pw: "$2b$12$fakehash")

    await seed_modulo_user(session, org, "user@example.com:secret1")

    audit_append_spy.assert_awaited_once()
    assert audit_append_spy.await_args.kwargs["payload_json"]["role"] == "runner"


@pytest.mark.anyio
async def test_seed_modulo_user_existing_account_records_nothing(audit_append_spy: AsyncMock) -> None:
    """Idempotent re-runs append no event — nothing changed."""
    org = SimpleNamespace(id=uuid.uuid4())
    existing = SimpleNamespace(id=uuid.uuid4(), password_hash="$2b$12$alreadyhashed")
    session = _mock_session(_result(scalar_one_or_none=existing))

    await seed_modulo_user(session, org, "user@example.com:secret1")

    audit_append_spy.assert_not_awaited()


@pytest.mark.anyio
async def test_rehash_existing_user_records_credential_and_role_grant(audit_append_spy: AsyncMock) -> None:
    """A rehash can GRANT the admin role — the credential rotation and the role
    it landed on are recorded together."""
    org = SimpleNamespace(id=uuid.uuid4())
    existing = SimpleNamespace(id=uuid.uuid4(), password_hash=None)
    membership = SimpleNamespace(role="runner")
    session = _mock_session(_result(scalar_one_or_none=membership))

    await rehash_existing_user(session, org, existing, "admin", "$2b$12$newhash")

    audit_append_spy.assert_awaited_once()
    kwargs = audit_append_spy.await_args.kwargs
    assert kwargs["event_type"] == "user_rehashed"
    assert kwargs["actor_user_id"] is None
    assert kwargs["resource_id"] == existing.id
    payload = kwargs["payload_json"]
    assert payload["actor_source"] == "boot_seed"
    assert payload["role"] == "admin"
    assert payload["role_granted"] is True


@pytest.mark.anyio
async def test_rehash_existing_user_noop_role_records_role_granted_false(audit_append_spy: AsyncMock) -> None:
    """A rehash that grants nothing still records the credential change — with
    ``role_granted`` false so the chain never implies an escalation."""
    org = SimpleNamespace(id=uuid.uuid4())
    existing = SimpleNamespace(id=uuid.uuid4(), password_hash="old")
    membership = SimpleNamespace(role="runner")
    session = _mock_session(_result(scalar_one_or_none=membership))

    await rehash_existing_user(session, org, existing, "ops@example.com", "$2b$12$newhash")

    payload = audit_append_spy.await_args.kwargs["payload_json"]
    assert payload["role"] == "runner"
    assert payload["role_granted"] is False


@pytest.mark.anyio
async def test_rehash_existing_user_already_admin_records_role_granted_false(
    audit_append_spy: AsyncMock,
) -> None:
    """``role_granted`` reports the ACTUAL escalation, not the admin email: an
    admin account whose membership is ALREADY admin escalates nothing, so the
    chain must never imply a grant that did not happen."""
    org = SimpleNamespace(id=uuid.uuid4())
    existing = SimpleNamespace(id=uuid.uuid4(), password_hash=None)
    membership = SimpleNamespace(role="admin")
    session = _mock_session(_result(scalar_one_or_none=membership))

    await rehash_existing_user(session, org, existing, "admin", "$2b$12$newhash")

    payload = audit_append_spy.await_args.kwargs["payload_json"]
    assert membership.role == "admin"  # unchanged — nothing was escalated
    assert payload["role"] == "admin"
    assert payload["role_granted"] is False


@pytest.mark.anyio
async def test_rehash_existing_user_creates_admin_membership_records_role_granted_true(
    audit_append_spy: AsyncMock,
) -> None:
    """A missing membership created straight at ``admin`` IS an escalation
    (no membership -> admin), so ``role_granted`` must be true there too."""
    org = SimpleNamespace(id=uuid.uuid4())
    existing = SimpleNamespace(id=uuid.uuid4(), password_hash=None)
    session = _mock_session(_result(scalar_one_or_none=None))

    await rehash_existing_user(session, org, existing, "admin@modulo.run", "$2b$12$newhash")

    payload = audit_append_spy.await_args.kwargs["payload_json"]
    assert session.add.call_args_list[0].args[0].role == "admin"
    assert payload["role"] == "admin"
    assert payload["role_granted"] is True


@pytest.mark.anyio
async def test_audit_failure_never_fails_the_seed(
    audit_append_spy: AsyncMock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The grant commits even when its record cannot be written — fail-open
    with a loud log (mirrors the admin create-user route)."""
    audit_append_spy.side_effect = RuntimeError("audit db down")
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(_result(scalar_one_or_none=None))
    monkeypatch.setattr("modulo.auth.passwords.hash_password", lambda pw: "$2b$12$fakehash")

    with caplog.at_level("ERROR"):
        await seed_modulo_user(session, org, "admin:secret1")

    assert session.add.call_count == 2  # account + membership, unharmed
    assert any("db.seed.boot_user_audit_failed" in record.message for record in caplog.records)


@pytest.mark.anyio
async def test_audit_cancellation_is_never_swallowed(
    audit_append_spy: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CancelledError`` is the one exception the fail-open record must not
    swallow — a cancelled boot seed must actually stop."""
    audit_append_spy.side_effect = asyncio.CancelledError()
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(_result(scalar_one_or_none=None))
    monkeypatch.setattr("modulo.auth.passwords.hash_password", lambda pw: "$2b$12$fakehash")

    with pytest.raises(asyncio.CancelledError):
        await seed_modulo_user(session, org, "admin:secret1")
