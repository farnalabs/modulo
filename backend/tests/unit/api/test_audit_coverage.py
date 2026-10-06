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
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core import audit_coverage
from modulo.core.audit_coverage import audit_session, audited, audited_system, bind_audit_actor_source, bind_audit_org
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


# ---------------------------------------------------------------------------
# audited_system() — the actor-less variant (FAR-1516)
# ---------------------------------------------------------------------------


def _mini_system_client(
    *,
    fail_closed: bool = False,
    publish_org: bool = True,
    promote_actor_source: bool = False,
    handler_raises: bool = False,
) -> tuple[TestClient, AsyncMock]:
    """One-route app on the real ``audited_system()`` dependency (no DB/auth).

    The route stands in for the pre-auth / webhook handlers: it publishes the
    org (and optionally promotes ``actor_source``) exactly as the real routes
    do, then returns — or raises, to exercise the error arm.
    """
    api = FastAPI()
    audit_session_mock = _make_mock_session()

    async def override_audit_session() -> AsyncGenerator[AsyncMock, None]:
        yield audit_session_mock

    @api.post(
        "/system",
        dependencies=[
            Depends(
                audited_system(
                    "login_attempted",
                    "session",
                    actor_source="unauthenticated",
                    fail_closed=fail_closed,
                )
            )
        ],
    )
    async def system_route(request: Request) -> dict[str, str]:
        if publish_org:
            bind_audit_org(request, _ORG_ID)
        if promote_actor_source:
            bind_audit_actor_source(request, "signature_verified")
        if handler_raises:
            raise RuntimeError("handler exploded")
        return {"ok": "true"}

    api.dependency_overrides[audit_session] = override_audit_session
    return TestClient(api), audit_session_mock


def test_audited_system_records_a_system_event_without_an_actor() -> None:
    """One POST -> one chained event: NULL actor, SYSTEM marker, route's org."""
    client, audit_session_mock = _mini_system_client()
    append = AsyncMock()
    rls_org = AsyncMock()
    with (
        patch("modulo.core.audit_coverage.append_audit_event", new=append),
        patch("modulo.core.audit_coverage.set_rls_org", new=rls_org),
    ):
        resp = client.post("/system")

    assert resp.status_code == 200, resp.text
    assert append.call_count == 1, f"expected exactly one audit event, got {append.call_count}"

    kwargs = append.call_args.kwargs
    assert kwargs["org_id"] == _ORG_ID
    assert kwargs["event_type"] == "login_attempted"
    assert kwargs["resource_type"] == "session"
    # Provenance: never a fabricated actor — NULL column + explicit marker.
    assert kwargs["actor_user_id"] is None

    payload = kwargs["payload_json"]
    assert payload["actor"] == "system"
    assert payload["actor_source"] == "unauthenticated"
    assert payload["outcome"] == "success"
    assert payload["path"] == "/system"
    # RLS context is pinned to the published org for the fresh transaction.
    assert rls_org.call_args.args[1] == _ORG_ID
    assert audit_session_mock.begin.call_count == 1


def test_audited_system_records_the_promoted_actor_source() -> None:
    """A route that promotes provenance records the promotion, not the default."""
    client, _ = _mini_system_client(promote_actor_source=True)
    append = AsyncMock()
    with patch("modulo.core.audit_coverage.append_audit_event", new=append):
        resp = client.post("/system")

    assert resp.status_code == 200, resp.text
    assert append.call_count == 1
    assert append.call_args.kwargs["payload_json"]["actor_source"] == "signature_verified"


def test_audited_system_skips_and_logs_when_no_org_was_published(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No published org -> nowhere honest to record: logged, never invented."""
    client, _ = _mini_system_client(publish_org=False)
    append = AsyncMock()
    with patch("modulo.core.audit_coverage.append_audit_event", new=append):
        resp = client.post("/system")

    assert resp.status_code == 200, resp.text
    append.assert_not_called()
    messages = [record.getMessage() for record in caplog.records]
    assert "audit_coverage.login_attempted.no_org_context" in messages


def test_audited_system_fail_closed_reraises_an_append_failure() -> None:
    """fail_closed=True: the append error reaches the caller instead of a log."""
    client, _ = _mini_system_client(fail_closed=True)
    failing = AsyncMock(side_effect=RuntimeError("audit store is down"))
    with (
        patch("modulo.core.audit_coverage.append_audit_event", new=failing),
        pytest.raises(RuntimeError, match="audit store is down"),
    ):
        client.post("/system")


def test_audited_system_records_a_handler_error_without_masking_it() -> None:
    """A handler crash is audited (outcome=error) and still reaches the caller."""
    client, _ = _mini_system_client(handler_raises=True)
    append = AsyncMock()
    with (
        patch("modulo.core.audit_coverage.append_audit_event", new=append),
        pytest.raises(RuntimeError, match="handler exploded"),
    ):
        client.post("/system")

    assert append.call_count == 1
    assert append.call_args.kwargs["payload_json"]["outcome"] == "error"


def test_audited_system_rejects_blank_arguments() -> None:
    """Fails at decoration time, not silently at request time."""
    with pytest.raises(ValueError, match="event_type"):
        audited_system("  ", "session", actor_source="pre_auth")
    with pytest.raises(ValueError, match="resource_type"):
        audited_system("login_attempted", "", actor_source="pre_auth")
    with pytest.raises(ValueError, match="actor_source"):
        audited_system("login_attempted", "session", actor_source="  ")


def test_bind_audit_org_never_clobbers_a_published_org_with_an_empty_value() -> None:
    """None / "" mean \"not known yet\" — they must not unpublish a good org."""
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""})
    bind_audit_org(request, _ORG_ID)
    bind_audit_org(request, None)
    bind_audit_org(request, "")
    assert request.state.audit_org_id == str(_ORG_ID)


