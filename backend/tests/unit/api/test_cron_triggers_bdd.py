"""Unit tests for cron trigger behaviours — API routes and scheduler fire.

Covers the behaviours described in cron.feature BDD scenarios:
create, validate, fire, spend limit, input template, event logging,
timezone support, disable."""

import datetime
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.trigger import Trigger
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.uuid4()
_NOW = datetime.datetime.now(datetime.UTC)


def _make_mock_trigger(**overrides) -> MagicMock:
    t = MagicMock(spec=Trigger)
    t.id = overrides.get("id", uuid.uuid4())
    t.pipeline_id = overrides.get("pipeline_id", _PIPELINE_ID)
    t.organisation_id = _ORG_ID
    t.trigger_type = overrides.get("trigger_type", "cron")
    t.active = overrides.get("active", True)
    t.max_concurrent_runs = overrides.get("max_concurrent_runs", 5)
    t.daily_spend_limit = overrides.get("daily_spend_limit", None)
    t.cron_expression = overrides.get("cron_expression", "0 6 * * *")
    t.cron_timezone = overrides.get("cron_timezone", "UTC")
    t.config_json = overrides.get("config_json", {})
    t.last_fired_at = overrides.get("last_fired_at", None)
    t.next_fire_at = overrides.get("next_fire_at", _NOW + datetime.timedelta(hours=1))
    t.created_by = _USER_ID
    t.created_at = _NOW
    return t


def _make_trigger_result(triggers: list[MagicMock]) -> MagicMock:
    r = MagicMock()
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=triggers)))
    r.scalar_one_or_none = MagicMock(return_value=triggers[0] if triggers else None)
    return r


def _make_mock_session(execute_result=None) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=execute_result or _make_trigger_result([]))
    return session


@pytest.fixture()
def client() -> TestClient:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    def override_settings() -> Settings:
        return Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_admin_password="testpass",
        )

    app.dependency_overrides[get_settings] = override_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, user_id=_USER_ID, org_role="admin"
    )
    return TestClient(app)


def _cleanup_client(client: TestClient) -> None:
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Scenario 1: Create cron trigger with full config
# ---------------------------------------------------------------------------


def test_create_cron_trigger_with_full_config_returns_201(client: TestClient) -> None:
    trigger = _make_mock_trigger(
        cron_expression="0 6 * * *",
        cron_timezone="America/New_York",
        config_json={"input_template": {"topic": "summary", "format": "markdown"}},
    )
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.api.routes.triggers.validate_cron_expression", return_value=None),
        patch("modulo.api.routes.triggers.compute_next_fire", return_value=_NOW + datetime.timedelta(hours=1)),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))
        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session
        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/triggers",
            json={
                "trigger_type": "cron",
                "cron_expression": "0 6 * * *",
                "cron_timezone": "America/New_York",
                "config_json": {"input_template": {"topic": "summary", "format": "markdown"}},
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["cron_expression"] == "0 6 * * *"
    assert body["cron_timezone"] == "America/New_York"
    assert body["input_template"] == {"topic": "summary", "format": "markdown"}
    assert body["next_fire_at"] is not None
    _cleanup_client(client)


# ---------------------------------------------------------------------------
# Scenario 2: Invalid cron expression is rejected
# ---------------------------------------------------------------------------


def test_create_cron_trigger_invalid_expression_returns_422(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.api.routes.triggers.validate_cron_expression", return_value="bad cron"),
    ):
        session = _make_mock_session()
        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session
        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/triggers",
            json={"trigger_type": "cron", "cron_expression": "not-a-cron"},
        )

    assert resp.status_code == 422
    assert "Invalid cron expression" in resp.json()["detail"]
    _cleanup_client(client)


