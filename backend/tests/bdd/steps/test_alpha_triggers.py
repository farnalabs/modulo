"""BDD step definitions: Manual trigger, webhook HMAC, payload mapping,
flood protection, trigger event log."""

import contextlib
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/triggers/manual.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/triggers/webhook_hmac.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/triggers/webhook_payload_mapping.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/triggers/flood_protection.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/triggers/trigger_event_log.feature")

from collections.abc import AsyncGenerator

from modulo.api.dependencies import get_system_db_session
from modulo.api.main import app
from tests.bdd.conftest import (
    make_mock_pipeline,
    make_mock_run,
    make_mock_snapshot,
    make_system_session_mock,
)

_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_TRIGGER_ID = uuid.UUID("00000000-0000-0000-0000-00000000000b")


class _ProvisionedSystemSettings:
    """Settings stub presenting a provisioned system database URL.

    These BDD scenarios mock the system SESSION but run without
    ``MODULO_SYSTEM_DATABASE_URL``; the (robust) fallback predicate would
    otherwise 503 every delivery. The created engine is lazy and never
    connects.
    """

    modulo_system_database_url = "postgresql+asyncpg://localhost/modulo-system-bdd-test"


@pytest.fixture(autouse=True)
def _provisioned_system_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    from modulo.api import dependencies as _deps

    monkeypatch.setattr(_deps, "get_settings", lambda: _ProvisionedSystemSettings())


def _patch_trigger_run(client, request, *, pipeline=None, run=None, payload=None, pipeline_not_found=False):
    """POST /api/v1/runs with the current route's patch targets."""
    if pipeline is None and not pipeline_not_found:
        pipeline = make_mock_pipeline(id=_PIPELINE_ID, name=request.node._pipeline_name)
    run = run or make_mock_run(status="pending", trigger_type="manual")
    with (
        patch("modulo.api.routes.runs.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.runs.get_pipeline", new_callable=AsyncMock, return_value=pipeline),
        patch(
            "modulo.api.routes.runs.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
            return_value=make_mock_snapshot(),
        ),
        patch("modulo.api.routes.runs.create_run", new_callable=AsyncMock, return_value=run) as create_run,
        patch("modulo.api.routes.runs.dispatch_run", new_callable=AsyncMock),
    ):
        resp = client.post(
            "/api/v1/runs",
            json={"pipeline_id": str(_PIPELINE_ID), "input_payload": payload or {}},
        )
    request.node._resp = resp
    request.node._create_run = create_run


@given(parsers.parse('org "{org}" has pipeline "{name}"'))
def org_has_pipeline(org: str, name: str, request):
    request.node._pipeline_name = name


@when(parsers.parse('I POST /api/v1/runs for "{pipeline}" with empty run_context'))
def trigger_manual_run(pipeline: str, client, request):
    _patch_trigger_run(client, request)


@when(parsers.parse('I POST /api/v1/runs for "{pipeline}" with run_context branch="{branch}"'))
def trigger_run_with_context(pipeline: str, branch: str, client, request):
    _patch_trigger_run(client, request, payload={"run_context": {"branch": branch}})


@then(parsers.parse('a run is created with status "{status}"'))
def check_run_status(status: str, request):
    data = request.node._resp.json()
    assert data.get("status") == status, f"Expected status {status}, got {data}"


@then(parsers.parse("the run has run_context with {key} {value}"))
def check_run_context(key: str, value, request):
    create_run = request.node._create_run
    kwargs = create_run.await_args.kwargs
    payload = kwargs.get("input_payload", {})
    run_context = payload.get("run_context", {})
    expected = value.strip('"')
    assert str(run_context.get(key)) == str(expected), f"Expected run_context.{key}={expected}, got {run_context}"


@then(parsers.parse('the run is created with trigger_type "{ttype}"'))
def check_trigger_type(ttype: str, request):
    create_run = request.node._create_run
    assert create_run.await_args.kwargs.get("trigger_type") == ttype


@given(parsers.parse('no pipeline exists with slug "{slug}"'))
def no_pipeline_slug(slug: str, request):
    request.node._no_pipeline = slug


