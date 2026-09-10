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

        async def _seed(instances: list[ConnectorInstance]) -> None:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session, session.begin():
                for ci in instances:
                    await session.merge(ci)

        async def _rows() -> list[ConnectorInstance]:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                result = await session.execute(select(ConnectorInstance).order_by(ConnectorInstance.name))
                return list(result.scalars().all())

        yield _run, _seed, _rows
    await engine.dispose()


async def test_sweep_stamps_last_check_on_every_active_instance(sweep, tmp_path) -> None:
    run, seed, rows = sweep
    await seed([_instance(connector_type_id="filesystem", config_json={"base_path": str(tmp_path)})])

    result = await run()

    assert result["checked"] == 1
    assert result["healthy"] == 1
    (row,) = await rows()
    assert row.last_health_check_at is not None
    assert row.last_health_check_error is None


async def test_sweep_isolates_poisoned_instance(sweep, tmp_path) -> None:
    run, seed, rows = sweep
    await seed(
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


async def test_sweep_skips_disabled_instances(sweep) -> None:
    run, seed, rows = sweep
    await seed([_instance(connector_type_id="shell", status="disabled")])

    result = await run()

    assert result["checked"] == 0
    (row,) = await rows()
    assert row.last_health_check_at is None


async def test_sweep_does_not_mutate_org_data(sweep, tmp_path) -> None:
    run, seed, rows = sweep
    original = _instance(connector_type_id="filesystem", config_json={"base_path": str(tmp_path)})
    await seed([original])
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
