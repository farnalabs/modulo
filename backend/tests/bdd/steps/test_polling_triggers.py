"""BDD step definitions: polling triggers (PRD §8.5).

Covers the polling fire path — condition evaluation, no-match, concurrency
limit, daily spend limit, JMESPath errors, connector failures, and inactive
trigger skipping. Exercises ``fire_polling_trigger`` directly with a mocked
DB session (same pattern as ``test_agent_signal.py``).
"""

import asyncio
import contextlib
import datetime
import uuid
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

from modulo.connectors.base import ConnectorResult
from modulo.core.cron_helpers import fire_polling_trigger
from modulo.db.models.trigger import Trigger

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/triggers/polling.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ORG_MAP: dict[str, uuid.UUID] = {
    "acme": _ORG_ID,
}


def _org_id(name: str) -> uuid.UUID:
    return _ORG_MAP.get(name, _ORG_ID)


def _ctx(request: Any) -> dict[str, Any]:
    if not hasattr(request.node, "_ctx"):
        request.node._ctx = {
            "trigger": None,
            "connector_instance": None,
            "active_runs_count": 0,
            "today_cost": Decimal(0),
            "condition_expression": "[?status=='open']",
            "connector_records": [{"id": 1}],
            "connector_fail": False,
            "mock_create_run": None,
            "mock_log_event": None,
            "result": None,
        }
    return cast("dict[str, Any]", request.node._ctx)


def _make_trigger(**overrides: Any) -> MagicMock:
    t = MagicMock(spec=Trigger)
    t.id = overrides.get("id", uuid.uuid4())
    t.organisation_id = overrides.get("org_id", _ORG_ID)
    t.pipeline_id = overrides.get("pipeline_id", uuid.uuid4())
    t.trigger_type = "polling"
    t.active = overrides.get("active", True)
    t.max_concurrent_runs = overrides.get("max_concurrent_runs", 5)
    t.daily_spend_limit = overrides.get("daily_spend_limit")
    t.config_json = overrides.get(
        "config_json",
        {
            "connector_instance_id": str(uuid.uuid4()),
            "poll_query": "select * from issues",
            "condition_expression": "[?status=='open']",
            "poll_interval_seconds": 60,
            "snapshot_id": str(uuid.uuid4()),
        },
    )
    t.next_fire_at = datetime.datetime.now(datetime.UTC)
    t.last_fired_at = None
    return t


