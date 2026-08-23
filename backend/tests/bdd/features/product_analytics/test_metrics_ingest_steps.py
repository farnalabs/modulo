"""BDD step definitions: Product Analytics Metrics Ingest (FAR-355).

Each scenario drives the real ``POST /api/v1/metrics/events`` route through a
TestClient, patching only the CRUD helpers and RLS primitives it depends on.
The staged writes are observed by intercepting ``pg_insert`` statements handed
to the mocked session — mirroring the assertions in
``tests/unit/product_analytics/test_metrics_ingest.py`` at the API level.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import Insert as PGInsert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from tests.bdd.conftest import ORG_ID, _active_client

scenarios("metrics_ingest.feature")

API_ERROR_DAILY_CAP = 100


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


def _org_with_consent(level: str | None) -> MagicMock:
    org = MagicMock()
    org.id = ORG_ID
    org.settings_json = {} if level is None else {"product_analytics": {"level": level}}
    return org


def _make_event(event_id: str, event_type: str = "pipeline_created", **payload: Any) -> dict:
    return {"event_id": event_id, "event_type": event_type, "payload": payload}


def _wrap_execute(mock_session: AsyncMock, *, raise_on_insert: Exception | None = None) -> list:
    """Wrap ``mock_session.execute`` to record PGInsert statements.

    Returns the list of captured inserts. When *raise_on_insert* is given, the
    wrapper raises it for PGInsert statements so the route's best-effort
    ``IntegrityError`` / ``SQLAlchemyError`` paths can be exercised end-to-end.
    """
    original = mock_session.execute
    captured: list = []

    async def execute(stmt, *args: Any, **kwargs: Any) -> Any:
        if isinstance(stmt, PGInsert):
            if raise_on_insert is not None:
                raise raise_on_insert
            captured.append(stmt)
        return await original(stmt, *args, **kwargs)

    mock_session.execute = execute
    return captured


def _compiled_payload(stmt: PGInsert) -> dict:
    compiled = stmt.compile(dialect=postgresql.dialect())
    payload = compiled.params.get("payload")
    assert isinstance(payload, dict), "captured insert must carry a payload dict"
    return payload


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(parsers.parse('the org has product analytics consent level "{level}"'))
def _given_consent_level(level: str, ctx) -> None:
    ctx["org"] = _org_with_consent(level)


@given("the org has no product analytics settings")
def _given_no_analytics_settings(ctx) -> None:
    ctx["org"] = _org_with_consent(None)


@given("the organisation does not exist")
def _given_org_missing(ctx) -> None:
    ctx["org"] = None


@given("today's api_error count is at the daily cap")
def _given_api_error_at_cap(ctx) -> None:
    ctx["api_error_count"] = API_ERROR_DAILY_CAP


@given("today's api_error count is below the daily cap")
def _given_api_error_below_cap(ctx) -> None:
    ctx["api_error_count"] = API_ERROR_DAILY_CAP - 5


@given("the staging insert rejects duplicate event ids")
def _given_insert_integrity_error(ctx, mock_session) -> None:
    ctx["_captured"] = _wrap_execute(
        mock_session,
        raise_on_insert=IntegrityError("INSERT INTO metrics_staging", {}, Exception("duplicate key")),
    )


@given("the staging insert fails with a database error")
def _given_insert_db_error(ctx, mock_session) -> None:
    ctx["_captured"] = _wrap_execute(mock_session, raise_on_insert=SQLAlchemyError("connection lost"))


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.re(r"I POST /api/v1/metrics/events with (?P<count>\d+) valid events?"))
def _when_post_valid_batch(count: str, request, ctx, mock_session) -> None:
    events = [_make_event(f"evt-{i}") for i in range(int(count))]
    if "_captured" not in ctx:
        ctx["_captured"] = _wrap_execute(mock_session)
    request.node._resp = _ingest(_active_client(request), ctx, events)


@when(parsers.re(r"I POST /api/v1/metrics/events with (?P<count>\d+) api_error events?"))
def _when_post_api_error_batch(count: str, request, ctx, mock_session) -> None:
    events = [_make_event(f"evt-{i}", "api_error") for i in range(int(count))]
    if "_captured" not in ctx:
        ctx["_captured"] = _wrap_execute(mock_session)
    request.node._resp = _ingest(_active_client(request), ctx, events)


@when(parsers.parse('I POST /api/v1/metrics/events with an api_error event carrying route "{route}"'))
def _when_post_api_error_with_route(route: str, request, ctx, mock_session) -> None:
    events = [_make_event("evt-1", "api_error", route=route, status=500)]
    if "_captured" not in ctx:
        ctx["_captured"] = _wrap_execute(mock_session)
    request.node._resp = _ingest(_active_client(request), ctx, events)


@when("I POST an empty event batch")
def _when_post_empty_batch(request, ctx) -> None:
    request.node._resp = _ingest(_active_client(request), ctx, [])


@when(parsers.parse("I POST a batch with {count:d} events"))
def _when_post_oversized_batch(count: int, request, ctx) -> None:
    events = [_make_event(f"evt-{i}") for i in range(count)]
    request.node._resp = _ingest(_active_client(request), ctx, events)


@when(parsers.parse('I POST /api/v1/metrics/events with an event of type "{event_type}"'))
def _when_post_unknown_type(event_type: str, request, ctx) -> None:
    events = [{"event_id": "evt-1", "event_type": event_type}]
    request.node._resp = _ingest(_active_client(request), ctx, events)


@when("I POST /api/v1/metrics/events with an event missing its event id")
def _when_post_missing_event_id(request, ctx) -> None:
    events = [{"event_type": "pipeline_created"}]
    request.node._resp = _ingest(_active_client(request), ctx, events)


def _ingest(client: Any, ctx: dict[str, Any], events: list[dict]) -> Any:
    """POST to the real route with only its helpers mocked."""
    org = ctx.get("org", _org_with_consent("all"))
    api_error_count = ctx.get("api_error_count", 0)

    import modulo.api.routes.metrics_ingest as metrics_module

    with (
        patch.object(metrics_module, "set_rls_org", new_callable=AsyncMock),
        patch.object(metrics_module, "set_rls_user_context", new_callable=AsyncMock),
        patch.object(metrics_module, "get_organisation", new_callable=AsyncMock, return_value=org),
        patch.object(
            metrics_module,
            "_api_error_count_today",
            new_callable=AsyncMock,
            return_value=api_error_count,
        ),
    ):
        return client.post("/api/v1/metrics/events", json={"events": events})


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the event is staged for the daily dump")
def _then_single_event_staged(request, ctx) -> None:
    assert len(ctx["_captured"]) == 1, f"Expected 1 staged event, got {len(ctx['_captured'])}"


@then(parsers.parse("{count:d} events are staged for the daily dump"))
def _then_events_staged(count: int, request, ctx) -> None:
    assert len(ctx["_captured"]) == count, f"Expected {count} staged events, got {len(ctx['_captured'])}"


@then("no events are staged for the daily dump")
def _then_no_events_staged(request, ctx) -> None:
    assert not ctx["_captured"], f"Expected no staged events, got {len(ctx['_captured'])}"


@then(parsers.parse('the staged payload route is "{expected}"'))
def _then_staged_route(expected: str, request, ctx) -> None:
    assert len(ctx["_captured"]) == 1, f"Expected 1 staged event, got {len(ctx['_captured'])}"
    payload = _compiled_payload(ctx["_captured"][0])
    assert payload["route"] == expected, f"Expected route {expected!r}, got {payload.get('route')!r}"
