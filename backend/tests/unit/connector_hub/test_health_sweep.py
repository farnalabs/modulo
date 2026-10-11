"""Unit tests for the cross-org connector health-check sweep (FAR-699).

Covers the acceptance criteria on the ticket:

* a real sweep run stamps ``last_health_check_at`` on every active instance
  (the "never"-forever bug) and records per-instance failure detail;
* one poisoned instance is isolated — the rest of the sweep still runs;
* the sweep NEVER mutates org data (only the two health columns change);
* disabled instances are skipped entirely.
"""

import uuid
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import modulo.core.connector_hub.health_sweep as health_sweep
from modulo.core.connector_hub.health_sweep import run_connector_health_checks
from modulo.db.models.connector_instance import ConnectorInstance

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_ORG = uuid.uuid4()
_ACCOUNT = uuid.uuid4()


class _FakeSecretsBackend:
    """Secrets backend stand-in — serves deterministic dummy credentials."""

    def __init__(self, session: AsyncSession) -> None:  # pragma: no cover - trivial
        pass

    async def get_secret(self, name: str) -> Any:
        return "{}"

    async def close(self) -> None:  # pragma: no cover - trivial
        pass


def _fake_backend_factory(*, session: AsyncSession, fernet_key: str | None = None) -> _FakeSecretsBackend:
    return _FakeSecretsBackend(session)


def _instance(**overrides: Any) -> ConnectorInstance:
    connector_type_id = overrides.pop("connector_type_id")
    ci = ConnectorInstance(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        account_id=_ACCOUNT,
        name=connector_type_id,
        connector_type_id=connector_type_id,
        config_json=overrides.pop("config_json", {}),
        visibility=overrides.pop("visibility", "org"),
        allowed_operations=overrides.pop("allowed_operations", ["read"]),
        **overrides,
    )
    ci.credentials_ciphertext = b"{}"
    return ci


@pytest.fixture
async def sweep(tmp_path):
    engine: AsyncEngine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sweep.db'}", echo=False)

    from modulo.db.models.base import Base

    # Only the table under test — creating ALL Base tables on SQLite pulls in
    # Postgres-only column types across the model graph.
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[ConnectorInstance.__table__]))
    with patch.object(health_sweep, "create_secrets_backend", _fake_backend_factory):

        async def _run() -> dict[str, Any]:
            return await run_connector_health_checks(
                async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
            )

        async def _seeder(instances: list[ConnectorInstance]) -> None:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session, session.begin():
                for ci in instances:
                    await session.merge(ci)

        async def _rows() -> list[ConnectorInstance]:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                result = await session.execute(select(ConnectorInstance).order_by(ConnectorInstance.name))
                return list(result.scalars().all())

        yield _run, _seeder, _rows
    await engine.dispose()


async def test_sweep_stamps_last_check_on_every_active_instance(sweep, tmp_path) -> None:
    run, seeder, rows = sweep
    await seeder([_instance(connector_type_id="filesystem", config_json={"base_path": str(tmp_path)})])

    result = await run()

    assert result["checked"] == 1
    assert result["healthy"] == 1
    (row,) = await rows()
    assert row.last_health_check_at is not None
    assert row.last_health_check_error is None


async def test_sweep_isolates_poisoned_instance(sweep, tmp_path) -> None:
    run, seeder, rows = sweep
    await seeder(
        [
            _instance(connector_type_id="filesystem", config_json={"base_path": str(tmp_path)}),
            _instance(connector_type_id="shell"),
            _instance(connector_type_id="no-such-connector-type"),
        ]
    )

    result = await run()

    assert result["checked"] == 3
    assert result["healthy"] == 1
    assert result["unhealthy"] == 2
    by_type = {row.connector_type_id: row for row in await rows()}
    assert by_type["filesystem"].last_health_check_at is not None
    assert by_type["filesystem"].last_health_check_error is None
    assert by_type["shell"].last_health_check_at is not None
    assert by_type["shell"].last_health_check_error
    assert by_type["no-such-connector-type"].last_health_check_at is not None
    assert by_type["no-such-connector-type"].last_health_check_error