# ---------------------------------------------------------------------------
# Provenance binding + resolution helpers (FAR-1516)
# ---------------------------------------------------------------------------


def _bare_request() -> Request:
    """A minimal real ``Request`` whose ``state`` the helpers can publish onto."""
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""})


def test_bind_audit_actor_source_rejects_a_blank_or_non_string_source() -> None:
    """An empty/missing provenance basis is a programming error, not a silent no-op."""
    request = _bare_request()
    with pytest.raises(ValueError, match="actor_source"):
        bind_audit_actor_source(request, "")
    with pytest.raises(ValueError, match="actor_source"):
        bind_audit_actor_source(request, "   ")
    with pytest.raises(ValueError, match="actor_source"):
        bind_audit_actor_source(request, None)  # type: ignore[arg-type]


def test_resolve_audit_org_reads_only_a_honest_org(caplog: pytest.LogCaptureFixture) -> None:
    """Every published spelling is resolved; anything else is refused, never coerced."""
    request = _bare_request()
    # Nothing published -> unattributed; never invent an org.
    assert audit_coverage._resolve_audit_org(request) is None
    # A raw uuid.UUID is accepted directly.
    request.state.audit_org_id = _ORG_ID
    assert audit_coverage._resolve_audit_org(request) == _ORG_ID
    # A UUID string round-trips.
    request.state.audit_org_id = str(_ORG_ID)
    assert audit_coverage._resolve_audit_org(request) == _ORG_ID
    # A non-string, non-UUID value is refused.
    request.state.audit_org_id = 12345
    assert audit_coverage._resolve_audit_org(request) is None
    # A malformed UUID string is logged and refused.
    request.state.audit_org_id = "not-a-uuid"
    assert audit_coverage._resolve_audit_org(request) is None
    assert any("not a UUID" in record.getMessage() for record in caplog.records)


def test_resolve_actor_source_falls_back_on_a_blank_or_non_string_value() -> None:
    """Only a non-empty string is a real promotion; everything else is the default."""
    request = _bare_request()
    assert audit_coverage._resolve_actor_source(request, "declared") == "declared"
    request.state.audit_actor_source = "signature_verified"
    assert audit_coverage._resolve_actor_source(request, "declared") == "signature_verified"
    request.state.audit_actor_source = "   "
    assert audit_coverage._resolve_actor_source(request, "declared") == "declared"
    request.state.audit_actor_source = 123
    assert audit_coverage._resolve_actor_source(request, "declared") == "declared"


async def test_emit_system_propagates_a_cancelled_append() -> None:
    """A cancellation is never swallowed by the audit failure policy."""
    session = _make_mock_session()
    with (
        patch(
            "modulo.core.audit_coverage._append_system_or_raise",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await audit_coverage._emit_system(
            session=session,
            org_id=_ORG_ID,
            event_type="login_attempted",
            resource_type="session",
            payload={"outcome": "success"},
            fail_closed=False,
        )


async def test_emit_system_fail_open_logs_and_swallows(caplog: pytest.LogCaptureFixture) -> None:
    """Default policy: an append failure is logged under its stable key, not raised."""
    session = _make_mock_session()
    with patch(
        "modulo.core.audit_coverage._append_system_or_raise",
        new=AsyncMock(side_effect=RuntimeError("audit store is down")),
    ):
        await audit_coverage._emit_system(
            session=session,
            org_id=_ORG_ID,
            event_type="login_attempted",
            resource_type="session",
            payload={"outcome": "success"},
            fail_closed=False,
        )
    messages = [record.getMessage() for record in caplog.records]
    assert "audit_coverage.login_attempted.append_failed" in messages, messages


async def test_audited_system_dependency_rethrows_cancellation_at_yield() -> None:
    """A cancelled request re-raises at the yield — no system event after cancel."""
    dep = audited_system("login_attempted", "session", actor_source="unauthenticated")
    gen = dep(_bare_request(), _make_mock_session())
    assert await gen.asend(None) is None
    with pytest.raises(asyncio.CancelledError):
        await gen.athrow(asyncio.CancelledError())
