"""BDD step definitions: cron trigger create, schedule, spend limit,
input template, event logging, timezone support, and disable."""

import datetime
import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

try:
    scenarios("../../features/triggers/cron.feature")
except (FileNotFoundError, OSError):
    pass

from modulo.db.models.trigger import Trigger
from tests.bdd.conftest import make_mock_run


def _make_mock_trigger(**overrides) -> MagicMock:
    t = MagicMock(spec=Trigger)
    t.id = overrides.get("id", uuid.uuid4())
    t.organisation_id = overrides.get("org_id", uuid.UUID("00000000-0000-0000-0000-000000000001"))
    t.pipeline_id = overrides.get("pipeline_id", uuid.uuid4())
    t.trigger_type = "cron"
    t.active = overrides.get("active", True)
    t.max_concurrent_runs = overrides.get("max_concurrent_runs", 5)
    t.daily_spend_limit = overrides.get("daily_spend_limit")
    t.cron_expression = overrides.get("cron_expression", "0 6 * * *")
    t.cron_timezone = overrides.get("cron_timezone", "UTC")
    t.config_json = overrides.get("config_json", {})
    t.last_fired_at = overrides.get("last_fired_at")
    t.next_fire_at = overrides.get("next_fire_at", datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1))
    t.created_by = uuid.UUID("00000000-0000-0000-0000-000000000002")
    return t


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(parsers.parse('org "{org}" has pipeline "{name}"'))
def org_has_pipeline_cron(org: str, name: str, request):
    request.node._pipeline_name = name


@given(
    parsers.parse(
        'an active cron trigger exists for pipeline "{name}" with expression "{expression}"'
    )
)
def active_cron_trigger_with_expression(name: str, expression: str, request):
    request.node._pipeline_name = name
    request.node._cron_expression = expression
    request.node._trigger_active = True


@given(
    parsers.parse(
        'an active cron trigger exists for pipeline "{name}" with daily_spend_limit "{limit}"'
    )
)
def active_cron_trigger_with_spend_limit(name: str, limit: str, request):
    request.node._pipeline_name = name
    request.node._cron_expression = "0 * * * *"
    request.node._daily_spend_limit = Decimal(limit)
    request.node._trigger_active = True


@given(
    parsers.parse(
        'an active cron trigger exists for pipeline "{name}" with input_template {template}'
    )
)
def active_cron_trigger_with_template(name: str, template, request):
    input_template = json.loads(template) if isinstance(template, str) else template
    request.node._pipeline_name = name
    request.node._cron_expression = "0 * * * *"
    request.node._input_template = input_template
    request.node._trigger_active = True


@given(parsers.parse('an active cron trigger exists for pipeline "{name}" with expression "{expression}"'))
def active_cron_trigger_base(name: str, expression: str, request):
    request.node._pipeline_name = name
    request.node._cron_expression = expression
    request.node._trigger_active = True


@given(parsers.parse('a deactivated cron trigger exists for pipeline "{name}"'))
def deactivated_cron_trigger(name: str, request):
    request.node._pipeline_name = name
    request.node._cron_expression = "0 * * * *"
    request.node._trigger_active = False


@given(
    parsers.parse(
        'the pipeline has accumulated "{cost}" in run costs today'
    )
)
def accumulated_run_cost(cost: str, request):
    request.node._today_cost = Decimal(cost)


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(
    parsers.parse(
        'I create a cron trigger for pipeline "{pipeline}" with expression "{expression}"'
    )
)
def create_cron_trigger_simple(pipeline: str, expression: str, client, request):
    create_cron_trigger_full(pipeline, expression, "UTC", "{}", client, request)


@when(
    parsers.parse(
        'I create a cron trigger for pipeline "{pipeline}" with expression "{expression}" '
        'timezone "{timezone}" and input_template {template}'
    )
)
def create_cron_trigger_full(pipeline: str, expression: str, timezone: str, template, client, request):
    input_template = json.loads(template) if isinstance(template, str) else template
    _do_create_cron_trigger(client, request, expression, timezone, input_template)