async def test_sweep_isolates_exploding_instance(sweep, tmp_path, monkeypatch) -> None:
    """A connector whose check raises is recorded on its own row; the sweep continues."""
    run, seeder, rows = sweep
    real = health_sweep._check_instance
    calls = {"n": 0}

    async def _flaky(ci, *, session, fernet_key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return await real(ci, session=session, fernet_key=fernet_key)

    monkeypatch.setattr(health_sweep, "_check_instance", _flaky)
    await seeder(
        [
            _instance(connector_type_id="filesystem", config_json={"base_path": str(tmp_path)}),
            _instance(connector_type_id="filesystem", config_json={"base_path": str(tmp_path)}),
        ]
    )

    result = await run()

    assert result["checked"] == 2
    assert result["healthy"] == 1
    assert result["unhealthy"] == 1
    errors = [row.last_health_check_error for row in await rows()]
    # FAR-1651Fix3: the residual catch persists a fail-closed placeholder, not
    # the raw exception message — the full text goes to the log, never to the
    # operator-visible column. The exception TYPE is still triageable.
    assert any(e is not None and "RuntimeError" in e and "boom" not in e for e in errors)
    assert any(e is None for e in errors)


async def test_sweep_skips_disabled_instances(sweep) -> None:
    run, seeder, rows = sweep
    await seeder([_instance(connector_type_id="shell", status="disabled")])

    result = await run()

    assert result["checked"] == 0
    (row,) = await rows()
    assert row.last_health_check_at is None


async def test_sweep_does_not_mutate_org_data(sweep, tmp_path) -> None:
    run, seeder, rows = sweep
    original = _instance(connector_type_id="filesystem", config_json={"base_path": str(tmp_path)})
    await seeder([original])
    (seeded,) = await rows()
    snapshot: dict[str, Any] = {
        "name": seeded.name,
        "connector_type_id": seeded.connector_type_id,
        "config_json": seeded.config_json,
        "allowed_operations": seeded.allowed_operations,
        "status": seeded.status,
        "visibility": seeded.visibility,
        "credentials_ciphertext": seeded.credentials_ciphertext,
        "tier": seeded.tier,
        "degraded_at": seeded.degraded_at,
        "last_skip_error": seeded.last_skip_error,
    }

    await run()

    (row,) = await rows()
    for field, value in snapshot.items():
        assert getattr(row, field) == value, field
    # Only the two health columns ever move.
    assert row.last_health_check_at is not None
    assert row.last_health_check_error is None


async def test_sweep_binds_org_context_for_real_secrets_backend(tmp_path) -> None:
    """FAR-1526: the system session factory carries NO org context.

    The prod sweep runs on ``modulo_system`` (BYPASSRLS, never ``set_rls_org``),
    so ``FernetSecretsBackend.get_secret`` used to raise ``RuntimeError:
    FernetSecretsBackend: RLS organisation context not set`` for EVERY instance,
    the hub skipped all of them, and the sweep recorded ``ConnectorNotFoundError``
    for the whole fleet (observed on all 5 prod connectors, 2026-10-06).

    This exercises the REAL secrets backend (nothing patched): without the
    per-org ``set_rls_org`` binding in ``_check_instance`` the instance is
    skipped and ``healthy`` stays 0.
    """
    from cryptography.fernet import Fernet

    from modulo.db.models.secret import Secret

    engine: AsyncEngine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'real_secrets.db'}", echo=False)

    from modulo.db.models.base import Base

    tables = [ConnectorInstance.__table__, Secret.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))

    instance = _instance(connector_type_id="filesystem", config_json={"base_path": str(tmp_path)})
    fernet_key = Fernet.generate_key().decode()

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        session.add(
            Secret(
                id=uuid.uuid4(),
                organisation_id=_ORG,
                key=str(instance.id),
                encrypted_value=Fernet(fernet_key.encode()).encrypt(b"{}"),
            )
        )
        await session.merge(instance)

    factory = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    try:
        result = await run_connector_health_checks(factory, fernet_key=fernet_key)

        assert result["checked"] == 1
        assert result["healthy"] == 1, result
        assert result["unhealthy"] == 0, result

        async with maker() as read_session:
            rows = (await read_session.execute(select(ConnectorInstance))).scalars().all()
            (row,) = rows
            assert row.last_health_check_at is not None
            assert row.last_health_check_error is None
    finally:
        await engine.dispose()


async def test_sweep_records_the_skip_reason_not_a_bare_not_found(sweep) -> None:
    """FAR-1526: a skipped instance records the hub's skip reason.

    Previously the sweep recorded ``ConnectorNotFoundError: 'Connector not
    found: <uuid>'`` for every failure — the real cause (e.g. an undecryptable
    secret or an unknown connector type) was only visible in the worker log.
    """
    run, seeder, rows = sweep
    await seeder([_instance(connector_type_id="no-such-connector-type")])

    result = await run()

    assert result["unhealthy"] == 1
    (row,) = await rows()
    error = row.last_health_check_error
    assert error
    assert not error.startswith("ConnectorNotFoundError"), error


async def test_sweep_acl_denial_is_not_a_health_failure(sweep, tmp_path) -> None:
    """FAR-1564: an ACL denial from ``health_check()`` is a PERMISSION answer,
    not a connector health failure.

    ``_TracedConnector.health_check`` enforces the ``read`` operation, so a
    connector whose non-empty allowlist omits ``read`` raises
    ``ConnectorPermissionError`` BEFORE the probe runs. The broad per-instance
    handler used to record that as ``last_health_check_error`` and count the
    instance unhealthy — marking a perfectly healthy connector broken. The
    sweep now skips it distinctly: no health columns written (the probe never
    ran), counted ``skipped``, never ``unhealthy``.
    """
    run, seeder, rows = sweep
    await seeder(
        [
            _instance(
                connector_type_id="filesystem",
                config_json={"base_path": str(tmp_path)},
                allowed_operations=["write"],  # non-empty allowlist WITHOUT read
            ),
        ]
    )

    result = await run()

    assert result["checked"] == 1
    assert result["healthy"] == 0
    assert result["unhealthy"] == 0
    assert result["skipped"] == 1
    (row,) = await rows()
    assert row.last_health_check_at is None
    assert row.last_health_check_error is None