# ---------------------------------------------------------------------------
# Scenario 3: Cron trigger fires and creates a run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_trigger_fires_and_creates_run() -> None:
    trigger_id = uuid.uuid4()
    run_id = uuid.uuid4()
    trigger = _make_mock_trigger(id=trigger_id, cron_expression="0 6 * * *")
    run_mock = MagicMock(id=run_id, status="pending")

    result = MagicMock()
    result.scalar_one_or_none.return_value = trigger

    event_mock = MagicMock(id=uuid.uuid4())

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=result)

    mock_factory = MagicMock(return_value=session)
    with (
        patch("modulo.core.cron_scheduler._get_engine"),
        patch("modulo.core.cron_scheduler.async_sessionmaker", return_value=mock_factory),
        patch("modulo.core.cron_scheduler._set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.cron_scheduler._count_active_runs", new_callable=AsyncMock, return_value=0),
        patch("modulo.core.cron_scheduler.create_run", new_callable=AsyncMock, return_value=run_mock),
        patch("modulo.core.cron_scheduler._log_event", new_callable=AsyncMock, return_value=event_mock),
        patch("modulo.core.cron_scheduler.compute_next_fire", return_value=_NOW + datetime.timedelta(hours=1)),
    ):
        from modulo.core.cron_scheduler import fire_cron_trigger

        result_data = await fire_cron_trigger(
            trigger_id=trigger_id,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            snapshot_id=uuid.uuid4(),
            cron_expression="0 6 * * *",
        )

    assert result_data["status"] == "fired"
    assert result_data["run_id"] == str(run_id)
    assert result_data["next_fire_at"] is not None


# ---------------------------------------------------------------------------
# Scenario 4: Daily spend limit stops trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_spend_limit_stops_trigger() -> None:
    trigger_id = uuid.uuid4()
    trigger = _make_mock_trigger(
        id=trigger_id,
        daily_spend_limit=Decimal("50.00"),
        config_json={},
    )

    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger

    cost_result = MagicMock()
    cost_result.scalar_one.return_value = Decimal("55.00")

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(side_effect=[trigger_result, cost_result])

    mock_factory = MagicMock(return_value=session)
    with (
        patch("modulo.core.cron_scheduler._get_engine"),
        patch("modulo.core.cron_scheduler.async_sessionmaker", return_value=mock_factory),
        patch("modulo.core.cron_scheduler._set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.cron_scheduler._count_active_runs", new_callable=AsyncMock, return_value=0),
        patch("modulo.core.cron_scheduler._log_event", new_callable=AsyncMock),
    ):
        from modulo.core.cron_scheduler import fire_cron_trigger

        result_data = await fire_cron_trigger(
            trigger_id=trigger_id,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            snapshot_id=uuid.uuid4(),
            cron_expression="* * * * *",
        )

    assert result_data["status"] == "skipped"
    assert result_data["reason"] == "spend_limit"


# ---------------------------------------------------------------------------
# Scenario 5: Input template populates run input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_template_populates_run_input() -> None:
    trigger_id = uuid.uuid4()
    input_template = {"channel": "#alerts", "priority": "P1"}
    trigger = _make_mock_trigger(
        id=trigger_id,
        config_json={"input_template": input_template},
    )
    run_mock = MagicMock(id=uuid.uuid4(), status="pending")

    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger

    cost_result = MagicMock()
    cost_result.scalar_one.return_value = Decimal("0")

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(side_effect=[trigger_result, cost_result])

    mock_factory = MagicMock(return_value=session)
    with (
        patch("modulo.core.cron_scheduler._get_engine"),
        patch("modulo.core.cron_scheduler.async_sessionmaker", return_value=mock_factory),
        patch("modulo.core.cron_scheduler._set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.cron_scheduler._count_active_runs", new_callable=AsyncMock, return_value=0),
        patch(
            "modulo.core.cron_scheduler.create_run",
            new_callable=AsyncMock,
            return_value=run_mock,
        ) as mock_create_run,
        patch("modulo.core.cron_scheduler._log_event", new_callable=AsyncMock),
        patch("modulo.core.cron_scheduler.compute_next_fire", return_value=_NOW + datetime.timedelta(hours=1)),
    ):
        from modulo.core.cron_scheduler import fire_cron_trigger

        await fire_cron_trigger(
            trigger_id=trigger_id,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            snapshot_id=uuid.uuid4(),
            cron_expression="* * * * *",
        )

    mock_create_run.assert_awaited_once()
    call_kwargs = mock_create_run.call_args.kwargs
    assert call_kwargs["input_payload"] == input_template