@when("I POST /api/v1/runs for a non-existent pipeline")
def trigger_nonexistent(client, request):
    request.node._pipeline_name = "ghost"
    _patch_trigger_run(client, request, pipeline_not_found=True)


@given(parsers.parse('org "{org}" has trigger "{name}" with webhook secret "{secret}"'))
def trigger_with_webhook_secret(org: str, name: str, secret: str, request):
    request.node._trigger_name = name
    request.node._webhook_secret = secret


@given(parsers.parse('no trigger exists with id "{trigger_id}"'))
def no_trigger_exists(trigger_id: str, request, mock_session):
    request.node._trigger_name = trigger_id
    request.node._webhook_secret = "secret"
    request.node._mock_session = mock_session


def _post_webhook(client, request, payload, *, error=None, trigger_missing=False, paused=False):
    """POST /api/v1/triggers/{name}/webhook with the current route's patch targets."""
    from modulo.core import trigger_engine as trigger_engine_module

    handle_webhook = AsyncMock(
        return_value=(
            make_mock_run(status="pending", trigger_type="webhook"),
            MagicMock(),
            payload,
        )
    )
    if error is not None:
        handle_webhook.side_effect = error
    request.node._handle_webhook = handle_webhook
    # The BDD mock session is an AsyncMock whose ``add`` would return an
    # unawaited coroutine when the route's paused catch writes an event — make
    # it a plain MagicMock so the paused path produces no RuntimeWarning.
    mock_session = request.getfixturevalue("mock_session")
    mock_session.add = MagicMock()
    with (
        patch("modulo.api.routes.webhooks.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
            return_value=make_mock_snapshot(),
        ),
        patch.object(trigger_engine_module.TriggerEngine, "handle_webhook", handle_webhook),
        # _dispatch_webhook_run: the route fires it as a background task — a
        # no-op patch keeps the BDD test hermetic (the old pipeline_executor_task
        # target was removed in the Celery->SAQ cutover).
        patch("modulo.api.routes.webhooks._dispatch_webhook_run", lambda *a, **k: None),
        # Org-wide pause gate: the mocked org read is a MagicMock that would
        # fail-closed as paused — force the deterministic state instead.
        patch("modulo.db.settings_resolver.org_is_paused", new_callable=AsyncMock, return_value=paused),
        # Route-level timestamp/HMAC validation against the mocked trigger's
        # MagicMock config_json would 400/401 the happy path — bypass it (the
        # engine-level HMAC handling is covered by webhook_hmac.feature).
        patch("modulo.api.routes.webhooks.verify_timestamp", return_value=1700000000),
        patch("modulo.api.routes.webhooks.verify_hmac", return_value=True),
    ):
        headers = {
            "X-Modulo-Timestamp": "1700000000",
            "X-Modulo-Webhook-Secret": request.node._webhook_secret or "secret",
        }
        if trigger_missing:
            # The bootstrap trigger read runs on the SYSTEM session (FAR-523):
            # a missing trigger must be absent from the instance-global read.
            missing_system = make_system_session_mock(trigger_found=False)

            async def _missing_system_override() -> AsyncGenerator[AsyncMock, None]:
                yield missing_system

            previous = app.dependency_overrides.get(get_system_db_session)
            app.dependency_overrides[get_system_db_session] = _missing_system_override
            try:
                resp = client.post(
                    f"/api/v1/triggers/{request.node._trigger_name}/webhook",
                    json=payload,
                    headers=headers,
                )
            finally:
                # pop-or-restore: put back whatever the client fixture installed.
                if previous is None:
                    app.dependency_overrides.pop(get_system_db_session, None)
                else:
                    app.dependency_overrides[get_system_db_session] = previous
        else:
            resp = client.post(
                f"/api/v1/triggers/{request.node._trigger_name}/webhook",
                json=payload,
                headers=headers,
            )
    request.node._resp = resp


@when(parsers.parse("I POST /api/v1/triggers/{name}/webhook with payload {payload} and valid HMAC"))
def webhook_valid_hmac(name: str, payload, client, request):
    payload_dict = json.loads(payload) if isinstance(payload, str) else payload
    trigger_missing = hasattr(request.node, "_mock_session")
    _post_webhook(client, request, payload_dict, trigger_missing=trigger_missing)


