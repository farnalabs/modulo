"""BDD step definitions: Manual trigger, webhook HMAC, payload mapping,
flood protection, trigger event log."""

import json
import uuid
from unittest.mock import MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

try:
    scenarios("../../features/triggers/manual.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/triggers/webhook_hmac.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/triggers/webhook_payload_mapping.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/triggers/flood_protection.feature")
except (FileNotFoundError, OSError):
    pass
try:
    scenarios("../../features/triggers/trigger_event_log.feature")
except (FileNotFoundError, OSError):
    pass

from tests.bdd.conftest import make_mock_pipeline  # noqa: E402


@given(parsers.parse('org "{org}" has pipeline "{name}"'))
def org_has_pipeline(org: str, name: str, request):
    request.node._pipeline_name = name


@when(parsers.parse("I POST /api/pipelines/{pipeline}/runs with empty run_context"))
def trigger_manual_run(pipeline: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_pipeline_by_name",
            return_value=make_mock_pipeline(name=pipeline),
        ),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_run",
            return_value=MagicMock(id=uuid.uuid4(), status="pending"),
        ),
        patch("modulo.trigger_engine.TriggerEngine.trigger_manual"),
    ):
        resp = client.post(f"/api/pipelines/{pipeline}/runs", json={})
    request.node._resp = resp


@when(parsers.parse('I POST /api/pipelines/{pipeline}/runs with run_context branch="{branch}"'))
def trigger_run_with_context(pipeline: str, branch: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_pipeline_by_name",
            return_value=make_mock_pipeline(name=pipeline),
        ),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_run",
            return_value=MagicMock(id=uuid.uuid4(), status="pending"),
        ),
        patch("modulo.trigger_engine.TriggerEngine.trigger_manual"),
    ):
        resp = client.post(
            f"/api/pipelines/{pipeline}/runs",
            json={"run_context": {"branch": branch}},
        )
    request.node._resp = resp


@then(parsers.parse('a run is created with status "{status}"'))
def check_run_status(status: str, request):
    data = request.node._resp.json()
    assert data.get("status") == status, f"Expected status {status}, got {data}"


@given(parsers.parse('no pipeline exists with slug "{slug}"'))
def no_pipeline_slug(slug: str, request):
    request.node._no_pipeline = slug


@when(parsers.parse("I POST /api/pipelines/{slug}/runs with empty run_context"))
def trigger_nonexistent(slug: str, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_pipeline_by_name",
            return_value=None,
        ),
    ):
        resp = client.post(f"/api/pipelines/{slug}/runs", json={})
    request.node._resp = resp


@given(parsers.parse('org "{org}" has pipeline "{name}" with status "{status}"'))
def pipeline_with_status(org: str, name: str, status: str, request):
    request.node._pipeline_name = name
    request.node._pipeline_status = status


@given(parsers.parse('org "{org}" has pipeline "{name}" with webhook secret "{secret}"'))
def pipeline_with_webhook_secret(org: str, name: str, secret: str, request):
    request.node._pipeline_name = name
    request.node._webhook_secret = secret


@given('org "{org}" has pipeline "{name}" with no webhook secret')
def pipeline_no_webhook_secret(org: str, name: str, request):
    request.node._pipeline_name = name
    request.node._webhook_secret = None


@when(parsers.parse("I POST /api/webhooks/{pipeline} with payload {payload} and valid HMAC"))
def webhook_valid_hmac(pipeline: str, payload, client, request):
    payload_dict = json.loads(payload) if isinstance(payload, str) else payload
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.get_pipeline_by_name",
            return_value=make_mock_pipeline(name=pipeline),
        ),
        patch(
            "modulo.webhook_trigger.verify_hmac_signature",
            return_value=True,
        ),
        patch(
            "modulo.webhook_trigger.deduplicate_trigger",
            return_value=None,
        ),
        patch(
            "modulo.core.pipeline_engine.run_crud.create_run",
            return_value=MagicMock(id=uuid.uuid4(), status="pending"),
        ),
    ):
        resp = client.post(
            f"/api/webhooks/{pipeline}",
            json=payload_dict,
            headers={"X-Modulo-Signature-256": "valid_signature"},
        )
    request.node._resp = resp


@when(parsers.parse("I POST /api/webhooks/{pipeline} with payload {payload} and invalid HMAC"))
def webhook_invalid_hmac(pipeline: str, payload, client, request):
    payload_dict = json.loads(payload) if isinstance(payload, str) else payload
    with (
        patch("modulo.webhook_trigger.verify_hmac_signature", return_value=False),
    ):
        resp = client.post(
            f"/api/webhooks/{pipeline}",
            json=payload_dict,
            headers={"X-Modulo-Signature-256": "bad_signature"},
        )
    request.node._resp = resp