def _setup_session(
    session: MagicMock,
    trigger: MagicMock,
    connector_instance: MagicMock | None,
    active_run_count: int = 0,
    today_cost: Decimal = Decimal(0),
) -> None:
    trigger_result = MagicMock()
    trigger_result.scalar_one_or_none.return_value = trigger
    ci_result = MagicMock()
    ci_result.scalar_one_or_none.return_value = connector_instance
    count_result = MagicMock()
    count_result.scalar_one.return_value = active_run_count
    cost_result = MagicMock()
    cost_result.scalar_one.return_value = today_cost
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True
    rls_result = MagicMock()

    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind_mock)

    # The consolidated implementation (cron_helpers.fire_polling_trigger) reads
    # the org-pause state inside a savepoint — give the mock a no-op one.
    nested_cm = MagicMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)

    async def _execute(stmt, *args, **kwargs):
        stmt_str = str(stmt).lower()
        if "set_config" in stmt_str:
            return rls_result
        if "try_advisory" in stmt_str:
            return lock_result
        if "for update" in stmt_str or "from triggers" in stmt_str:
            return trigger_result
        if "connector_instance" in stmt_str:
            return ci_result
        if "count(*)" in stmt_str:
            return count_result
        if "total_cost_usd" in stmt_str or "coalesce" in stmt_str:
            return cost_result
        if "update" in stmt_str:
            return count_result
        return rls_result

    session.execute = _execute


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(parsers.parse('org "{org}" has pipeline "{name}" with polling config'))
def _given_polling_config(org: str, name: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["trigger"] = _make_trigger(org_id=_org_id(org))
    ctx["connector_instance"] = MagicMock()
    ctx["connector_records"] = [{"status": "open"}]


@given(parsers.parse('the connector returns records matching "{expr}"'))
def _given_connector_records_matching(expr: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["connector_records"] = [{"status": "open"}]
    ctx["condition_expression"] = expr


@given("the connector returns no matching records")
def _given_connector_no_records(request: Any) -> None:
    ctx = _ctx(request)
    ctx["connector_records"] = []


@given(parsers.parse("the pipeline has {count:d} active runs"))
def _given_active_runs(count: int, request: Any) -> None:
    ctx = _ctx(request)
    ctx["active_runs_count"] = count


@given(parsers.parse("the trigger max_concurrent_runs is {limit:d}"))
def _given_max_concurrent(limit: int, request: Any) -> None:
    ctx = _ctx(request)
    ctx["trigger"].max_concurrent_runs = limit


@given(parsers.parse("the trigger has a daily spend limit of {limit}"))
def _given_daily_spend_limit(limit: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["trigger"].daily_spend_limit = Decimal(limit)


@given(parsers.parse("the pipeline has accumulated {cost} in run costs today"))
def _given_accumulated_cost(cost: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["today_cost"] = Decimal(cost)


@given(parsers.parse('the condition_expression is "{expr}"'))
def _given_condition_expression(expr: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["condition_expression"] = expr
    ctx["trigger"].config_json["condition_expression"] = expr


@given("the connector query fails")
def _given_connector_fails(request: Any) -> None:
    ctx = _ctx(request)
    ctx["connector_fail"] = True


@given("the trigger is deactivated")
def _given_trigger_inactive(request: Any) -> None:
    ctx = _ctx(request)
    ctx["trigger"].active = False


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


def _fire_polling_trigger(ctx: dict[str, Any]) -> dict[str, Any]:
    trigger = ctx["trigger"]
    connector_instance = ctx["connector_instance"]

    session = MagicMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()
    session.flush = AsyncMock()
    _setup_session(
        session,
        trigger,
        connector_instance,
        ctx["active_runs_count"],
        today_cost=ctx["today_cost"],
    )

    factory = MagicMock()
    factory.return_value = session
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    run_mock = MagicMock()
    run_mock.id = uuid.uuid4()

    connector = AsyncMock()
    if ctx["connector_fail"]:
        connector.query.side_effect = RuntimeError("API timeout")
    else:
        connector.query.return_value = ConnectorResult(
            records=ctx["connector_records"],
            total=len(ctx["connector_records"]),
        )

    with (
        patch("modulo.core.cron_helpers.get_settings") as mock_settings,
        patch("modulo.core.cron_helpers._open_factory", return_value=factory),
        patch("modulo.core.cron_helpers._set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.cron_helpers.org_is_paused", new_callable=AsyncMock, return_value=False),
        patch("modulo.core.secrets_backend.create_secrets_backend") as mock_backend,
        patch("modulo.core.trigger_engine.polling._build_polling_connector", return_value=connector),
        patch(
            "modulo.db.crud.run.create_run",
            new_callable=AsyncMock,
            return_value=run_mock,
        ) as mock_cr,
        patch("modulo.core.cron_helpers._log_poll_event", new_callable=AsyncMock) as mock_event,
    ):
        mock_settings.return_value = MagicMock(
            database_url="postgresql+asyncpg://localhost/test",
            fernet_key="a" * 32,
            modulo_secrets_backend="fernet",
        )
        backend = AsyncMock()
        backend.get_secret.return_value = '{"token": "test-token"}'
        mock_backend.return_value = backend
        mock_event.return_value = MagicMock(id=uuid.uuid4())

        ctx["mock_create_run"] = mock_cr
        ctx["mock_log_event"] = mock_event
        ctx["mock_connector"] = connector

        ctx["result"] = asyncio.run(
            fire_polling_trigger(
                trigger_id=trigger.id,
                org_id=trigger.organisation_id,
                pipeline_id=trigger.pipeline_id,
                connector_instance_id=uuid.UUID(trigger.config_json["connector_instance_id"]),
                poll_query=trigger.config_json["poll_query"],
                condition_expression=ctx["condition_expression"],
            )
        )


@when(parsers.parse("the polling scheduler runs and evaluates the condition"))
def _when_polling_scheduler_runs_with_condition(request: Any) -> None:
    _fire_polling_trigger(_ctx(request))


@when("the polling scheduler runs")
def _when_polling_scheduler_runs(request: Any) -> None:
    _fire_polling_trigger(_ctx(request))


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then(parsers.parse('a Run is created with trigger_type "{ttype}"'))
def _then_run_created(ttype: str, request: Any) -> None:
    ctx = _ctx(request)
    assert ctx["result"]["status"] == "fired", f"Expected fired, got {ctx['result']}"
    assert ctx["result"].get("run_id") is not None
    mock_cr = ctx["mock_create_run"]
    assert mock_cr.await_count == 1
    assert mock_cr.call_args[1]["trigger_type"] == ttype


@then("no Run is created")
def _then_no_run_created(request: Any) -> None:
    ctx = _ctx(request)
    mock_cr = ctx.get("mock_create_run")
    if mock_cr is not None:
        mock_cr.assert_not_called()


@then("the run references the polling trigger")
def _then_run_references_trigger(request: Any) -> None:
    ctx = _ctx(request)
    mock_cr = ctx.get("mock_create_run")
    assert mock_cr is not None
    assert mock_cr.call_args[1]["trigger_type"] == "polling"


@then(parsers.parse('a TriggerEvent is created with result "{result}"'))
def _then_trigger_event_result(result: str, request: Any) -> None:
    ctx = _ctx(request)
    mock_event = ctx.get("mock_log_event")
    assert mock_event is not None, "No _log_poll_event mock recorded"
    assert mock_event.await_count >= 1
    assert mock_event.call_args.kwargs["result"] == result


@then(parsers.parse('the error_detail mentions "{msg}"'))
def _then_error_detail_mentions(msg: str, request: Any) -> None:
    ctx = _ctx(request)
    mock_event = ctx.get("mock_log_event")
    assert mock_event is not None
    detail = mock_event.call_args.kwargs.get("error_detail", "")
    assert msg.lower() in detail.lower(), f"error_detail does not mention '{msg}': {detail}"


@then(parsers.parse('the trigger is skipped with reason "{reason}"'))
def _then_trigger_skipped(reason: str, request: Any) -> None:
    ctx = _ctx(request)
    result = ctx["result"]
    assert result["status"] == "skipped", f"Expected skipped, got {result}"
    assert result.get("reason") == reason, f"Expected reason {reason}, got {result.get('reason')}"
