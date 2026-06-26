"""Integration tests for polling trigger — poll-to-run cycle with real Postgres.

Uses testcontainers Postgres + Alembic migrations. Mocks connector and
secrets backend to avoid external dependencies.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.connectors.base import ConnectorResult
from modulo.core.trigger_engine.polling import _fire_polling_trigger
from modulo.db.models.run import Run
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.rls import set_rls_org

_POLL_PKG = "modulo.core.trigger_engine.polling"

# Share the session-scoped Postgres container from conftest
pytestmark = pytest.mark.integration

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TRIGGER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_CI_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000006")
_VALID_32 = "a" * 32


@pytest_asyncio.fixture
async def seeded_db(db_session: AsyncSession) -> AsyncSession:
    """Seed minimal entities needed for a polling trigger test."""
    # Insert organisation
    await db_session.execute(
        text(
            "INSERT INTO organisations (id, name, slug, settings_json) "
            "VALUES (:id, :name, :slug, '{}'::json) ON CONFLICT (id) DO NOTHING"
        ),
        {"id": str(_ORG_ID), "name": "Integration Org", "slug": "int-org"},
    )
    # Insert user
    await db_session.execute(
        text(
            "INSERT INTO users (id, organisation_id, email, display_name, "
            "org_role, auth_provider, active, password_hash) "
            "VALUES (:id, :oid, :email, :name, 'admin', 'local', true, 'hash') "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": str(_USER_ID),
            "oid": str(_ORG_ID),
            "email": "admin@int.org",
            "name": "Integration Admin",
        },
    )
    # Insert pipeline
    await db_session.execute(
        text(
            "INSERT INTO pipelines (id, organisation_id, name, created_by, "
            "visibility, run_context_defaults) "
            "VALUES (:id, :oid, :name, :uid, 'org', '{}'::json) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": str(_PIPELINE_ID),
            "oid": str(_ORG_ID),
            "name": "Polling Pipeline",
            "uid": str(_USER_ID),
        },
    )
    # Insert snapshot
    await db_session.execute(
        text(
            "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
            "snapshot_version, graph_json, connector_bindings_json, "
            "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
            "run_context_defaults) "
            "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
            "'[]'::json, '[]'::json, '[]'::json, '{}'::json) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": str(_SNAPSHOT_ID),
            "pid": str(_PIPELINE_ID),
            "oid": str(_ORG_ID),
        },
    )
    # Insert connector instance
    await db_session.execute(
        text(
            "INSERT INTO connector_instances (id, organisation_id, owner_id, "
            "connector_type_id, name, config_json, visibility, "
            "allowed_operations, credentials_ciphertext) "
            "VALUES (:id, :oid, :uid, 'stub', 'Stub CI', "
            "'{}'::json, 'org', ARRAY['query']::text[], 'ciphertext') "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": str(_CI_ID),
            "oid": str(_ORG_ID),
            "uid": str(_USER_ID),
        },
    )

    await db_session.commit()
    return db_session


@pytest_asyncio.fixture
async def polling_trigger(seeded_db: AsyncSession) -> dict[str, Any]:
    """Insert a polling trigger row and return its config."""
    await seeded_db.execute(
        text(
            "INSERT INTO triggers (id, organisation_id, pipeline_id, "
            "trigger_type, active, max_concurrent_runs, config_json, "
            "created_by, cron_expression, cron_timezone, "
            "last_fired_at, next_fire_at) "
            "VALUES (:id, :oid, :pid, 'polling', true, 5, "
            "(:config)::json, :uid, NULL, NULL, NULL, "
            "NOW() - INTERVAL '1 minute') "
            "ON CONFLICT (id) DO UPDATE SET next_fire_at = NOW() - INTERVAL '1 minute'"
        ),
        {
            "id": str(_TRIGGER_ID),
            "oid": str(_ORG_ID),
            "pid": str(_PIPELINE_ID),
            "uid": str(_USER_ID),
            "config": _make_polling_config_json(),
        },
    )
    await seeded_db.commit()
    return {
        "trigger_id": _TRIGGER_ID,
        "org_id": _ORG_ID,
        "pipeline_id": _PIPELINE_ID,
        "connector_instance_id": _CI_ID,
        "snapshot_id": _SNAPSHOT_ID,
    }


def _make_polling_config_json() -> str:
    return (
        '{"connector_instance_id": "' + str(_CI_ID)
        + '", "poll_query": "select * from issues", '
        + '"condition_expression": "[?status==`open`]", '
        + '"poll_interval_seconds": 60, '
        + '"snapshot_id": "' + str(_SNAPSHOT_ID) + '"}'
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_polling_trigger_happy_path(
    db_url: str,
    polling_trigger: dict[str, Any],
) -> None:
    """Verify poll-to-run: condition met → run created + TriggerEvent logged."""
    settings_mock = MagicMock()
    settings_mock.database_url = db_url
    settings_mock.fernet_key = _VALID_32
    settings_mock.modulo_secrets_backend = "fernet"

    connector_mock = AsyncMock()
    connector_mock.query.return_value = ConnectorResult(
        records=[{"issue": {"number": 1, "title": "Bug"}}],
        total=1,
    )

    secrets_backend_mock = AsyncMock()
    secrets_backend_mock.get_secret.return_value = '{"token": "test"}'

    with (
        patch(f"{_POLL_PKG}.get_settings", return_value=settings_mock),
        patch(f"{_POLL_PKG}._build_polling_connector", return_value=connector_mock),
        patch(f"{_POLL_PKG}.create_secrets_backend", return_value=secrets_backend_mock),
    ):
        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="select * from issues",
            condition_expression="[?status==`open`]",
        )

    assert result["status"] == "fired"
    assert "run_id" in result
    run_id = uuid.UUID(result["run_id"])

    # Query the DB to verify the run exists
    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            await set_rls_org(session, _ORG_ID)

            run_result = await session.execute(
                select(Run).where(Run.id == run_id)
            )
            run = run_result.scalar_one_or_none()
            assert run is not None, f"Run {run_id} not found in DB"
            assert run.trigger_type == "polling"
            assert run.organisation_id == _ORG_ID
            assert run.pipeline_id == _PIPELINE_ID

            event_result = await session.execute(
                select(TriggerEvent).where(
                    TriggerEvent.trigger_id == _TRIGGER_ID,
                    TriggerEvent.run_id == run_id,
                )
            )
            event = event_result.scalar_one_or_none()
            assert event is not None, "TriggerEvent not found"
            assert event.validation_result == "condition_met"
            assert event.trigger_type == "polling"
            assert event.run_id == run_id

            trigger_result = await session.execute(
                select(Trigger).where(Trigger.id == _TRIGGER_ID)
            )
            trigger = trigger_result.scalar_one_or_none()
            assert trigger is not None
            assert trigger.last_fired_at is not None

    await engine.dispose()


@pytest.mark.integration
async def test_polling_trigger_no_match(
    db_url: str,
    polling_trigger: dict[str, Any],
) -> None:
    """Verify condition no_match → no run created, no_match event logged."""
    settings_mock = MagicMock()
    settings_mock.database_url = db_url
    settings_mock.fernet_key = _VALID_32
    settings_mock.modulo_secrets_backend = "fernet"

    connector_mock = AsyncMock()
    connector_mock.query.return_value = ConnectorResult(
        records=[],
        total=0,
    )

    secrets_backend_mock = AsyncMock()
    secrets_backend_mock.get_secret.return_value = '{"token": "test"}'

    with (
        patch(f"{_POLL_PKG}.get_settings", return_value=settings_mock),
        patch(f"{_POLL_PKG}._build_polling_connector", return_value=connector_mock),
        patch(f"{_POLL_PKG}.create_secrets_backend", return_value=secrets_backend_mock),
    ):
        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="select * from issues",
            condition_expression="[?status==`nonexistent`]",
        )

    assert result["status"] == "no_match"
    assert "run_id" not in result

    # Verify no run was created and a no_match event was logged
    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            await set_rls_org(session, _ORG_ID)

            runs_result = await session.execute(
                select(Run).where(Run.trigger_id == _TRIGGER_ID)
            )
            runs = runs_result.scalars().all()
            assert len(runs) == 0

            event_result = await session.execute(
                select(TriggerEvent).where(
                    TriggerEvent.trigger_id == _TRIGGER_ID,
                    TriggerEvent.validation_result == "no_match",
                )
            )
            event = event_result.scalar_one_or_none()
            assert event is not None, "Expected no_match TriggerEvent"
            assert event.run_id is None

    await engine.dispose()


@pytest.mark.integration
async def test_polling_trigger_concurrency_limit(
    db_url: str,
    polling_trigger: dict[str, Any],
) -> None:
    """Verify max_concurrent_runs is respected."""
    settings_mock = MagicMock()
    settings_mock.database_url = db_url
    settings_mock.fernet_key = _VALID_32
    settings_mock.modulo_secrets_backend = "fernet"

    # Set max_concurrent_runs to 2 and create 2 already-active runs
    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            await set_rls_org(session, _ORG_ID)
            await session.execute(
                text(
                    "UPDATE triggers SET max_concurrent_runs = 2 "
                    "WHERE id = :id"
                ),
                {"id": str(_TRIGGER_ID)},
            )
            for i in range(2):
                await session.execute(
                    text(
                        "INSERT INTO runs (id, organisation_id, pipeline_id, "
                        "snapshot_id, trigger_type, status, "
                        "input_hash, langgraph_thread_id, trigger_id) "
                        "VALUES (:rid, :oid, :pid, :sid, 'polling', 'running', "
                        "'abcd1234', :thread_id, :tid)"
                    ),
                    {
                        "rid": str(uuid.uuid4()),
                        "oid": str(_ORG_ID),
                        "pid": str(_PIPELINE_ID),
                        "sid": str(_SNAPSHOT_ID),
                        "thread_id": f"{_ORG_ID}:run-{i}",
                        "tid": str(_TRIGGER_ID),
                    },
                )
        await session.commit()

    secrets_backend_mock = AsyncMock()
    secrets_backend_mock.get_secret.return_value = '{"token": "test"}'

    with (
        patch(f"{_POLL_PKG}.get_settings", return_value=settings_mock),
        patch(f"{_POLL_PKG}.create_secrets_backend", return_value=secrets_backend_mock),
    ):
        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="select * from issues",
            condition_expression="[?status==`open`]",
        )

    assert result["status"] == "skipped"
    assert result["reason"] == "concurrency_limit"
    assert result["active_runs"] == 2

    # Verify a concurrency_limit_reached event was logged
    async with factory() as session:
        async with session.begin():
            await set_rls_org(session, _ORG_ID)
            event_result = await session.execute(
                select(TriggerEvent).where(
                    TriggerEvent.trigger_id == _TRIGGER_ID,
                    TriggerEvent.validation_result == "concurrency_limit_reached",
                )
            )
            event = event_result.scalar_one_or_none()
            assert event is not None, "Expected concurrency_limit_reached TriggerEvent"

    await engine.dispose()


@pytest.mark.integration
async def test_polling_trigger_connector_not_found(
    db_url: str,
    polling_trigger: dict[str, Any],
) -> None:
    """Verify missing connector_instance logs poll_error."""
    settings_mock = MagicMock()
    settings_mock.database_url = db_url
    settings_mock.fernet_key = _VALID_32
    settings_mock.modulo_secrets_backend = "fernet"

    with (
        patch(f"{_POLL_PKG}.get_settings", return_value=settings_mock),
    ):
        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=uuid.uuid4(),  # nonexistent CI
            poll_query="select * from issues",
            condition_expression="[?status==`open`]",
        )

    assert result["status"] == "error"
    assert result["reason"] == "connector_not_found"


@pytest.mark.integration
async def test_polling_trigger_inactive(
    db_url: str,
    polling_trigger: dict[str, Any],
) -> None:
    """Verify inactive trigger is skipped."""
    settings_mock = MagicMock()
    settings_mock.database_url = db_url
    settings_mock.fernet_key = _VALID_32
    settings_mock.modulo_secrets_backend = "fernet"

    # Deactivate the trigger
    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            await set_rls_org(session, _ORG_ID)
            await session.execute(
                text("UPDATE triggers SET active = false WHERE id = :id"),
                {"id": str(_TRIGGER_ID)},
            )
        await session.commit()

    with patch(f"{_POLL_PKG}.get_settings", return_value=settings_mock):
        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="select * from issues",
            condition_expression="[?status==`open`]",
        )

    assert result["status"] == "skipped"
    assert result["reason"] == "trigger_inactive_or_missing"

    await engine.dispose()


@pytest.mark.integration
async def test_polling_trigger_condition_eval_failure(
    db_url: str,
    polling_trigger: dict[str, Any],
) -> None:
    """Verify invalid JMESPath logs poll_error."""
    settings_mock = MagicMock()
    settings_mock.database_url = db_url
    settings_mock.fernet_key = _VALID_32
    settings_mock.modulo_secrets_backend = "fernet"

    connector_mock = AsyncMock()
    connector_mock.query.return_value = ConnectorResult(
        records=[{"issue": {"number": 1}}],
        total=1,
    )
    secrets_backend_mock = AsyncMock()
    secrets_backend_mock.get_secret.return_value = '{"token": "test"}'

    with (
        patch(f"{_POLL_PKG}.get_settings", return_value=settings_mock),
        patch(f"{_POLL_PKG}._build_polling_connector", return_value=connector_mock),
        patch(f"{_POLL_PKG}.create_secrets_backend", return_value=secrets_backend_mock),
    ):
        result = await _fire_polling_trigger(
            trigger_id=_TRIGGER_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            connector_instance_id=_CI_ID,
            poll_query="select * from issues",
            condition_expression="[invalid: syntax",
        )

    assert result["status"] == "error"
    assert result["reason"] == "condition_eval_failed"
