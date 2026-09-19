"""Step definitions for the Analytics Query Surface BDD feature (FAR-102 / ADR 020).

Wires the scenarios in ``query.feature`` to the REAL analytics routes
(``modulo/api/routes/analytics.py``: ``/query``, ``/concurrency``,
``/guardrails``, ``/export``) through the shared TestClient pattern used by the
dashboard steps. The route is a thin adapter over ``modulo.core.analytics.service``,
so only the four service functions are patched — every other seam is real: the
FastAPI query-param binding (enum/date/limit validation), the ``analytics.query``
permission gate, the org-context requirement, the ``analytics_page`` feature
gate, the error mapping to 422/429/503, and the response model serialisation.

The org-context path is covered for both orientations: an admin principal
(which short-circuits the team-boundary resolver before any session work) and a
runner principal (which is refused by the permission gate before the handler
runs). The unauthenticated caller is exercised with NO ``Authorization`` header
so the HTTPBearer dependency itself surfaces the 401 (see the AGENTS.md lesson
that pre-auth denial paths need an explicit unauthenticated assertion).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when
from tests.unit.api.mock_session import configure_mock_session

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.analytics.service import (
    AnalyticsQueryTimeoutError,
    AnalyticsRateLimitedError,
    AnalyticsValidationError,
)
from modulo.settings import Settings, get_settings

scenarios("query.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_FILTER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_FILTER_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_RUN_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_RUN_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

_QUERY_RESULT: dict[str, Any] = {
    "group_by": "day",
    "dimension": None,
    "date_from": "2026-07-01T00:00:00+00:00",
    "date_to": "2026-07-07T23:59:59.999999+00:00",
    "facts_freshness_hours": 3.0,
    "facts_stale": False,
    "buckets": [{"date": "2026-07-01", "key": None, "count": 3}],
}

_DIMENSION_RESULT: dict[str, Any] = {
    **_QUERY_RESULT,
    "dimension": "trigger_type",
    "buckets": [
        {"date": "2026-07-01", "key": "manual", "count": 2},
        {"date": "2026-07-01", "key": "cron", "count": 1},
    ],
}

_EXPORT_RESULT: dict[str, Any] = {
    "items": [
        {
            "run_id": str(_RUN_A),
            "run_date": "2026-07-01",
            "trigger_type": "manual",
            "status": "complete",
            "total_cost_usd": 1.25,
            "total_tokens": 100,
            "created_at": "2026-07-01T09:00:00+00:00",
        },
        {
            "run_id": str(_RUN_B),
            "run_date": "2026-07-02",
            "trigger_type": "cron",
            "status": "failed",
            "error_code": "node_timeout",
            "created_at": "2026-07-02T09:00:00+00:00",
        },
    ],
    "total": 2,
    "offset": 0,
    "limit": 10,
}

_CONCURRENCY_RESULT: dict[str, Any] = {
    "group_by": "day",
    "date_from": "2026-07-01T00:00:00+00:00",
    "date_to": "2026-07-07T23:59:59.999999+00:00",
    "pool_reference": 3,
    "buckets": [
        {
            "date": "2026-07-01",
            "key": None,
            "max_active": 2,
            "avg_active": 1.5,
            "max_queued": 1,
            "avg_queued": 0.5,
            "pool_reference": 3,
        },
    ],
}

_GUARDRAIL_RESULT: dict[str, Any] = {
    "advisory_only": True,
    "date_from": "2026-07-01T00:00:00+00:00",
    "date_to": "2026-07-07T23:59:59.999999+00:00",
    "scope": {
        "runs_with_guardrail": 2,
        "runs_with_violations": 1,
        "runs_blocked": 1,
        "first_try_pass_runs": 1,
    },
    "fire_counts": {
        "bound": 2,
        "evaluated": 2,
        "passed": 1,
        "violated": 1,
        "observed": 0,
        "errored": 0,
        "redacted": 0,
        "skipped": 0,
        "expected_skips": 0,
        "unexpected_skips": 0,
    },
    "rates": {"raw_violation_rate": 0.5, "first_try_pass_rate": 0.5, "note": ""},
    "self_correction": {
        "corrections_total": 0,
        "converged_clean": 0,
        "escalated_hitl": 0,
        "budget_exhausted": 0,
        "dismissed": 0,
        "in_flight": 0,
        "corrected_pass_rate": None,
        "note": "",
    },
    "evasion_band_drift": {
        "current_errored_rate": 0.0,
        "baseline_errored_rate": 0.0,
        "baseline_window_days": 7,
        "unexpected_skips_total": 0,
        "drift_detected": False,
        "drift_indicator": "in_band",
        "advisory_only": True,
        "note": "",
    },
    "generated_at": "2026-07-08T00:00:00+00:00",
}


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
        modulo_csrf_enabled=False,
    )


def _make_mock_session() -> AsyncMock:
    """AsyncSession double satisfying the permission gate's kill-switch read.

    ``configure_mock_session`` stubs ``execute`` for the per-request
    ``authz_enforce`` SELECT (returning ``None`` → enforcement defaults to ON),
    so ``require_permission`` resolves for the admin principal without a
    database. The session is never asked anything else: an admin short-circuits
    the team-boundary resolver before any analytics session factory is used.
    """
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _principal(role: str, *, org_context: bool = True) -> TenantPrincipal:
    return TenantPrincipal(
        username="analyst",
        organisation_id=_ORG_ID if org_context else None,
        account_id=_ACCOUNT_ID,
        org_role=role,
    )


def _make_client(
    *,
    role: str = "admin",
    unauthenticated: bool = False,
    feature_disabled: bool = False,
    org_context: bool = True,
) -> Generator[TestClient, None, None]:
    """Build a TestClient over the REAL app with the auth/feature seams overridden.

    ``unauthenticated=True`` leaves ``get_current_tenant_user`` intact so the
    HTTPBearer dependency itself produces the 401 (no header), while the plan
    and session seams are still overridden so only the auth dependency raises.
    """
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield _make_mock_session()

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = not feature_disabled

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    if not unauthenticated:
        app.dependency_overrides[get_current_tenant_user] = lambda: _principal(role, org_context=org_context)
    try:
        yield TestClient(app)
    finally:
        for dep in (get_settings, get_db_session, get_plan_context, get_current_tenant_user):
            app.dependency_overrides.pop(dep, None)


def _ctx(request: Any) -> dict[str, Any]:
    if not hasattr(request.node, "_ctx"):
        request.node._ctx = {}
    return cast("dict[str, Any]", request.node._ctx)


def _perform_request(request: Any, url: str, **service_mocks: AsyncMock) -> None:
    """Run the request against the real app with the supplied service seams.

    Each keyword value is an ``AsyncMock`` installed as the route module's
    service function for the duration of the call. ``get_or_create_engine`` is
    patched to a MagicMock so ``_analytics_session_factory`` builds a factory
    without touching the process-global engine (the admin path never opens it).
    """
    ctx = _ctx(request)
    client_gen = _make_client(
        role=ctx.get("role", "admin"),
        unauthenticated=ctx.get("unauthenticated", False),
        feature_disabled=ctx.get("feature_disabled", False),
        org_context=not ctx.get("no_org_context", False),
    )
    client = next(client_gen)
    engine_patch = patch("modulo.api.routes.analytics.get_or_create_engine", new=MagicMock())
    service_patches = {
        name: patch(f"modulo.api.routes.analytics.{name}", new=mock) for name, mock in service_mocks.items()
    }
    try:
        engine_patch.start()
        for p in service_patches.values():
            p.start()
        resp = client.get(url)
        request.node._resp = resp
    finally:
        for p in service_patches.values():
            p.stop()
        engine_patch.stop()
        client_gen.close()
        request.node._service_mocks = service_mocks


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("I am an authenticated org admin")
def _given_admin() -> None:
    """No-op — the default client uses an admin principal."""


@given("the analytics service reports one bucket with 3 runs on 2026-07-01")
def _given_query_result(request: Any) -> None:
    _ctx(request)["query_result"] = dict(_QUERY_RESULT)


@given("the analytics service reports a trigger_type dimension")
def _given_dimension_result(request: Any) -> None:
    _ctx(request)["query_result"] = dict(_DIMENSION_RESULT)


@given(parsers.parse("the analytics service rejects the query as invalid"))
def _given_invalid_query(request: Any) -> None:
    _ctx(request)["query_error"] = AnalyticsValidationError("date_from must be <= date_to")


@given("the analytics service is rate limited")
def _given_rate_limited(request: Any) -> None:
    _ctx(request)["query_error"] = AnalyticsRateLimitedError("Rate limit exceeded")


@given("the analytics service times out")
def _given_timeout(request: Any) -> None:
    _ctx(request)["query_error"] = AnalyticsQueryTimeoutError(
        "query exceeded timeout — reduce the date range"
    )


@given("the analytics_page feature is disabled")
def _given_feature_disabled(request: Any) -> None:
    _ctx(request)["feature_disabled"] = True


@given("I am a runner principal")
def _given_runner(request: Any) -> None:
    _ctx(request)["role"] = "runner"


@given("I am signed in without an organisation context")
def _given_no_org_context(request: Any) -> None:
    _ctx(request)["no_org_context"] = True


@given("an unauthenticated caller")
def _given_unauthenticated(request: Any) -> None:
    _ctx(request)["unauthenticated"] = True


@given("I filter on two distinct pipelines")
def _given_two_pipelines(request: Any) -> None:
    _ctx(request)["pipeline_ids"] = (_FILTER_A, _FILTER_B)


@given("the export service reports 2 fact rows with a total of 2")
def _given_export_result(request: Any) -> None:
    _ctx(request)["export_result"] = dict(_EXPORT_RESULT)


@given("the concurrency service reports one bucket with 2 active and 1 queued on 2026-07-01")
def _given_concurrency_result(request: Any) -> None:
    _ctx(request)["concurrency_result"] = dict(_CONCURRENCY_RESULT)


@given("the guardrail scorecard reports 1 blocked run")
def _given_guardrail_result(request: Any) -> None:
    _ctx(request)["guardrail_result"] = dict(_GUARDRAIL_RESULT)


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.re(r"I request GET (?P<url>[^ ]+)"))
def _when_request(request: Any, url: str) -> None:
    ctx = _ctx(request)
    for pipeline_id in ctx.get("pipeline_ids") or ():
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}pipeline_id={pipeline_id}"

    query_error = ctx.get("query_error")
    if query_error is not None:
        run_query_mock = AsyncMock(side_effect=query_error)
    else:
        query_result = ctx.get("query_result", dict(_QUERY_RESULT))
        run_query_mock = AsyncMock(return_value=query_result)

    mocks: dict[str, AsyncMock] = {}
    if "/analytics/query" in url:
        mocks["run_analytics_query"] = run_query_mock
    elif "/analytics/concurrency" in url:
        mocks["run_concurrency_query"] = AsyncMock(
            return_value=ctx.get("concurrency_result", dict(_CONCURRENCY_RESULT))
        )
    elif "/analytics/guardrails" in url:
        mocks["run_guardrail_scorecard"] = AsyncMock(
            return_value=ctx.get("guardrail_result", dict(_GUARDRAIL_RESULT))
        )
    elif "/analytics/export" in url:
        mocks["export_facts"] = AsyncMock(
            return_value=ctx.get("export_result", dict(_EXPORT_RESULT))
        )
    _perform_request(request, url, **mocks)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


def _resp(request: Any) -> Any:
    return request.node._resp


def _json(request: Any) -> Any:
    return _resp(request).json()


@then("the response status is 200")
def _then_200(request: Any) -> None:
    assert _resp(request).status_code == 200, f"Expected 200, got {_resp(request).status_code}: {_resp(request).text}"


@then("the response status is 401")
def _then_401(request: Any) -> None:
    assert _resp(request).status_code == 401, f"Expected 401, got {_resp(request).status_code}: {_resp(request).text}"


@then("the response status is 402")
def _then_402(request: Any) -> None:
    assert _resp(request).status_code == 402, f"Expected 402, got {_resp(request).status_code}: {_resp(request).text}"


@then("the response status is 403")
def _then_403(request: Any) -> None:
    assert _resp(request).status_code == 403, f"Expected 403, got {_resp(request).status_code}: {_resp(request).text}"


@then("the response status is 422")
def _then_422(request: Any) -> None:
    assert _resp(request).status_code == 422, f"Expected 422, got {_resp(request).status_code}: {_resp(request).text}"


@then("the response status is 429")
def _then_429(request: Any) -> None:
    assert _resp(request).status_code == 429, f"Expected 429, got {_resp(request).status_code}: {_resp(request).text}"


@then("the response status is 503")
def _then_503(request: Any) -> None:
    assert _resp(request).status_code == 503, f"Expected 503, got {_resp(request).status_code}: {_resp(request).text}"


def _body(request: Any) -> dict[str, Any]:
    body = _json(request)
    assert isinstance(body, dict), f"Expected a JSON object body, got {type(body)}"
    return body


@then(parsers.re(r"the response carries the day-series envelope with (?P<count>\d+) buckets?"))
def _then_envelope(request: Any, count: str) -> None:
    body = _body(request)
    for field in ("group_by", "dimension", "date_from", "date_to", "facts_freshness_hours", "facts_stale", "buckets"):
        assert field in body, f"analytics response missing '{field}': {sorted(body)}"
    assert isinstance(body["buckets"], list) and len(body["buckets"]) == int(count)


@then(parsers.parse("the bucket for {day} counts {count:d} runs"))
def _then_bucket_count(request: Any, day: str, count: int) -> None:
    buckets = _body(request)["buckets"]
    matching = [b for b in buckets if b.get("date") == day]
    assert matching, f"no bucket for {day}: {buckets}"
    assert matching[0]["count"] == count, f"{day} count={matching[0]['count']}, expected {count}"


@then("the query surfaced both pipeline ids to the analytics service")
def _then_pipeline_ids(request: Any) -> None:
    mock = request.node._service_mocks["run_analytics_query"]
    assert mock.await_count == 1, f"run_analytics_query awaited {mock.await_count} times"
    _, kwargs = mock.await_args
    params = kwargs["params"]
    assert tuple(params.pipeline_ids) == (_FILTER_A, _FILTER_B), (
        f"expected pipeline_ids ({_FILTER_A}, {_FILTER_B}), got {tuple(params.pipeline_ids)}"
    )


@then("the response echoes the trigger_type dimension")
def _then_echo_dimension(request: Any) -> None:
    assert _body(request)["dimension"] == "trigger_type"


@then(parsers.parse("the export response carries {count:d} items and a total of {total:d}"))
def _then_export_shape(request: Any, count: int, total: int) -> None:
    body = _body(request)
    for field in ("items", "total", "offset", "limit"):
        assert field in body, f"export response missing '{field}': {sorted(body)}"
    assert len(body["items"]) == count
    assert body["total"] == total


@then("the response is a CSV attachment")
def _then_csv_attachment(request: Any) -> None:
    resp = _resp(request)
    assert resp.headers["content-type"].startswith("text/csv"), resp.headers.get("content-type")
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert resp.content.startswith(b"run_id,run_date,team_id,team_name") or b"run_id" in resp.content.splitlines()[0]


@then("the concurrency response carries the pooled bucket series")
def _then_concurrency_shape(request: Any) -> None:
    body = _body(request)
    for field in ("group_by", "date_from", "date_to", "pool_reference", "buckets"):
        assert field in body, f"concurrency response missing '{field}': {sorted(body)}"
    assert len(body["buckets"]) == 1
    bucket = body["buckets"][0]
    for field in ("date", "key", "max_active", "avg_active", "max_queued", "avg_queued", "pool_reference"):
        assert field in bucket, f"concurrency bucket missing '{field}': {bucket}"


@then("the scorecard is labelled advisory_only")
def _then_guardrail_advisory(request: Any) -> None:
    body = _body(request)
    assert body["advisory_only"] is True
    scorecard_fields = (
        "date_from",
        "date_to",
        "scope",
        "fire_counts",
        "rates",
        "self_correction",
        "evasion_band_drift",
        "generated_at",
    )
    for field in scorecard_fields:
        assert field in body, f"scorecard missing '{field}': {sorted(body)}"