@when(parsers.parse("I POST /api/v1/triggers/{name}/webhook with payload {payload} and valid HMAC raising duplicate"))
def webhook_duplicate(name: str, payload, client, request):
    from modulo.core.trigger_engine import DuplicateWebhookError

    payload_dict = json.loads(payload) if isinstance(payload, str) else payload
    _post_webhook(client, request, payload_dict, error=DuplicateWebhookError(payload_hash="x"))


@when(parsers.parse("I POST /api/v1/triggers/{name}/webhook with payload {payload} and valid HMAC raising rate_limit"))
def webhook_rate_limited(name: str, payload, client, request):
    from modulo.core.trigger_engine import PipelineRateLimitError

    payload_dict = json.loads(payload) if isinstance(payload, str) else payload
    _post_webhook(
        client,
        request,
        payload_dict,
        error=PipelineRateLimitError(pipeline_id=_PIPELINE_ID, key="k", max_triggers=10, window_seconds=3600),
    )


@when(parsers.parse("I POST /api/v1/triggers/{name}/webhook with payload {payload} and invalid HMAC"))
def webhook_invalid_hmac(name: str, payload, client, request):
    from modulo.core.trigger_engine import HmacValidationError

    payload_dict = json.loads(payload) if isinstance(payload, str) else payload
    _post_webhook(client, request, payload_dict, error=HmacValidationError())


@when(parsers.parse("I POST /api/v1/triggers/{name}/webhook with payload {payload} and no HMAC"))
def webhook_no_hmac(name: str, payload, client, request):
    from modulo.core.trigger_engine import HmacValidationError

    payload_dict = json.loads(payload) if isinstance(payload, str) else payload
    _post_webhook(client, request, payload_dict, error=HmacValidationError())


@then("the webhook is accepted")
def webhook_accepted(request):
    data = request.node._resp.json()
    assert data.get("status") == "accepted", f"Expected accepted, got {data}"


@then("the trigger engine received the raw payload")
def trigger_engine_received_payload(request):
    handle_webhook = request.node._handle_webhook
    assert handle_webhook.await_args is not None, "handle_webhook was not called"
    kwargs = handle_webhook.await_args.kwargs
    assert kwargs.get("raw_payload") is not None


@then("the trigger engine was called for the delivery")
def trigger_engine_called(request):
    handle_webhook = request.node._handle_webhook
    assert handle_webhook.await_args is not None, "handle_webhook was not called"


@then(parsers.parse('the error mentions "{text}"'))
def error_mentions(text: str, request):
    data = request.node._resp.json()
    detail = str(data.get("detail", data.get("error", ""))).lower()
    assert text.lower() in detail, f"Does not mention '{text}': {data}"


@given(parsers.parse("{count:d} trigger events have been recorded for the trigger"))
def trigger_events_recorded(count: int, request, mock_session):
    events = []
    for _ in range(count):
        ev = MagicMock()
        ev.id = uuid.uuid4()
        ev.validation_result = "accepted"
        ev.received_at = None
        ev.created_at = None
        ev.run_id = None
        ev.error_detail = None
        events.append(ev)
    request.node._trigger_events = events
    request.node._mock_session = mock_session


@when(parsers.parse("I GET /api/v1/triggers/{name}/events?limit={limit:d}"))
def list_trigger_events(name: str, limit: int, client, request, mock_session):
    events = getattr(request.node, "_trigger_events", [])

    trigger_row = MagicMock()
    trigger_row.scalar_one_or_none.return_value = MagicMock()
    events_row = MagicMock()
    events_row.scalars.return_value.all.return_value = events[:limit]

    async def _fake_execute(stmt, *args, **kwargs):
        if "trigger_events" in str(stmt):
            return events_row
        return trigger_row

    with (
        patch("modulo.api.routes.triggers.set_rls_org", new_callable=AsyncMock),
        patch.object(mock_session, "execute", side_effect=_fake_execute),
    ):
        resp = client.get(f"/api/v1/triggers/{name}/events?limit={limit}")
    request.node._resp = resp


@then(parsers.parse("the response contains {count:d} TriggerEvents"))
def check_trigger_event_count(count: int, request):
    data = request.node._resp.json()
    assert len(data.get("items", data)) == count, f"Expected {count} events, got {data}"
