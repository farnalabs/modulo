"""Tests for the admin org work-item agent-minting flag endpoint (FAR-795 Slice A)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes import admin as admin_routes
from modulo.api.routes.admin import UpdateWorkItemAgentMintingRequest
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.runtime_config.org_flags import FLAG_WORK_ITEM_AGENT_MINTING_ENABLED, clear_org_flag_cache

ORG_ID = uuid4()
USER_ID = uuid4()


def _admin(role: str = "admin") -> TenantPrincipal:
    return TenantPrincipal(
        username="admin@test",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role=role,
    )


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_org_flag_cache()
    yield
    clear_org_flag_cache()


@pytest.fixture
def org_settings():
    return {}


@pytest.fixture
def mock_session(org_settings):
    """Mock session whose org row exposes a mutable settings_json."""
    org = MagicMock()
    org.id = ORG_ID
    org.settings_json = org_settings

    result = MagicMock()
    result.scalar_one_or_none.return_value = org

    session = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    nested_cm = MagicMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)
    session.execute.return_value = result
    session.flush = AsyncMock()
    return session


def _begin_cm() -> MagicMock:
    """An ``async with session.begin()`` context manager backed by mocks."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _direct_session() -> AsyncMock:
    """A standalone mock session wired with a no-op ``begin()`` for direct calls.

    The org row exposes a mutable ``settings_json`` so the real ``set_org_flag``
    write path succeeds and the call reaches the audit-event block.
    """
    org = MagicMock()
    org.id = ORG_ID
    org.settings_json = {}
    result = MagicMock()
    result.scalar_one_or_none.return_value = org
    session = AsyncMock()
    session.begin = _begin_cm()
    session.execute.return_value = result
    session.flush = AsyncMock()
    return session


def _make_client(mock_session, role: str):
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True

    async def _override_tenant() -> TenantPrincipal:
        return _admin(role)

    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant_user] = _override_tenant
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def client_admin(mock_session):
    client = _make_client(mock_session, role="admin")
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def client_viewer(mock_session):
    client = _make_client(mock_session, role="viewer")
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def client_operator(mock_session):
    client = _make_client(mock_session, role="operator")
    yield client
    app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_default_off(client_admin):
    """An org that never configured the flag reads OFF (fail-closed default)."""
    resp = await client_admin.get("/api/v1/admin/org/work-item-agent-minting")
    assert resp.status_code == 200
    assert resp.json() == {"work_item_agent_minting_enabled": False}


@pytest.mark.anyio
async def test_get_true_when_enabled(client_admin, org_settings):
    org_settings[FLAG_WORK_ITEM_AGENT_MINTING_ENABLED] = True
    resp = await client_admin.get("/api/v1/admin/org/work-item-agent-minting")
    assert resp.status_code == 200
    assert resp.json() == {"work_item_agent_minting_enabled": True}


@pytest.mark.anyio
async def test_string_true_reads_off(client_admin, org_settings):
    """Only a JSON bool true enables; a string 'true' is never honoured."""
    org_settings[FLAG_WORK_ITEM_AGENT_MINTING_ENABLED] = "true"
    resp = await client_admin.get("/api/v1/admin/org/work-item-agent-minting")
    assert resp.status_code == 200
    assert resp.json() == {"work_item_agent_minting_enabled": False}


@pytest.mark.anyio
async def test_put_sets_flag(client_admin, mock_session):
    resp = await client_admin.put(
        "/api/v1/admin/org/work-item-agent-minting",
        json={"work_item_agent_minting_enabled": True},
    )
    assert resp.status_code == 200
    assert resp.json() == {"work_item_agent_minting_enabled": True}
    org = mock_session.execute.return_value.scalar_one_or_none.return_value
    assert org.settings_json[FLAG_WORK_ITEM_AGENT_MINTING_ENABLED] is True
    assert "FOR UPDATE" in str(mock_session.execute.await_args_list[0].args[0])