# ---------------------------------------------------------------------------
# Scenario 6: Trigger event logged on every fire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_event_logged_on_fire() -> None:
    trigger_id = uuid.uuid4()
    trigger = _make_mock_trigger(id=trigger_id, cron_expression="0 * * * *")
    run_mock = MagicMock(id=uuid.uuid4(), status="pending")
    event_mock = MagicMock(id=uuid.uuid4())

    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger

    cost_result = MagicMock()
    cost_result.scalar_one.return_value = Decimal("0")

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(side_effect=[trigger_result, cost_result])

    mock_factory = MagicMock(return_value=session)
    with (
        patch("modulo.core.cron_scheduler._get_engine"),
        patch("modulo.core.cron_scheduler.async_sessionmaker", return_value=mock_factory),
        patch("modulo.core.cron_scheduler._set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.cron_scheduler._count_active_runs", new_callable=AsyncMock, return_value=0),
        patch("modulo.core.cron_scheduler.create_run", new_callable=AsyncMock, return_value=run_mock),
        patch("modulo.core.cron_scheduler._log_event", new_callable=AsyncMock, return_value=event_mock) as mock_log,
        patch("modulo.core.cron_scheduler.compute_next_fire", return_value=_NOW + datetime.timedelta(hours=1)),
    ):
        from modulo.core.cron_scheduler import fire_cron_trigger

        result_data = await fire_cron_trigger(
            trigger_id=trigger_id,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            snapshot_id=uuid.uuid4(),
            cron_expression="0 * * * *",
        )

    assert result_data["status"] == "fired"
    mock_log.assert_awaited_once()
    call_kwargs = mock_log.call_args.kwargs
    assert call_kwargs["result"] == "accepted"
    assert call_kwargs["run_id"] == run_mock.id


# ---------------------------------------------------------------------------
# Scenario 7: Cron timezone support — API creates trigger with timezone
# ---------------------------------------------------------------------------


def test_create_cron_trigger_with_timezone_returns_201(client: TestClient) -> None:
    trigger = _make_mock_trigger(
        cron_expression="0 9 * * *",
        cron_timezone="America/New_York",
    )
    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.api.routes.triggers.validate_cron_expression", return_value=None),
        patch("modulo.api.routes.triggers.compute_next_fire", return_value=_NOW + datetime.timedelta(hours=1)),
    ):
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_make_trigger_result([trigger]))
        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session
        client.app.dependency_overrides[get_db_session] = override_session
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/triggers",
            json={
                "trigger_type": "cron",
                "cron_expression": "0 9 * * *",
                "cron_timezone": "America/New_York",
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["cron_timezone"] == "America/New_York"
    assert body["cron_expression"] == "0 9 * * *"
    _cleanup_client(client)


# ---------------------------------------------------------------------------
# Scenario 8: Disabled cron trigger does not fire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_cron_trigger_skipped() -> None:
    trigger_id = uuid.uuid4()
    trigger = _make_mock_trigger(
        id=trigger_id,
        active=False,
    )

    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=trigger_result)

    mock_factory = MagicMock(return_value=session)
    with (
        patch("modulo.core.cron_scheduler._get_engine"),
        patch("modulo.core.cron_scheduler.async_sessionmaker", return_value=mock_factory),
        patch("modulo.core.cron_scheduler._set_rls_org", new_callable=AsyncMock),
    ):
        from modulo.core.cron_scheduler import fire_cron_trigger

        result_data = await fire_cron_trigger(
            trigger_id=trigger_id,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
            snapshot_id=uuid.uuid4(),
            cron_expression="0 * * * *",
        )

    assert result_data["status"] == "skipped"
    assert result_data["reason"] == "trigger_inactive_or_missing"
