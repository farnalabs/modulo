"""Unit tests for the centralised audit wrapper (FAR-1472).

Driven through real FastAPI requests — the real app, the real pilot route
(``POST /api/v1/parameter-schemas``) and the real ``audited()`` dependency —
with only the DB seams stubbed (session, append), the same way the rest of
this package stubs them. ``tests/unit/api/conftest.py`` supplies the settings
env and monkeypatches ``_verify_identity`` so no test here opens a socket.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.audit_coverage import audit_session, audited
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_CREATE_PATH = "/api/v1/parameter-schemas"
_CREATE_EVENT = "parameter_schema_created"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    """Contract-correct session double (sync result objects, explicit stubs)."""
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
        is_system_admin=False,
    )


def _make_schema() -> MagicMock:
    schema = MagicMock()
    schema.id = uuid.uuid4()
    schema.organisation_id = _ORG_ID
    schema.name = "Created"
    schema.description = None
    schema.version = 1
    schema.parameters = []
    schema.account_id = _USER_ID
    schema.created_at = _NOW
    schema.updated_at = _NOW
    return schema


@pytest.fixture
def route_session() -> AsyncMock:
    """The request's own session, as ``get_db_session`` would yield it."""
    return _make_mock_session()


@pytest.fixture
def audit_session_mock() -> AsyncMock:
    """The FRESH session the wrapper writes its event on (``audit_session``)."""
    return _make_mock_session()


@pytest.fixture
def client(route_session: AsyncMock, audit_session_mock: AsyncMock) -> Generator[TestClient, None, None]:
    """Real app + pilot route, DB and auth seams stubbed like sibling suites."""

    async def override_route_session() -> AsyncGenerator[AsyncMock, None]:
        yield route_session

    async def override_audit_session() -> AsyncGenerator[AsyncMock, None]:
        yield audit_session_mock

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_route_session
    app.dependency_overrides[audit_session] = override_audit_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_plan_context] = lambda: MagicMock()
    yield TestClient(app)
    app.dependency_overrides.clear()


def _route_write_patches(order: list[str]) -> tuple[object, object, object]:
    """Patch the pilot route's create + RLS calls; record the handler's position."""

    async def _handler(*_a: object, **_k: object) -> MagicMock:
        order.append("handler")
        return _make_schema()

    return (
        patch("modulo.api.routes.parameter_schemas.create_schema", new=AsyncMock(side_effect=_handler)),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new=AsyncMock()),
    )


def test_successful_mutation_emits_exactly_one_audit_event(client: TestClient, audit_session_mock: AsyncMock) -> None:
    """One POST -> exactly one chained audit event with actor, org and path."""
    order: list[str] = []
    captured = AsyncMock(side_effect=lambda *_a, **_k: order.append("audit"))
    create, rls_org, rls_user = _route_write_patches(order)
    with (
        patch("modulo.core.audit_coverage.append_audit_event_isolated", new=captured),
        create,
        rls_org,
        rls_user,
    ):
        resp = client.post(_CREATE_PATH, json={"name": "Created"})

    assert resp.status_code == 201, resp.text
    assert captured.call_count == 1, f"expected exactly one audit event, got {captured.call_count}"

    args, kwargs = captured.call_args
    session, principal = args[0], args[1]
    assert session is audit_session_mock, "the event must be written on the fresh audit session"
    assert principal.organisation_id == _ORG_ID
    assert principal.account_id == _USER_ID
    assert kwargs["event_type"] == _CREATE_EVENT
    assert kwargs["resource_type"] == "parameter_schema"
    assert kwargs["payload"] == {"http_method": "POST", "path": _CREATE_PATH, "outcome": "success"}
    assert kwargs["log_key"] == f"audit_coverage.{_CREATE_EVENT}.append_failed"
    # The business write completes BEFORE the audit append runs.
    assert order == ["handler", "audit"]


def test_reads_emit_no_audit_event(client: TestClient) -> None:
    """GET is not a mutation: the ratchet only ever annotates writes."""
    captured = AsyncMock()
    schema = _make_schema()
    with (
        patch("modulo.core.audit_coverage.append_audit_event_isolated", new=captured),
        patch("modulo.api.routes.parameter_schemas.get_schema", new=AsyncMock(return_value=schema)),
        patch("modulo.api.routes.parameter_schemas.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.parameter_schemas.set_rls_user_context", new=AsyncMock()),
    ):
        resp = client.get(_CREATE_PATH + f"/{schema.id}")

    assert resp.status_code == 200, resp.text
    captured.assert_not_called()