@pytest.mark.anyio
async def test_put_merge_preserves_other_settings(client_admin, mock_session):
    org = mock_session.execute.return_value.scalar_one_or_none.return_value
    org.settings_json = {"license_key": "license-abc", "sandbox_concurrency_limit": 4}
    resp = await client_admin.put(
        "/api/v1/admin/org/work-item-agent-minting",
        json={"work_item_agent_minting_enabled": True},
    )
    assert resp.status_code == 200
    assert org.settings_json["license_key"] == "license-abc"
    assert org.settings_json["sandbox_concurrency_limit"] == 4
    assert org.settings_json[FLAG_WORK_ITEM_AGENT_MINTING_ENABLED] is True


@pytest.mark.anyio
@pytest.mark.parametrize("value", [1, "true", "yes"])
async def test_put_rejects_non_bool(client_admin, value):
    resp = await client_admin.put(
        "/api/v1/admin/org/work-item-agent-minting",
        json={"work_item_agent_minting_enabled": value},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_viewer_forbidden_on_get(client_viewer):
    resp = await client_viewer.get("/api/v1/admin/org/work-item-agent-minting")
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_operator_forbidden_on_put(client_operator):
    """Admin-only: operators are not trusted to arm agent minting."""
    resp = await client_operator.put(
        "/api/v1/admin/org/work-item-agent-minting",
        json={"work_item_agent_minting_enabled": True},
    )
    assert resp.status_code == 403


def _raise(exc: Exception):
    async def _raises(*_args, **_kwargs):
        raise exc

    return _raises


@pytest.mark.anyio
async def test_get_programming_error_returns_501(client_admin):
    with patch(
        "modulo.api.routes.admin.read_org_flag",
        new=_raise(ProgrammingError("stmt", {}, Exception("missing table"))),
    ):
        resp = await client_admin.get("/api/v1/admin/org/work-item-agent-minting")
    assert resp.status_code == 501


@pytest.mark.anyio
async def test_get_sqlalchemy_error_returns_503(client_admin):
    with patch(
        "modulo.api.routes.admin.read_org_flag",
        new=_raise(SQLAlchemyError("mock", {}, "")),
    ):
        resp = await client_admin.get("/api/v1/admin/org/work-item-agent-minting")
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_put_sqlalchemy_error_returns_503(client_admin, mock_session):
    """A DB failure during the write surfaces 503 — never a silent partial."""
    mock_session.execute.side_effect = SQLAlchemyError("db outage")
    resp = await client_admin.put(
        "/api/v1/admin/org/work-item-agent-minting",
        json={"work_item_agent_minting_enabled": True},
    )
    assert resp.status_code == 503


# ── GET error-surface branches ────────────────────────────────────────────


@pytest.mark.anyio
async def test_get_cancelled_error_propagates():
    """A CancelledError is never swallowed — it must propagate to the caller."""
    user = _admin("admin")
    session = _direct_session()
    with (
        patch.object(admin_routes, "set_rls_org", new=AsyncMock()),
        patch.object(admin_routes, "read_org_flag", new=_raise(asyncio.CancelledError())),
        pytest.raises(asyncio.CancelledError),
    ):
        await admin_routes.admin_get_work_item_agent_minting(current_user=user, session=session)


@pytest.mark.anyio
async def test_get_unexpected_error_returns_500(client_admin):
    with patch(
        "modulo.api.routes.admin.read_org_flag",
        new=_raise(ValueError("boom")),
    ):
        resp = await client_admin.get("/api/v1/admin/org/work-item-agent-minting")
    assert resp.status_code == 500


# ── PUT error-surface branches ────────────────────────────────────────────


@pytest.mark.anyio
async def test_put_missing_org_returns_404(client_admin, mock_session):
    """An org that vanished between read and write surfaces 404, not 500."""
    mock_session.execute.return_value.scalar_one_or_none.return_value = None
    resp = await client_admin.put(
        "/api/v1/admin/org/work-item-agent-minting",
        json={"work_item_agent_minting_enabled": True},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_put_integrity_error_returns_409(client_admin):
    with patch(
        "modulo.api.routes.admin.set_org_flag",
        new=_raise(IntegrityError("stmt", {}, Exception("dup"))),
    ):
        resp = await client_admin.put(
            "/api/v1/admin/org/work-item-agent-minting",
            json={"work_item_agent_minting_enabled": True},
        )
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_put_programming_error_returns_501(client_admin):
    with patch(
        "modulo.api.routes.admin.set_org_flag",
        new=_raise(ProgrammingError("stmt", {}, Exception("missing table"))),
    ):
        resp = await client_admin.put(
            "/api/v1/admin/org/work-item-agent-minting",
            json={"work_item_agent_minting_enabled": True},
        )
    assert resp.status_code == 501


@pytest.mark.anyio
async def test_put_http_exception_reraises():
    """A pre-formed HTTPException (e.g. from a dependency) is re-raised verbatim."""
    user = _admin("admin")
    session = _direct_session()
    with (
        patch.object(admin_routes, "set_rls_org", new=AsyncMock()),
        patch.object(admin_routes, "set_org_flag", new=_raise(HTTPException(status_code=418, detail="teapot"))),
        pytest.raises(HTTPException) as exc,
    ):
        await admin_routes.admin_update_work_item_agent_minting(
            req=UpdateWorkItemAgentMintingRequest(work_item_agent_minting_enabled=True),
            current_user=user,
            session=session,
        )
    assert exc.value.status_code == 418


@pytest.mark.anyio
async def test_put_unexpected_error_returns_500(client_admin):
    with patch(
        "modulo.api.routes.admin.set_org_flag",
        new=_raise(ValueError("boom")),
    ):
        resp = await client_admin.put(
            "/api/v1/admin/org/work-item-agent-minting",
            json={"work_item_agent_minting_enabled": True},
        )
    assert resp.status_code == 500


@pytest.mark.anyio
async def test_put_cancelled_error_propagates():
    user = _admin("admin")
    session = _direct_session()
    with (
        patch.object(admin_routes, "set_rls_org", new=AsyncMock()),
        patch.object(admin_routes, "set_org_flag", new=_raise(asyncio.CancelledError())),
        pytest.raises(asyncio.CancelledError),
    ):
        await admin_routes.admin_update_work_item_agent_minting(
            req=UpdateWorkItemAgentMintingRequest(work_item_agent_minting_enabled=True),
            current_user=user,
            session=session,
        )


# ── PUT audit-event fire-and-forget branches (flag write already committed) ─


@pytest.mark.anyio
async def test_put_audit_integrity_error_swallowed(client_admin):
    with patch(
        "modulo.core.audit_logger.append_audit_event",
        new=_raise(IntegrityError("stmt", {}, Exception("dup"))),
    ):
        resp = await client_admin.put(
            "/api/v1/admin/org/work-item-agent-minting",
            json={"work_item_agent_minting_enabled": True},
        )
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_put_audit_programming_error_swallowed(client_admin):
    with patch(
        "modulo.core.audit_logger.append_audit_event",
        new=_raise(ProgrammingError("stmt", {}, Exception("missing table"))),
    ):
        resp = await client_admin.put(
            "/api/v1/admin/org/work-item-agent-minting",
            json={"work_item_agent_minting_enabled": True},
        )
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_put_audit_sqlalchemy_error_swallowed(client_admin):
    with patch(
        "modulo.core.audit_logger.append_audit_event",
        new=_raise(SQLAlchemyError("mock", {}, "")),
    ):
        resp = await client_admin.put(
            "/api/v1/admin/org/work-item-agent-minting",
            json={"work_item_agent_minting_enabled": True},
        )
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_put_audit_unexpected_error_swallowed(client_admin):
    with patch(
        "modulo.core.audit_logger.append_audit_event",
        new=_raise(ValueError("boom")),
    ):
        resp = await client_admin.put(
            "/api/v1/admin/org/work-item-agent-minting",
            json={"work_item_agent_minting_enabled": True},
        )
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_put_audit_cancelled_error_propagates():
    user = _admin("admin")
    session = _direct_session()
    with (
        patch.object(admin_routes, "set_rls_org", new=AsyncMock()),
        patch.object(admin_routes, "set_rls_user_context", new=AsyncMock()),
        patch(
            "modulo.core.audit_logger.append_audit_event",
            new=_raise(asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await admin_routes.admin_update_work_item_agent_minting(
            req=UpdateWorkItemAgentMintingRequest(work_item_agent_minting_enabled=True),
            current_user=user,
            session=session,
        )
