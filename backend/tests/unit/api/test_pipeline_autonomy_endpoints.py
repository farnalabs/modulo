"""FAR-1175 (ADR 043 S1): manual autonomy demote/promote REST surface.

Covers the direction validation (demote must lower, promote must raise and never
exceed the ceiling), the admin/owner RBAC gate, and the audit events
(``pipeline.autonomy_demoted`` / ``pipeline.autonomy_promotion_decided``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
_OWNER_TEAM_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
_NOW = datetime(2026, 10, 10, tzinfo=UTC)
_ROUTES = "modulo.api.routes.pipelines."


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _pipeline(
    *,
    earned: str | None = "manual_approval",
    default: str = "manual_approval",
    ceiling: str | None = "fully_autonomous",
    owner_team_id: uuid.UUID | None = None,
) -> MagicMock:
    p = MagicMock()
    p.id = _PIPELINE_ID
    p.organisation_id = _ORG_ID
    p.owner_team_id = owner_team_id
    p.default_autonomy_level = default
    p.max_autonomy_level = ceiling
    p.earned_autonomy_level = earned
    p.earned_autonomy_updated_at = _NOW
    return p


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    community_plan = MagicMock()
    community_plan.feature_enabled.return_value = False
    community_plan.list_enabled_features.return_value = []
    app.dependency_overrides[get_plan_context] = lambda: community_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def _patches(pipeline: MagicMock, *, in_owner_team: bool = False, **extra: object) -> list:
    return [
        patch(f"{_ROUTES}_set_rls_context"),
        patch(f"{_ROUTES}get_pipeline", new=AsyncMock(return_value=pipeline)),
        patch(f"{_ROUTES}is_account_in_team", new=AsyncMock(return_value=in_owner_team)),
        *(patch(f"{_ROUTES}{name}", new=value) for name, value in extra.items()),
    ]


def _enter(stack: list) -> list:
    return [p.start() for p in stack]


class TestDemote:
    def teardown_method(self) -> None:
        patch.stopall()

    def test_admin_demote_lowers_and_audits(self, client: TestClient) -> None:
        pipeline = _pipeline(earned="fully_autonomous")
        set_level = AsyncMock(return_value=pipeline)
        audit = AsyncMock()
        _enter(_patches(pipeline, set_earned_autonomy_level=set_level, append_audit_event=audit))

        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/autonomy/demote",
            json={"level": "manual_approval", "reason": "defect escaped"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["earned_autonomy_level"] == "manual_approval"
        assert body["previous_level"] == "fully_autonomous"
        assert body["event_type"] == "pipeline.autonomy_demoted"
        assert set_level.await_args.kwargs["level"] == "manual_approval"
        kwargs = audit.await_args.kwargs
        assert kwargs["event_type"] == "pipeline.autonomy_demoted"
        assert kwargs["payload_json"]["previous_level"] == "fully_autonomous"
        assert kwargs["payload_json"]["new_level"] == "manual_approval"
        assert kwargs["payload_json"]["reason"] == "defect escaped"
        assert kwargs["actor_user_id"] == _USER_ID

    def test_demote_not_below_current_is_rejected(self, client: TestClient) -> None:
        pipeline = _pipeline(earned="manual_approval")
        set_level = AsyncMock(return_value=pipeline)
        audit = AsyncMock()
        _enter(_patches(pipeline, set_earned_autonomy_level=set_level, append_audit_event=audit))

        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/autonomy/demote",
            json={"level": "notify_on_complete"},
        )

        assert resp.status_code == 422, resp.text
        set_level.assert_not_awaited()
        audit.assert_not_awaited()

    def test_invalid_level_is_rejected(self, client: TestClient) -> None:
        pipeline = _pipeline()
        set_level = AsyncMock(return_value=pipeline)
        _enter(_patches(pipeline, set_earned_autonomy_level=set_level))

        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/autonomy/demote",
            json={"level": "bogus"},
        )

        assert resp.status_code == 422, resp.text
        set_level.assert_not_awaited()


class TestPromote:
    def teardown_method(self) -> None:
        patch.stopall()

    def test_admin_promote_raises_within_ceiling_and_audits(self, client: TestClient) -> None:
        pipeline = _pipeline(earned="manual_approval", ceiling="fully_autonomous")
        set_level = AsyncMock(return_value=pipeline)
        audit = AsyncMock()
        _enter(_patches(pipeline, set_earned_autonomy_level=set_level, append_audit_event=audit))

        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/autonomy/promote",
            json={"level": "notify_on_complete", "reason": "clean streak"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["earned_autonomy_level"] == "notify_on_complete"
        assert body["previous_level"] == "manual_approval"
        assert body["event_type"] == "pipeline.autonomy_promotion_decided"
        kwargs = audit.await_args.kwargs
        assert kwargs["event_type"] == "pipeline.autonomy_promotion_decided"
        assert kwargs["payload_json"]["new_level"] == "notify_on_complete"

    def test_promote_above_ceiling_is_rejected(self, client: TestClient) -> None:
        pipeline = _pipeline(earned="manual_approval", ceiling="notify_on_complete")
        set_level = AsyncMock(return_value=pipeline)
        audit = AsyncMock()
        _enter(_patches(pipeline, set_earned_autonomy_level=set_level, append_audit_event=audit))

        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/autonomy/promote",
            json={"level": "fully_autonomous"},
        )

        assert resp.status_code == 422, resp.text
        set_level.assert_not_awaited()
        audit.assert_not_awaited()

    def test_promote_not_above_current_is_rejected(self, client: TestClient) -> None:
        pipeline = _pipeline(earned="notify_on_complete", ceiling="fully_autonomous")
        set_level = AsyncMock(return_value=pipeline)
        _enter(_patches(pipeline, set_earned_autonomy_level=set_level))

        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/autonomy/promote",
            json={"level": "manual_approval"},
        )

        assert resp.status_code == 422, resp.text
        set_level.assert_not_awaited()


class TestRbac:
    def teardown_method(self) -> None:
        patch.stopall()

    def _as_operator(self, *, team_id: uuid.UUID | None = None) -> None:
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="op",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="operator",
            team_id=team_id,
        )

    def test_operator_without_owner_team_is_forbidden(self, client: TestClient) -> None:
        self._as_operator()
        pipeline = _pipeline(earned="fully_autonomous", owner_team_id=None)
        set_level = AsyncMock(return_value=pipeline)
        audit = AsyncMock()
        _enter(_patches(pipeline, set_earned_autonomy_level=set_level, append_audit_event=audit))

        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/autonomy/demote",
            json={"level": "manual_approval"},
        )

        assert resp.status_code == 403, resp.text
        set_level.assert_not_awaited()
        audit.assert_not_awaited()

    def test_operator_of_owner_team_is_allowed(self, client: TestClient) -> None:
        self._as_operator(team_id=_OWNER_TEAM_ID)
        pipeline = _pipeline(earned="fully_autonomous", owner_team_id=_OWNER_TEAM_ID)
        set_level = AsyncMock(return_value=pipeline)
        audit = AsyncMock()
        _enter(
            _patches(
                pipeline,
                in_owner_team=True,
                set_earned_autonomy_level=set_level,
                append_audit_event=audit,
            )
        )

        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/autonomy/demote",
            json={"level": "manual_approval"},
        )

        assert resp.status_code == 200, resp.text
        set_level.assert_awaited_once()


def _session_cm(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class TestMcpTools:
    def setup_method(self) -> None:
        from modulo.api.mcp_server import (
            _ctx_auth_token,
            _ctx_auth_type,
            _ctx_org_id,
            _ctx_role,
            _ctx_team_id,
            _ctx_user_id,
        )

        _ctx_org_id.set(_ORG_ID)
        _ctx_role.set("admin")
        _ctx_user_id.set(_USER_ID)
        _ctx_team_id.set(None)
        _ctx_auth_token.set("mk_testprefix_testsecretkey1234567890abc")
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import (
            _ctx_auth_token,
            _ctx_auth_type,
            _ctx_org_id,
            _ctx_role,
            _ctx_team_id,
            _ctx_user_id,
        )

        for var in (_ctx_org_id, _ctx_role, _ctx_user_id, _ctx_team_id, _ctx_auth_token, _ctx_auth_type):
            var.set(None)

    async def test_demote_lowers_and_audits(self) -> None:
        from modulo.api.mcp_server import demote_pipeline_autonomy

        pipeline = _pipeline(earned="fully_autonomous")
        set_level = AsyncMock(return_value=pipeline)
        audit = AsyncMock()
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch("modulo.api.mcp_server._pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=pipeline)),
            patch("modulo.db.crud.pipeline.set_earned_autonomy_level", new=set_level),
            patch("modulo.core.audit_logger.append_audit_event", new=audit),
        ):
            result = await demote_pipeline_autonomy(
                pipeline_id=str(_PIPELINE_ID), level="manual_approval", reason="escape"
            )

        assert result["earned_autonomy_level"] == "manual_approval"
        assert result["event_type"] == "pipeline.autonomy_demoted"
        assert set_level.await_args.kwargs["level"] == "manual_approval"
        kwargs = audit.await_args.kwargs
        assert kwargs["event_type"] == "pipeline.autonomy_demoted"
        assert kwargs["payload_json"]["previous_level"] == "fully_autonomous"
        assert kwargs["payload_json"]["reason"] == "escape"

    async def test_promote_above_ceiling_rejected(self) -> None:
        from modulo.api.mcp_server import promote_pipeline_autonomy

        pipeline = _pipeline(earned="manual_approval", ceiling="notify_on_complete")
        set_level = AsyncMock(return_value=pipeline)
        audit = AsyncMock()
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch("modulo.api.mcp_server._pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=pipeline)),
            patch("modulo.db.crud.pipeline.set_earned_autonomy_level", new=set_level),
            patch("modulo.core.audit_logger.append_audit_event", new=audit),
        ):
            result = await promote_pipeline_autonomy(pipeline_id=str(_PIPELINE_ID), level="fully_autonomous")

        assert result["error"] == "validation_failed"
        set_level.assert_not_awaited()
        audit.assert_not_awaited()

    async def test_operator_without_owner_team_denied(self) -> None:
        from modulo.api.mcp_server import _ctx_role, demote_pipeline_autonomy

        _ctx_role.set("operator")
        pipeline = _pipeline(earned="fully_autonomous", owner_team_id=None)
        set_level = AsyncMock(return_value=pipeline)
        audit = AsyncMock()
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch("modulo.api.mcp_server._pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=pipeline)),
            patch("modulo.db.crud.pipeline.set_earned_autonomy_level", new=set_level),
            patch("modulo.core.audit_logger.append_audit_event", new=audit),
        ):
            result = await demote_pipeline_autonomy(pipeline_id=str(_PIPELINE_ID), level="manual_approval")

        assert result["error"] == "permission_denied"
        set_level.assert_not_awaited()
        audit.assert_not_awaited()

    async def test_owner_team_key_allowed(self) -> None:
        from modulo.api.mcp_server import _ctx_role, _ctx_team_id, demote_pipeline_autonomy

        _ctx_role.set("operator")
        _ctx_team_id.set(_OWNER_TEAM_ID)
        pipeline = _pipeline(earned="fully_autonomous", owner_team_id=_OWNER_TEAM_ID)
        set_level = AsyncMock(return_value=pipeline)
        audit = AsyncMock()
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch("modulo.api.mcp_server._pipeline_owner_team_id", new=AsyncMock(return_value=_OWNER_TEAM_ID)),
            patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=pipeline)),
            patch("modulo.db.crud.pipeline.set_earned_autonomy_level", new=set_level),
            patch("modulo.core.audit_logger.append_audit_event", new=audit),
        ):
            result = await demote_pipeline_autonomy(pipeline_id=str(_PIPELINE_ID), level="manual_approval")

        assert result["earned_autonomy_level"] == "manual_approval"
        set_level.assert_awaited_once()