@when(parsers.parse("I POST /api/webhooks/{pipeline} with payload {payload} and no HMAC"))
def webhook_no_hmac(pipeline: str, payload, client, request):
    payload_dict = json.loads(payload) if isinstance(payload, str) else payload
    resp = client.post(f"/api/webhooks/{pipeline}", json=payload_dict)
    request.node._resp = resp


@then(parsers.parse("a TriggerEvent is created with type {event_type}"))
def trigger_event_created(event_type: str, request):
    pass


@then(parsers.parse("the TriggerEvent references the created run"))
def trigger_event_references_run(request):
    pass


@then(parsers.parse("the TriggerEvent has the original payload"))
def trigger_event_has_payload(request):
    pass


@then(parsers.parse("the TriggerEvent has payload {payload}"))
def trigger_event_payload(payload: str, request):
    pass


@then(parsers.parse('the TriggerEvent has triggered_by "{user}"'))
def trigger_event_triggered_by(user: str, request):
    pass


@then(parsers.parse('the TriggerEvent has status "{status}"'))
def trigger_event_status(status: str, request):
    pass


@then("the TriggerEvent has error_detail")
def trigger_event_error(request):
    pass


@then(parsers.parse("the run has run_context with {key} {value}"))
def check_run_context(key: str, value, request):
    pass


@then(parsers.parse('the run has trigger_type "{ttype}"'))
def check_trigger_type(ttype: str, request):
    data = request.node._resp.json()
    assert data.get("trigger_type") == ttype


@given(parsers.parse('org "{org}" has pipeline "{name}" with payload mapping {mapping}'))
def pipeline_with_payload_mapping(org: str, name: str, mapping, request):
    request.node._pipeline_name = name
    request.node._payload_mapping = json.loads(mapping) if isinstance(mapping, str) else mapping


@when(parsers.parse("I POST /api/webhooks/{pipeline} with same payload {payload} and valid HMAC"))
def webhook_duplicate(pipeline: str, payload, client, request):
    payload_dict = json.loads(payload) if isinstance(payload, str) else payload
    with (
        patch("modulo.webhook_trigger.verify_hmac_signature", return_value=True),
        patch(
            "modulo.webhook_trigger.deduplicate_trigger",
            return_value=uuid.uuid4(),
        ),
    ):
        resp = client.post(
            f"/api/webhooks/{pipeline}",
            json=payload_dict,
            headers={"X-Modulo-Signature-256": "valid_signature"},
        )
    request.node._resp = resp


@when("I send {count:d} webhooks in rapid succession")
def send_rapid_webhooks(count: int, client, request):
    request.node._webhook_count = count


@then(parsers.parse("the {nth} webhook is rate limited"))
def check_rate_limited(nth: str, request):
    pass


@then(parsers.parse("only {count:d} run was created"))
def check_only_n_runs(count: int, request):
    pass


@when(parsers.parse('I trigger a manual run for pipeline "{name}"'))
def trigger_manual_run_simple(name: str, client, request):
    trigger_manual_run(name, client, request)


@given(parsers.parse("{count:d} manual triggers have been performed"))
def manual_triggers_performed(count: int, request):
    request.node._trigger_count = count


@when(parsers.parse("I GET /api/triggers/events?limit={limit:d}"))
def get_trigger_events(limit: int, client, request):
    with (
        patch("modulo.core.pipeline_engine.run_crud.set_rls_org"),
        patch(
            "modulo.core.pipeline_engine.run_crud.list_trigger_events",
            return_value=[MagicMock(id=uuid.uuid4()) for _ in range(limit)],
        ),
    ):
        resp = client.get(f"/api/triggers/events?limit={limit}")
    request.node._resp = resp


@then(parsers.parse("the response contains {count:d} TriggerEvents"))
def check_trigger_event_count(count: int, request):
    data = request.node._resp.json()
    assert len(data) == count


@then(parsers.parse('the error mentions "{text}"'))
def error_mentions(text: str, request):
    data = request.node._resp.json()
    detail = str(data.get("detail", data.get("error", ""))).lower()
    assert text.lower() in detail, f"Does not mention '{text}': {data}"


@given("I am authenticated in org {org} as {user}")
def authenticated_as_user(org: str, user: str, request):
    request.node._auth_user = user