def test_fail_open_logs_and_does_not_raise(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """Default policy: a write failure is logged, the response still succeeds."""
    order: list[str] = []
    create, rls_org, rls_user = _route_write_patches(order)
    failing = AsyncMock(side_effect=RuntimeError("audit store is down"))
    with (
        patch("modulo.core.audit_coverage.append_audit_event_isolated", new=failing),
        create,
        rls_org,
        rls_user,
    ):
        resp = client.post(_CREATE_PATH, json={"name": "Created"})

    assert resp.status_code == 201, resp.text
    messages = [record.getMessage() for record in caplog.records]
    assert f"audit_coverage.{_CREATE_EVENT}.append_failed" in messages, messages


def test_fail_open_logs_a_real_append_failure_from_the_shared_helper(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Production-shaped failure: the chained append itself blows up inside the helper."""
    order: list[str] = []
    create, rls_org, rls_user = _route_write_patches(order)
    with (
        patch("modulo.core.audit_logger.append_audit_event", new=AsyncMock(side_effect=SQLAlchemyError("boom"))),
        create,
        rls_org,
        rls_user,
    ):
        resp = client.post(_CREATE_PATH, json={"name": "Created"})

    assert resp.status_code == 201, resp.text
    messages = [record.getMessage() for record in caplog.records]
    assert f"audit_coverage.{_CREATE_EVENT}.append_failed" in messages, messages


def _mini_client() -> TestClient:
    """One-route-per-case app built on the real wrapper (no handle_db_errors in between)."""
    api = FastAPI()
    audit_session_mock = _make_mock_session()

    async def override_principal() -> TenantPrincipal:
        return _make_principal()

    async def override_audit_session() -> AsyncGenerator[AsyncMock, None]:
        yield audit_session_mock

    @api.post(
        "/loose", dependencies=[Depends(audited("widget_created", "widget", principal_dep=get_current_tenant_user))]
    )
    async def loose() -> dict[str, str]:
        return {"ok": "true"}

    @api.post(
        "/strict",
        dependencies=[
            Depends(audited("widget_created", "widget", principal_dep=get_current_tenant_user, fail_closed=True))
        ],
    )
    async def strict() -> dict[str, str]:
        return {"ok": "true"}

    @api.post(
        "/crash",
        dependencies=[
            Depends(audited("widget_created", "widget", principal_dep=get_current_tenant_user, fail_closed=True))
        ],
    )
    async def crash() -> dict[str, str]:
        raise RuntimeError("handler exploded")

    @api.post(
        "/conflict",
        dependencies=[
            Depends(audited("widget_created", "widget", principal_dep=get_current_tenant_user, fail_closed=True))
        ],
    )
    async def conflict() -> dict[str, str]:
        raise HTTPException(status_code=409, detail="conflict")

    api.dependency_overrides[get_current_tenant_user] = override_principal
    api.dependency_overrides[audit_session] = override_audit_session
    return TestClient(api)


def test_fail_closed_reraises_a_write_failure() -> None:
    """fail_closed=True: the append error reaches the caller instead of a log line."""
    client = _mini_client()
    failing = AsyncMock(side_effect=RuntimeError("audit store is down"))
    with (
        patch("modulo.core.audit_coverage.append_audit_event", new=failing),
        pytest.raises(RuntimeError, match="audit store is down"),
    ):
        client.post("/strict")


def test_fail_closed_succeeds_when_the_append_succeeds() -> None:
    """fail_closed is not a permanent failure mode — a good write still returns 200."""
    client = _mini_client()
    ok = AsyncMock()
    with patch("modulo.core.audit_coverage.append_audit_event", new=ok):
        resp = client.post("/strict")
    assert resp.status_code == 200, resp.text
    assert ok.call_count == 1


def test_fail_open_route_never_raises() -> None:
    """The default policy on a standalone route: append failure, still 200."""
    client = _mini_client()
    failing = AsyncMock(side_effect=RuntimeError("audit store is down"))
    with patch("modulo.core.audit_coverage.append_audit_event_isolated", new=failing):
        resp = client.post("/loose")
    assert resp.status_code == 200, resp.text


def test_handler_http_error_is_recorded_without_masking_the_error() -> None:
    """A handler error is audited (outcome=error) but never replaced by an audit error."""
    client = _mini_client()
    recording = AsyncMock()
    with patch("modulo.core.audit_coverage.append_audit_event_isolated", new=recording):
        resp = client.post("/conflict")

    assert resp.status_code == 409, resp.text
    assert recording.call_count == 1
    assert recording.call_args.kwargs["payload"]["outcome"] == "error"
    assert recording.call_args.kwargs["event_type"] == "widget_created"


def test_unhandled_handler_error_is_not_swallowed_by_the_audit_path() -> None:
    """An unexpected handler crash still surfaces after the audit attempt."""
    client = _mini_client()
    with (
        patch("modulo.core.audit_coverage.append_audit_event_isolated", new=AsyncMock()),
        pytest.raises(RuntimeError, match="handler exploded"),
    ):
        client.post("/crash")


def test_audited_rejects_a_blank_event_type() -> None:
    """Fails at decoration time, not silently at request time."""
    with pytest.raises(ValueError, match="event_type"):
        audited("  ", "widget", principal_dep=get_current_tenant_user)
    with pytest.raises(ValueError, match="resource_type"):
        audited("widget_created", "", principal_dep=get_current_tenant_user)


def _drive_to_yield() -> AsyncGenerator[None, None]:
    """Build the real ``audited()`` dependency and advance it to its yield.

    Driving the async generator directly is the only way to reach its teardown
    arms: ``TestClient`` always finishes the request, so a cancelled-request
    teardown (``GeneratorExit`` / ``CancelledError``) never fires through HTTP.
    """
    dep = audited("widget_created", "widget", principal_dep=get_current_tenant_user)
    return dep(MagicMock(), _make_mock_session(), _make_principal())


async def test_dependency_propagates_append_cancellation() -> None:
    """A cancelled audit append is re-raised, never logged and swallowed."""
    gen = _drive_to_yield()
    assert await gen.asend(None) is None
    with (
        patch(
            "modulo.core.audit_coverage.append_audit_event_isolated",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await gen.asend(None)


async def test_dependency_rethrows_cancellation_at_yield() -> None:
    """A cancelled request re-raises at the yield — no audit write after cancel."""
    gen = _drive_to_yield()
    assert await gen.asend(None) is None
    with pytest.raises(asyncio.CancelledError):
        await gen.athrow(asyncio.CancelledError())
