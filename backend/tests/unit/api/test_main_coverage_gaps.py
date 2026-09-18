"""Targeted coverage tests for ``modulo.api.main`` (FAR-573).

Complements test_main.py (which covers ``_verify_db_connectivity``) by
exercising the boot path the existing suite leaves uncovered: the migration
runner + advisory lock, boot guards and seeds, the full lifespan (startup +
teardown), license/OTel configuration, plugin discovery, and the retention
loop. Unit tier: every external seam (DB engine/factory, alembic, Redis,
checkpointer, seed CRUD) is patched at its module boundary - nothing connects.
"""

import asyncio
import logging
import uuid
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, ClassVar, Self
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.exc import SQLAlchemyError

import modulo.api.main as main
from modulo.settings import Settings

# ── _seed_modulo_users / _seed_modulo_user / _rehash_existing_user ──


@pytest.mark.anyio
async def test_seed_modulo_users_empty_env(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings(modulo_users="")
    session = _mock_session()
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    await main._seed_modulo_users(settings)
    assert session.add.call_count == 0


@pytest.mark.anyio
async def test_seed_modulo_users_no_org(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings(modulo_users="admin:secret1")
    session = _mock_session(_result(scalar_one_or_none=None))
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    await main._seed_modulo_users(settings)
    assert session.add.call_count == 0


@pytest.mark.anyio
async def test_seed_modulo_users_new_user(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings(modulo_users="user@example.com:secret1")
    org = SimpleNamespace(id=uuid.uuid4())
    session = _session_with_results([_result(scalar_one_or_none=org), _result(scalar_one_or_none=None)])
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    monkeypatch.setattr("modulo.auth.passwords.hash_password", lambda pw: "$2b$12$fakehash")
    await main._seed_modulo_users(settings)
    assert session.add.call_count == 2


@pytest.mark.anyio
async def test_seed_modulo_user_admin_email_gets_admin_role(monkeypatch: pytest.MonkeyPatch) -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(_result(scalar_one_or_none=None))
    monkeypatch.setattr("modulo.auth.passwords.hash_password", lambda pw: "$2b$12$fakehash")
    await main._seed_modulo_user(session, org, "admin:secret1")
    added_membership = session.add.call_args_list[1].args[0]
    assert added_membership.role == "admin"


@pytest.mark.anyio
async def test_seed_modulo_user_runner_role(monkeypatch: pytest.MonkeyPatch) -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(_result(scalar_one_or_none=None))
    monkeypatch.setattr("modulo.auth.passwords.hash_password", lambda pw: "$2b$12$fakehash")
    await main._seed_modulo_user(session, org, "user@example.com:secret1")
    added_membership = session.add.call_args_list[1].args[0]
    assert added_membership.role == "runner"


@pytest.mark.anyio
async def test_seed_modulo_user_skips_entries_without_colon() -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(_result(scalar_one_or_none=None))
    await main._seed_modulo_user(session, org, "no-colon-entry")
    await main._seed_modulo_user(session, org, ":password-only")
    await main._seed_modulo_user(session, org, "   ")
    assert session.add.call_count == 0


@pytest.mark.anyio
async def test_seed_modulo_user_existing_hashed_noop() -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    existing = SimpleNamespace(id=uuid.uuid4(), password_hash="$2b$12$alreadyhashed")
    session = _mock_session(_result(scalar_one_or_none=existing))
    await main._seed_modulo_user(session, org, "user@example.com:secret1")
    assert session.add.call_count == 0


@pytest.mark.anyio
async def test_rehash_existing_user_updates_role_for_admin_email() -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    existing = SimpleNamespace(id=uuid.uuid4(), password_hash=None)
    membership = SimpleNamespace(role="runner")
    session = _mock_session(_result(scalar_one_or_none=membership))
    await main._rehash_existing_user(session, org, existing, "admin", "$2b$12$newhash")
    assert existing.password_hash == "$2b$12$newhash"
    assert membership.role == "admin"


@pytest.mark.anyio
async def test_rehash_existing_user_creates_missing_membership() -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    existing = SimpleNamespace(id=uuid.uuid4(), password_hash=None)
    session = _mock_session(_result(scalar_one_or_none=None))
    await main._rehash_existing_user(session, org, existing, "user@example.com", "$2b$12$newhash")
    added = session.add.call_args.args[0]
    assert added.role == "runner"
    assert added.organisation_id == org.id


# ── _seed_sso_providers ──


@pytest.mark.anyio
async def test_seed_sso_providers_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _mock_session()
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    await main._seed_sso_providers(_make_settings(modulo_oidc_providers=""))
    await main._seed_sso_providers(_make_settings(modulo_oidc_providers="[]"))
    assert session.add.call_count == 0


@pytest.mark.anyio
async def test_seed_sso_providers_already_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings(
        modulo_oidc_providers='[{"provider_id": "p1", "client_id": "c1", "client_secret": "s1", "discovery_url": "https://d"}]'
    )
    session = _mock_session(_result(scalar_one_or_none=MagicMock()))
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    await main._seed_sso_providers(settings)
    assert session.add.call_count == 0


@pytest.mark.anyio
async def test_seed_sso_providers_no_org(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings(
        modulo_oidc_providers='[{"provider_id": "p1", "client_id": "c1", "client_secret": "s1", "discovery_url": "https://d"}]'
    )
    session = _mock_session(_result(scalar_one_or_none=None))
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    await main._seed_sso_providers(settings)
    assert session.add.call_count == 0


@pytest.mark.anyio
async def test_seed_sso_providers_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings(modulo_oidc_providers="not-json")
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(_result(scalar_one_or_none=org))
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    await main._seed_sso_providers(settings)
    assert session.add.call_count == 0


@pytest.mark.anyio
async def test_seed_sso_providers_entry_missing_fields_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings(modulo_oidc_providers='[{"provider_id": "p1"}]')
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(_result(scalar_one_or_none=org))
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    await main._seed_sso_providers(settings)
    assert session.add.call_count == 0


@pytest.mark.anyio
async def test_seed_sso_providers_valid_entry_seeded(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings(
        modulo_oidc_providers='[{"provider_id": "p1", "client_id": "c1", "client_secret": "s1", "discovery_url": "https://d"}]'
    )
    org = SimpleNamespace(id=uuid.uuid4())
    session = _session_with_results([_result(scalar_one_or_none=None), _result(scalar_one_or_none=org)])
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    await main._seed_sso_providers(settings)
    assert session.add.call_count == 1


# ── _seed_system_schemas / _seed_environment_profiles ──


@pytest.mark.anyio
async def test_seed_system_schemas_no_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _mock_session(_result(scalars=[], scalar_one_or_none=None))
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    seed = AsyncMock()
    monkeypatch.setattr("modulo.db.seed.seed_system_schemas", seed)
    await main._seed_system_schemas(_make_settings())
    assert seed.await_count == 0


@pytest.mark.anyio
async def test_seed_system_schemas_admin_found(monkeypatch: pytest.MonkeyPatch) -> None:
    orgs = [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]
    admin = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session()
    results = [_result(scalars=orgs), _result(scalar_one_or_none=admin)]

    async def _execute(*_a: object, **_kw: object) -> MagicMock:
        return results.pop(0) if results else results[-1]

    session.execute = AsyncMock(side_effect=_execute)
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    seed = AsyncMock()
    monkeypatch.setattr("modulo.db.seed.seed_system_schemas", seed)
    await main._seed_system_schemas(_make_settings())
    assert seed.await_count == 2


@pytest.mark.anyio
async def test_seed_environment_profiles_no_org(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _mock_session(_result(scalar_one_or_none=None))
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    create_profile = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.environment_profile.create_environment_profile", create_profile)
    await main._seed_environment_profiles(_make_settings())
    assert create_profile.await_count == 0


@pytest.mark.anyio
async def test_seed_environment_profiles_already_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session()
    results = [_result(scalar_one_or_none=org), _result(scalar_one_or_none=MagicMock())]

    async def _execute(*_a: object, **_kw: object) -> MagicMock:
        return results.pop(0) if results else results[-1]

    session.execute = AsyncMock(side_effect=_execute)
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    create_profile = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.environment_profile.create_environment_profile", create_profile)
    await main._seed_environment_profiles(_make_settings())
    assert create_profile.await_count == 0


@pytest.mark.anyio
async def test_seed_environment_profiles_creates_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    admin = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session()
    results = [
        _result(scalar_one_or_none=org),
        _result(scalar_one_or_none=None),
        _result(scalar_one_or_none=admin),
    ]

    async def _execute(*_a: object, **_kw: object) -> MagicMock:
        return results.pop(0) if results else results[-1]

    session.execute = AsyncMock(side_effect=_execute)
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    create_profile = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.environment_profile.create_environment_profile", create_profile)
    await main._seed_environment_profiles(_make_settings())
    assert create_profile.await_count == 1


@pytest.mark.anyio
async def test_seed_environment_profiles_first_account_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    first_account = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session()
    results = [
        _result(scalar_one_or_none=org),
        _result(scalar_one_or_none=None),
        _result(scalar_one_or_none=None),
        _result(scalar_one_or_none=first_account),
    ]

    async def _execute(*_a: object, **_kw: object) -> MagicMock:
        return results.pop(0) if results else results[-1]

    session.execute = AsyncMock(side_effect=_execute)
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    create_profile = AsyncMock()
    monkeypatch.setattr("modulo.db.crud.environment_profile.create_environment_profile", create_profile)
    await main._seed_environment_profiles(_make_settings())
    assert create_profile.await_count == 1


# ── _init_checkpointer / _run_retention_loop ──


def _checkpointer_mock() -> MagicMock:
    saver = MagicMock()
    saver.setup = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=saver)
    cm.__aexit__ = AsyncMock(return_value=False)
    cls = MagicMock()
    cls.from_conn_string = MagicMock(return_value=cm)
    return cls


@pytest.mark.anyio
async def test_init_checkpointer_success(monkeypatch: pytest.MonkeyPatch) -> None:
    cls = _checkpointer_mock()
    monkeypatch.setattr("modulo.core.pipeline_engine.modulo_saver.ModuloPostgresSaver", cls)
    await main._init_checkpointer("postgresql://u:p@h/db", _FERNET_KEY, "")
    assert cls.from_conn_string.call_count == 1


@pytest.mark.anyio
async def test_init_checkpointer_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    cls = MagicMock()
    cls.from_conn_string = MagicMock(side_effect=RuntimeError("conn refused"))
    monkeypatch.setattr("modulo.core.pipeline_engine.modulo_saver.ModuloPostgresSaver", cls)
    await main._init_checkpointer("postgresql://u:p@h/db", _FERNET_KEY, "")
    assert cls.from_conn_string.call_count == 1


@pytest.mark.anyio
async def test_run_retention_loop_logs_and_exits_on_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _mock_session()
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    batch = AsyncMock(side_effect=[RuntimeError("blip"), 3])
    monkeypatch.setattr("modulo.db.crud.run.batch_delete_old_terminal_runs", batch)

    sleep_calls = {"n": 0}

    async def _fake_sleep(_seconds: int) -> None:
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(main, "asyncio", SimpleNamespace(sleep=_fake_sleep, CancelledError=asyncio.CancelledError))
    with suppress(asyncio.CancelledError):
        await main._run_retention_loop()
    assert batch.await_count == 2


# ── _configure_license_and_otel / _discover_plugins ──


def test_configure_license_and_otel_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    setup_otel = MagicMock()
    monkeypatch.setattr(main, "setup_otel", setup_otel)
    monkeypatch.setattr("modulo.core.license.check_production_public_key", MagicMock())
    main._configure_license_and_otel(_make_settings())
    assert setup_otel.call_count == 1


def test_configure_license_sets_public_key(monkeypatch: pytest.MonkeyPatch) -> None:
    setup_otel = MagicMock()
    monkeypatch.setattr(main, "setup_otel", setup_otel)
    set_public_key = MagicMock()
    monkeypatch.setattr("modulo.core.license.set_public_key", set_public_key)
    monkeypatch.setattr("modulo.core.license.check_production_public_key", MagicMock())
    settings = _make_settings(modulo_license_public_key="test-public-key")
    main._configure_license_and_otel(settings)
    assert set_public_key.call_count == 1


def test_configure_license_production_check_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "setup_otel", MagicMock())
    check = MagicMock(side_effect=RuntimeError("bad key"))
    monkeypatch.setattr("modulo.core.license.check_production_public_key", check)
    main._configure_license_and_otel(_make_settings())
    assert check.call_count == 1


def test_configure_license_sqlite_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    setup_otel = MagicMock()
    monkeypatch.setattr(main, "setup_otel", setup_otel)
    monkeypatch.setattr("modulo.core.license.check_production_public_key", MagicMock())
    main._configure_license_and_otel(_make_settings(modulo_db="sqlite"))
    assert setup_otel.call_count == 1


def test_configure_license_requires_redis() -> None:
    settings = _make_settings(redis_url="")
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        main._configure_license_and_otel(settings)


# ── _discover_plugins ──


def test_discover_plugins_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    get_registry = MagicMock()
    monkeypatch.setattr("modulo.core.plugin_registry.get_plugin_registry", get_registry)
    main._discover_plugins(_make_settings(modulo_plugin_discovery=False))
    assert get_registry.call_count == 0


def test_discover_plugins_none_found(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = MagicMock()
    registry.discover_plugins = MagicMock(return_value=[])
    monkeypatch.setattr("modulo.core.plugin_registry.get_plugin_registry", MagicMock(return_value=registry))
    main._discover_plugins(_make_settings(modulo_plugin_discovery=True))
    assert registry.discover_plugins.call_count == 1


def test_discover_plugins_found(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = MagicMock()
    plugin.PLUGIN_ID = "p1"
    registry = MagicMock()
    registry.discover_plugins = MagicMock(return_value=[plugin])
    monkeypatch.setattr("modulo.core.plugin_registry.get_plugin_registry", MagicMock(return_value=registry))
    main._discover_plugins(_make_settings(modulo_plugin_discovery=True))
    assert registry.discover_plugins.call_count == 1


# ── _register_shutdown_manager / _start_background_tasks / _teardown ──


@pytest.mark.anyio
async def test_register_shutdown_manager_and_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock()
    engine.dispose = AsyncMock()
    monkeypatch.setattr(main, "get_or_create_engine", lambda _s: engine)
    main._register_shutdown_manager(_make_settings())
    await main._shutdown_manager.shutdown()
    assert engine.dispose.await_count == 1


def test_register_shutdown_manager_failure_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(main, "get_or_create_engine", MagicMock(side_effect=RuntimeError("boom")))
    with caplog.at_level(logging.WARNING, logger="modulo.api.main"):
        main._register_shutdown_manager(_make_settings())
    assert any("shutdown_manager_init_failed" in rec.message for rec in caplog.records)


@pytest.mark.anyio
async def test_start_and_teardown_background_tasks_with_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings(watchdog_enabled=True)

    async def _fake_watchdog(_settings: object) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr("modulo.core.watchdog.worker_liveness.run_worker_liveness_watchdog", _fake_watchdog)
    monkeypatch.setattr(main, "configure_event_bus", AsyncMock())
    monkeypatch.setattr("modulo.core.events.redis_broker.RedisEventBroker", MagicMock())

    expiry_instance = MagicMock()
    expiry_instance.start = AsyncMock()
    expiry_instance.stop = AsyncMock()
    monkeypatch.setattr(main, "ClaimExpiryJob", MagicMock(return_value=expiry_instance))

    session = _mock_session()
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))

    tasks = await main._start_background_tasks(settings)
    assert tasks["watchdog_task"] is not None
    assert expiry_instance.start.await_count == 1
    await main._teardown_tasks(tasks)
    assert expiry_instance.stop.await_count == 1


# ── Full lifespan (startup + teardown) ──


def _patch_lifespan_seams(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    engine = _mock_engine(_result(scalar_one=1))
    session = _mock_session()
    _patch_db_seams(monkeypatch, engine, _mock_factory(session))
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "_db_is_at_migration_head", AsyncMock(return_value=True))
    monkeypatch.setattr("modulo.settings.validate_break_glass_boot", MagicMock())
    monkeypatch.setattr(main, "setup_otel", MagicMock())
    monkeypatch.setattr("modulo.core.license.check_production_public_key", MagicMock())
    monkeypatch.setattr("modulo.core.license.set_public_key", MagicMock())
    monkeypatch.setattr("modulo.db.seed_demo.seed_demo_runtime", AsyncMock(return_value=None))
    monkeypatch.setattr("modulo.core.seed_data.cost_components.seed_cost_components", AsyncMock())
    monkeypatch.setattr("modulo.core.pipeline_engine.modulo_saver.ModuloPostgresSaver", _checkpointer_mock())
    monkeypatch.setattr(
        "modulo.core.runtime_config.store.get_runtime_config_store", MagicMock(return_value=MagicMock())
    )
    monkeypatch.setattr(main, "configure_event_bus", AsyncMock())
    monkeypatch.setattr("modulo.core.events.redis_broker.RedisEventBroker", MagicMock())
    expiry_instance = MagicMock()
    expiry_instance.start = AsyncMock()
    expiry_instance.stop = AsyncMock()
    monkeypatch.setattr("modulo.core.hitl_manager.expiry_job.ClaimExpiryJob", MagicMock(return_value=expiry_instance))
    monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncSession", _FakeAsyncSession)


@pytest.mark.anyio
async def test_lifespan_full_boot_and_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings()
    _patch_lifespan_seams(monkeypatch, settings)
    async with main._lifespan(main.app):
        pass
    assert main.app.title == "Modulo"


@pytest.mark.anyio
async def test_lifespan_seeds_demo_orgs_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings(modulo_seed_demo_orgs=True)
    _patch_lifespan_seams(monkeypatch, settings)
    seed_demo_orgs = AsyncMock()
    monkeypatch.setattr("modulo.core.seed_data.demo_data.seed_demo_orgs", seed_demo_orgs)
    async with main._lifespan(main.app):
        assert main.app.title == "Modulo"
    assert seed_demo_orgs.await_count == 1


_VALID_32 = "a" * 32
_FERNET_KEY = Fernet.generate_key().decode()


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
    scalar: Any = 0,
    scalar_one: Any = True,
    scalar_one_or_none: Any = None,
    rows: list[Any] | None = None,
    scalars: list[Any] | None = None,
) -> MagicMock:
    """A mocked execute() result covering every accessor main.py uses."""
    r = MagicMock()
    r.scalar = MagicMock(return_value=scalar)
    r.scalar_one = MagicMock(return_value=scalar_one)
    r.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    r.fetchall = MagicMock(return_value=rows if rows is not None else [])
    sc = MagicMock()
    sc.all = MagicMock(return_value=scalars if scalars is not None else [])
    r.scalars = MagicMock(return_value=sc)
    return r


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


def _mock_engine(conn_execute_result: MagicMock | None = None) -> MagicMock:
    """Engine whose connect() yields a working async connection."""
    engine = MagicMock()
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=conn_execute_result if conn_execute_result is not None else MagicMock())
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    engine.connect = MagicMock(return_value=cm)
    return engine


def _session_with_results(results: list[MagicMock]) -> AsyncMock:
    """Mock session whose execute() pops from ``results`` (last repeats)."""
    session = _mock_session()
    queue = list(results)

    async def _execute(*_a: object, **_kw: object) -> MagicMock:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _mock_factory(session: AsyncMock) -> MagicMock:
    """Session factory whose ``factory()`` yields the given mock session."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _patch_db_seams(monkeypatch: pytest.MonkeyPatch, engine: MagicMock, factory: MagicMock) -> None:
    """Patch the engine/factory seams both at module level and function-local."""
    monkeypatch.setattr(main, "get_or_create_engine", lambda _settings: engine)
    monkeypatch.setattr("modulo.api.dependencies.get_or_create_engine", lambda _settings: engine)
    monkeypatch.setattr("modulo.api.dependencies.get_or_create_session_factory", lambda _engine: factory)


class _FakeAsyncSession:
    """Drop-in for ``sqlalchemy.ext.asyncio.AsyncSession`` in _seed_tier_catalog."""

    instantiations: ClassVar[list["_FakeAsyncSession"]] = []

    def __init__(self, *_a: object, **_kw: object) -> None:
        _FakeAsyncSession.instantiations.append(self)
        self.begin_cm = AsyncMock()
        self.begin_cm.__aenter__ = AsyncMock(return_value=None)
        self.begin_cm.__aexit__ = AsyncMock(return_value=False)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def begin(self) -> AsyncMock:
        return self.begin_cm

    async def execute(self, *_a: object, **_kw: object) -> MagicMock:
        return MagicMock()


# ── _resolve_alembic_ini / _db_is_at_migration_head ──


def test_resolve_alembic_ini_finds_backend_config() -> None:
    path = main._resolve_alembic_ini()
    assert path.name == "alembic.ini"
    assert path.exists() is True


@pytest.mark.anyio
async def test_db_is_at_migration_head_true(monkeypatch: pytest.MonkeyPatch) -> None:
    script_dir = MagicMock()
    script_dir.get_current_head = MagicMock(return_value="head123")
    monkeypatch.setattr("alembic.script.ScriptDirectory.from_config", MagicMock(return_value=script_dir))
    monkeypatch.setattr(main, "get_or_create_engine", lambda _s: _mock_engine(_result(rows=[("head123",)])))
    assert await main._db_is_at_migration_head(_make_settings()) is True


@pytest.mark.anyio
async def test_db_is_at_migration_head_false_on_version_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    script_dir = MagicMock()
    script_dir.get_current_head = MagicMock(return_value="head123")
    monkeypatch.setattr("alembic.script.ScriptDirectory.from_config", MagicMock(return_value=script_dir))
    monkeypatch.setattr(main, "get_or_create_engine", lambda _s: _mock_engine(_result(rows=[("old123",)])))
    assert await main._db_is_at_migration_head(_make_settings()) is False


@pytest.mark.anyio
async def test_db_is_at_migration_head_false_when_query_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    script_dir = MagicMock()
    script_dir.get_current_head = MagicMock(return_value="head123")
    monkeypatch.setattr("alembic.script.ScriptDirectory.from_config", MagicMock(return_value=script_dir))
    engine = MagicMock()
    conn = AsyncMock()
    conn.execute = AsyncMock(side_effect=RuntimeError("no table"))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    engine.connect = MagicMock(return_value=cm)
    monkeypatch.setattr(main, "get_or_create_engine", lambda _s: engine)
    assert await main._db_is_at_migration_head(_make_settings()) is False


@pytest.mark.anyio
async def test_db_is_at_migration_head_false_when_head_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    script_dir = MagicMock()
    script_dir.get_current_head = MagicMock(return_value=None)
    monkeypatch.setattr("alembic.script.ScriptDirectory.from_config", MagicMock(return_value=script_dir))
    assert await main._db_is_at_migration_head(_make_settings()) is False


@pytest.mark.anyio
async def test_db_is_at_migration_head_false_when_scriptdir_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("alembic.script.ScriptDirectory.from_config", MagicMock(side_effect=RuntimeError("bad ini")))
    assert await main._db_is_at_migration_head(_make_settings()) is False


# ── _migration_advisory_lock / _run_migrations ──


def _patch_migration_env(monkeypatch: pytest.MonkeyPatch, engine: MagicMock) -> None:
    _patch_db_seams(monkeypatch, engine, _mock_factory(_mock_session()))
    monkeypatch.setattr("alembic.command.upgrade", MagicMock())
    monkeypatch.setattr("modulo.db.migrations.env._to_sync_url", lambda url: url)
    monkeypatch.setattr("modulo.db.migrations.env.set_lock_held_by_caller", MagicMock())


@pytest.mark.anyio
async def test_run_migrations_skips_when_already_at_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "_db_is_at_migration_head", AsyncMock(return_value=True))
    bootstrap = AsyncMock()
    monkeypatch.setattr(main, "_run_bootstrap", bootstrap)
    await main._run_migrations(_make_settings())
    assert bootstrap.await_count == 0


@pytest.mark.anyio
async def test_run_migrations_acquires_lock_and_upgrades(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings()
    monkeypatch.setattr(main, "_db_is_at_migration_head", AsyncMock(return_value=False))
    bootstrap = AsyncMock()
    monkeypatch.setattr(main, "_run_bootstrap", bootstrap)
    _patch_migration_env(monkeypatch, _mock_engine(_result(scalar_one=True)))
    monkeypatch.setattr(
        main,
        "asyncio",
        SimpleNamespace(sleep=AsyncMock(), to_thread=AsyncMock(), CancelledError=asyncio.CancelledError),
    )
    await main._run_migrations(settings)
    assert bootstrap.await_count == 2


@pytest.mark.anyio
async def test_run_migrations_retries_then_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings()
    monkeypatch.setattr(main, "_db_is_at_migration_head", AsyncMock(return_value=False))
    monkeypatch.setattr(main, "_run_bootstrap", AsyncMock())
    _patch_migration_env(monkeypatch, _mock_engine(_result(scalar_one=True)))
    fake_asyncio = SimpleNamespace(
        sleep=AsyncMock(),
        to_thread=AsyncMock(side_effect=SQLAlchemyError("db down")),
        CancelledError=asyncio.CancelledError,
    )
    monkeypatch.setattr(main, "asyncio", fake_asyncio)
    with pytest.raises(RuntimeError, match="FATAL"):
        await main._run_migrations(settings)


@pytest.mark.anyio
async def test_run_migrations_unexpected_error_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_settings()
    monkeypatch.setattr(main, "_db_is_at_migration_head", AsyncMock(return_value=False))
    monkeypatch.setattr(main, "_run_bootstrap", AsyncMock())
    _patch_migration_env(monkeypatch, _mock_engine(_result(scalar_one=True)))
    monkeypatch.setattr("alembic.command.upgrade", MagicMock(side_effect=ValueError("boom")))
    fake_asyncio = SimpleNamespace(
        sleep=AsyncMock(),
        to_thread=AsyncMock(side_effect=ValueError("boom")),
        CancelledError=asyncio.CancelledError,
    )
    monkeypatch.setattr(main, "asyncio", fake_asyncio)
    with pytest.raises(RuntimeError, match="FATAL"):
        await main._run_migrations(settings)


@pytest.mark.anyio
async def test_migration_advisory_lock_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lock that never acquires exhausts the poll loop and fails the boot."""
    settings = _make_settings()
    monkeypatch.setattr(main, "_db_is_at_migration_head", AsyncMock(return_value=False))
    monkeypatch.setattr(main, "_run_bootstrap", AsyncMock())
    _patch_migration_env(monkeypatch, _mock_engine(_result(scalar_one=False)))
    monkeypatch.setattr(
        main,
        "asyncio",
        SimpleNamespace(sleep=AsyncMock(), to_thread=AsyncMock(), CancelledError=asyncio.CancelledError),
    )
    with pytest.raises(RuntimeError, match="FATAL"):
        await main._run_migrations(settings)


# ── _run_bootstrap / _run_break_glass_watchdog ──


@pytest.mark.anyio
async def test_run_bootstrap_success(monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap_roles = AsyncMock()
    monkeypatch.setattr("modulo.db.bootstrap_role.bootstrap_roles", bootstrap_roles)
    await main._run_bootstrap(_make_settings())
    assert bootstrap_roles.await_count == 1


@pytest.mark.anyio
async def test_run_bootstrap_break_glass_posture_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "modulo.db.bootstrap_role.bootstrap_roles",
        AsyncMock(side_effect=RuntimeError("Break-glass role posture assertion FAILED: superuser")),
    )
    await main._run_bootstrap(_make_settings())
    assert any("break_glass_role_posture_failed" in rec.message for rec in caplog.records)


@pytest.mark.anyio
async def test_run_bootstrap_generic_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap_roles = AsyncMock(side_effect=RuntimeError("db blip"))
    monkeypatch.setattr("modulo.db.bootstrap_role.bootstrap_roles", bootstrap_roles)
    await main._run_bootstrap(_make_settings())
    assert bootstrap_roles.await_count == 1


@pytest.mark.anyio
async def test_run_break_glass_watchdog_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    validate = MagicMock()
    monkeypatch.setattr("modulo.settings.validate_break_glass_boot", validate)
    await main._run_break_glass_watchdog(_make_settings())
    assert validate.call_count == 1


@pytest.mark.anyio
async def test_run_break_glass_watchdog_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "modulo.settings.validate_break_glass_boot", MagicMock(side_effect=RuntimeError("missing config"))
    )
    with pytest.raises(RuntimeError, match="missing config"):
        await main._run_break_glass_watchdog(_make_settings())


# ── _assert_no_owner_rows / _ensure_default_org ──


@pytest.mark.anyio
async def test_assert_no_owner_rows_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _mock_session(_result(scalars=[]))
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    await main._assert_no_owner_rows(_make_settings())
    assert session.execute.await_count == 1


@pytest.mark.anyio
async def test_assert_no_owner_rows_present_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = SimpleNamespace(account_id=uuid.uuid4())
    session = _mock_session(_result(scalars=[owner]))
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    with pytest.raises(RuntimeError, match="owner"):
        await main._assert_no_owner_rows(_make_settings())


@pytest.mark.anyio
async def test_ensure_default_org_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(_result(scalar_one_or_none=org))
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    await main._ensure_default_org(_make_settings())
    assert session.add.call_count == 0


@pytest.mark.anyio
async def test_ensure_default_org_creates(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _mock_session(_result(scalar_one_or_none=None))
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    seed_components = AsyncMock()
    monkeypatch.setattr("modulo.core.seed_data.cost_components.seed_cost_components_for_org", seed_components)
    await main._ensure_default_org(_make_settings())
    assert session.add.call_count == 1
    assert seed_components.await_count == 1


@pytest.mark.anyio
async def test_ensure_default_org_seed_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _mock_session(_result(scalar_one_or_none=None))
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    monkeypatch.setattr(
        "modulo.core.seed_data.cost_components.seed_cost_components_for_org",
        AsyncMock(side_effect=RuntimeError("seed blip")),
    )
    await main._ensure_default_org(_make_settings())
    assert session.add.call_count == 1


# ── _boot_seed / _seed_tier_catalog / demo + cost seeds ──


@pytest.mark.anyio
async def test_boot_seed_ok(capsys: pytest.CaptureFixture) -> None:
    async def _coro() -> str:
        return "3 rows"

    await main._boot_seed("thing", _coro())
    out = capsys.readouterr().out
    assert "[boot] seed thing: ok (3 rows)" in out


@pytest.mark.anyio
async def test_boot_seed_failed(capsys: pytest.CaptureFixture) -> None:
    async def _coro() -> None:
        raise RuntimeError("boom")

    await main._boot_seed("thing", _coro())
    out = capsys.readouterr().out
    assert "[boot] seed thing: FAILED" in out


@pytest.mark.anyio
async def test_seed_tier_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncSession.instantiations.clear()
    monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncSession", _FakeAsyncSession)
    await main._seed_tier_catalog()
    assert _FakeAsyncSession.instantiations


@pytest.mark.anyio
async def test_seed_cost_components(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _mock_session()
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    seed_cost_components = AsyncMock()
    monkeypatch.setattr("modulo.core.seed_data.cost_components.seed_cost_components", seed_cost_components)
    await main._seed_cost_components(_make_settings())
    assert seed_cost_components.await_count == 1


@pytest.mark.anyio
async def test_seed_demo_orgs(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _mock_session()
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    seed_demo_orgs = AsyncMock()
    monkeypatch.setattr("modulo.core.seed_data.demo_data.seed_demo_orgs", seed_demo_orgs)
    await main._seed_demo_orgs(_make_settings())
    assert seed_demo_orgs.await_count == 1


@pytest.mark.anyio
async def test_seed_demo_login_success(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _mock_session()
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    monkeypatch.setattr("modulo.db.seed_demo.seed_demo_runtime", AsyncMock(return_value="demo seeded"))
    result = await main._seed_demo_login(_make_settings())
    assert result == "demo seeded"


@pytest.mark.anyio
async def test_seed_demo_login_failure_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    from modulo.db.seed_demo import DemoSeedError

    session = _mock_session()
    _patch_db_seams(monkeypatch, _mock_engine(), _mock_factory(session))
    monkeypatch.setattr(
        "modulo.db.seed_demo.seed_demo_runtime",
        AsyncMock(side_effect=RuntimeError("INSERT failed [parameters: (...)]")),
    )
    with pytest.raises(DemoSeedError):
        await main._seed_demo_login(_make_settings())