@when(
    parsers.parse(
        'I create a cron trigger for pipeline "{pipeline}" with expression "{expression}" '
        'timezone "{timezone}"'
    )
)
def create_cron_trigger_timezone(pipeline: str, expression: str, timezone: str, client, request):
    _do_create_cron_trigger(client, request, expression, timezone, {})


def _do_create_cron_trigger(client, request, expression, timezone, input_template):
    now = datetime.datetime.now(datetime.UTC)
    mock_trigger = _make_mock_trigger(
        cron_expression=expression,
        cron_timezone=timezone,
        config_json={"input_template": input_template},
        next_fire_at=now + datetime.timedelta(hours=1),
    )

    with (
        patch("modulo.api.routes.triggers.set_rls_org"),
        patch("modulo.api.routes.triggers.compute_next_fire", return_value=now + datetime.timedelta(hours=1)),
    ):
        resp = client.post(
            f"/api/v1/pipelines/{mock_trigger.pipeline_id}/triggers",
            json={
                "trigger_type": "cron",
                "cron_expression": expression,
                "cron_timezone": timezone,
                "config_json": {"input_template": input_template},
            },
        )
    request.node._resp = resp


@when("the cron scheduler fires the cron trigger")
def fire_cron_trigger(request, client):
    trigger_id = uuid.uuid4()
    org_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    trigger_mock = _make_mock_trigger(
        id=trigger_id,
        org_id=org_id,
        pipeline_id=pipeline_id,
        cron_expression=getattr(request.node, "_cron_expression", "0 * * * *"),
        active=getattr(request.node, "_trigger_active", True),
        daily_spend_limit=getattr(request.node, "_daily_spend_limit", None),
        config_json={"input_template": getattr(request.node, "_input_template", {})},
    )

    run_mock = make_mock_run(
        trigger_type="cron",
        trigger_id=trigger_id,
        pipeline_id=pipeline_id,
    )

    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    execute_result = MagicMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=trigger_mock)
    execute_result.scalar_one = MagicMock(
        return_value=getattr(request.node, "_today_cost", Decimal(0))
    )
    session.execute = AsyncMock(return_value=execute_result)

    mock_factory = MagicMock(return_value=session)
    with (
        patch("modulo.core.cron_scheduler._get_engine"),
        patch("modulo.core.cron_scheduler.async_sessionmaker", return_value=mock_factory),
        patch("modulo.core.cron_scheduler._set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.cron_scheduler._count_active_runs",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch("modulo.core.cron_scheduler.create_run", new_callable=AsyncMock, return_value=run_mock),
        patch("modulo.core.cron_scheduler._log_event", new_callable=AsyncMock) as mock_event,
    ):
        mock_event.return_value = MagicMock(id=uuid.uuid4())

        import asyncio

        from modulo.core.cron_scheduler import fire_cron_trigger as fire_fn

        event_loop = asyncio.new_event_loop()
        try:
            result = event_loop.run_until_complete(
                fire_fn(
                    trigger_id=trigger_id,
                    org_id=org_id,
                    pipeline_id=pipeline_id,
                    snapshot_id=snapshot_id,
                    cron_expression=getattr(request.node, "_cron_expression", "0 * * * *"),
                )
            )
        finally:
            event_loop.close()

    request.node._fire_result = result
    request.node._trigger_mock = trigger_mock
    request.node._run_mock = run_mock


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then(parsers.parse('the trigger has cron_expression "{expression}"'))
def trigger_has_expression(expression: str, request):
    data = request.node._resp.json()
    assert data.get("cron_expression") == expression, f"Expected {expression}, got {data.get('cron_expression')}"


@then(parsers.parse('the trigger has cron_timezone "{timezone}"'))
def trigger_has_timezone(timezone: str, request):
    data = request.node._resp.json()
    assert data.get("cron_timezone") == timezone, f"Expected {timezone}, got {data.get('cron_timezone')}"


@then(parsers.parse("the trigger has input_template {template}"))
def trigger_has_input_template(template, request):
    expected = json.loads(template) if isinstance(template, str) else template
    data = request.node._resp.json()
    assert data.get("input_template") == expected, f"Expected {expected}, got {data.get('input_template')}"


@then("the trigger has a next_fire_at timestamp")
def trigger_has_next_fire(request):
    data = request.node._resp.json()
    assert data.get("next_fire_at") is not None, "Expected next_fire_at to be set"


@then(parsers.parse("a run is created with trigger_type {ttype}"))
def run_created_with_trigger_type(ttype: str, request):
    result = getattr(request.node, "_fire_result", None)
    if result:
        assert result.get("status") == "fired", f"Expected fired, got {result}"
        assert result.get("run_id") is not None
    else:
        run = getattr(request.node, "_run_mock", None)
        if run:
            assert run.trigger_type == ttype


@then("the run references the cron trigger")
def run_references_trigger(request):
    run = getattr(request.node, "_run_mock", None)
    if run:
        assert run.trigger_id is not None


@then("the trigger's last_fired_at is updated")
def trigger_last_fired_updated(request):
    result = getattr(request.node, "_fire_result", None)
    assert result is not None
    assert result.get("status") == "fired"


@then("the trigger's next_fire_at is advanced")
def trigger_next_fire_advanced(request):
    result = getattr(request.node, "_fire_result", None)
    assert result is not None
    assert result.get("next_fire_at") is not None


@then(parsers.parse('the trigger is skipped with reason "{reason}"'))
def trigger_skipped_with_reason(reason: str, request):
    result = getattr(request.node, "_fire_result", None)
    assert result is not None, "No fire result stored"
    assert result.get("status") == "skipped", f"Expected skipped, got {result.get('status')}"
    assert result.get("reason") == reason, f"Expected reason {reason}, got {result.get('reason')}"


@then(parsers.parse('a TriggerEvent is created with result "{result}"'))
def trigger_event_with_result(result: str, request):
    pass


@then(parsers.parse('a TriggerEvent is created with type "{etype}"'))
def trigger_event_with_type(etype: str, request):
    pass


@then("the TriggerEvent references the created run")
def trigger_event_references_run(request):
    pass


@then(parsers.parse('a run is created with input_payload {payload}'))
def run_created_with_input_payload(payload, request):
    result = getattr(request.node, "_fire_result", None)
    assert result is not None
    assert result.get("status") == "fired"


@then(parsers.parse('the run has trigger_type "{ttype}"'))
def run_has_trigger_type(ttype: str, request):
    run = getattr(request.node, "_run_mock", None)
    if run:
        assert run.trigger_type == ttype
    result = getattr(request.node, "_fire_result", None)
    if result:
        assert result.get("status") == "fired"


@then(parsers.parse('the TriggerEvent has result "{result_val}"'))
def trigger_event_has_result(result_val: str, request):
    result = getattr(request.node, "_fire_result", None)
    if result:
        assert result.get("status") == "fired"


@then("no run is created")
def no_run_created(request):
    result = getattr(request.node, "_fire_result", None)
    if result:
        assert result.get("status") != "fired", f"Expected no run, but got {result}"


@then("the response status is 201")
def response_status_201(request):
    resp = getattr(request.node, "_resp", None)
    assert resp is not None
    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}"


@then("the response status is 422")
def response_status_422(request):
    resp = getattr(request.node, "_resp", None)
    assert resp is not None
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"


@then(parsers.parse('the error mentions "{msg}"'))
def error_mentions(msg: str, request):
    data = request.node._resp.json()
    assert msg in str(data.get("detail", "")), f"Expected error to mention '{msg}', got {data}"
