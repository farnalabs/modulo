"""Regression tests for the team-assignment bypass on CREATE/copy paths (#1793).

CREATE endpoints accepted a raw client-supplied ``owner_team_id`` and
persisted it without validating that the team exists in the caller's
organisation or that the caller is a member — the same boundary the PATCH gate
(``require_team_membership_or_admin`` / ``_assert_team_transition_allowed``)
enforces on updates. The fix routes every create/copy site through
``validate_owner_team_for_create``: fail CLOSED (404 for a missing/foreign-org
team, 403 for a non-member).

Endpoint-level tests exercise the wires via POST /pipelines (representative of
the five wired sites); helper-level tests cover the membership and
update-transition semantics directly.
"""

import uuid
from collections.abc import AsyncGenerator, Callable, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.sql import Select

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.team_scope import (
    validate_owner_team_for_create,
    validate_team_transition_for_update,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_CREATE_BODY = {
    "name": "Sprint Pipeline",
    "visibility": "team",
    "owner_team_id": str(_TEAM_ID),
}


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_team_visible_session(team_found: bool) -> AsyncMock:
    """Session whose ``SELECT ... FROM teams`` result reflects ``team_found``."""
    session = configure_mock_session(AsyncMock())
    base_effect = session.execute.side_effect

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(stmt, Select) and "FROM teams" in str(stmt):
            result = MagicMock()
            result.first.return_value = (_TEAM_ID,) if team_found else None
            return result
        return base_effect(stmt, *args, **kwargs)

    session.execute = AsyncMock(side_effect=_execute)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def make_client() -> Generator[Callable[..., TestClient], None, None]:
    """Build a TestClient authorised as the given principal role."""

    def _make(*, org_role: str = "operator", team_found: bool = True) -> TestClient:
        session = _make_team_visible_session(team_found=team_found)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        app.dependency_overrides.setdefault(get_settings, _make_settings)
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="testuser",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role=org_role,
        )
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


async def _principal(org_role: str = "operator") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )


class TestPipelineCreateTeamGateEndpoint:
    """POST /pipelines must fail closed on a client-supplied foreign team."""

    def test_create_with_foreign_org_team_returns_404(self, make_client: Callable[..., TestClient]) -> None:
        """404 even for admins — a missing team is never silently persisted."""
        client = make_client(org_role="admin", team_found=False)
        with patch("modulo.api.routes.pipelines.create_pipeline", new=AsyncMock()) as create_pipeline:
            resp = client.post("/api/v1/pipelines", json=_CREATE_BODY)
        assert resp.status_code == 404
        create_pipeline.assert_not_called()

    def test_create_with_non_member_team_returns_403(self, make_client: Callable[..., TestClient]) -> None:
        client = make_client(org_role="operator")
        with (
            patch("modulo.api.team_scope.team_membership_exists", new=AsyncMock(return_value=False)),
            patch("modulo.api.routes.pipelines.create_pipeline", new=AsyncMock()) as create_pipeline,
        ):
            resp = client.post("/api/v1/pipelines", json=_CREATE_BODY)
        assert resp.status_code == 403
        create_pipeline.assert_not_called()

    def test_create_with_member_team_reaches_crud(self, make_client: Callable[..., TestClient]) -> None:
        client = make_client(org_role="operator")
        with (
            patch("modulo.api.team_scope.team_membership_exists", new=AsyncMock(return_value=True)),
            patch("modulo.api.routes.pipelines._pipeline_response", new=MagicMock(return_value=MagicMock())),
            patch(
                "modulo.api.routes.pipelines.create_pipeline",
                new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
            ) as create_pipeline,
        ):
            client.post("/api/v1/pipelines", json=_CREATE_BODY)
        create_pipeline.assert_called_once()


class TestValidateOwnerTeamForCreate:
    async def test_none_owner_team_is_noop(self) -> None:
        session = configure_mock_session(AsyncMock())
        with patch.object(session, "execute", new=AsyncMock()) as execute:
            await validate_owner_team_for_create(session, await _principal(), None)
        execute.assert_not_awaited()

    async def test_foreign_org_team_raises_404_even_for_admin(self) -> None:
        session = configure_mock_session(AsyncMock())
        result = MagicMock()
        result.first.return_value = None
        with (
            patch.object(session, "execute", new=AsyncMock(return_value=result)),
            pytest.raises(HTTPException) as exc_info,
        ):
            await validate_owner_team_for_create(session, await _principal("admin"), _TEAM_ID)
        assert exc_info.value.status_code == 404

    async def test_non_member_raises_403(self) -> None:
        session = configure_mock_session(AsyncMock())
        result = MagicMock()
        result.first.return_value = (_TEAM_ID,)
        with (
            patch.object(session, "execute", new=AsyncMock(return_value=result)),
            patch("modulo.api.team_scope.team_membership_exists", new=AsyncMock(return_value=False)),
            pytest.raises(HTTPException) as exc_info,
        ):
            await validate_owner_team_for_create(session, await _principal(), _TEAM_ID)
        assert exc_info.value.status_code == 403

    async def test_member_passes(self) -> None:
        session = configure_mock_session(AsyncMock())
        result = MagicMock()
        result.first.return_value = (_TEAM_ID,)
        membership_exists = AsyncMock(return_value=True)
        with (
            patch.object(session, "execute", new=AsyncMock(return_value=result)),
            patch("modulo.api.team_scope.team_membership_exists", new=membership_exists),
        ):
            await validate_owner_team_for_create(session, await _principal(), _TEAM_ID)
        membership_exists.assert_awaited_once()

    async def test_admin_bypasses_membership_but_not_org_check(self) -> None:
        """Admin is not required to be a member, but the team must exist in-org."""
        session = configure_mock_session(AsyncMock())
        result = MagicMock()
        result.first.return_value = (_TEAM_ID,)
        with (
            patch.object(session, "execute", new=AsyncMock(return_value=result)),
            patch("modulo.api.team_scope.team_membership_exists", new=AsyncMock()) as membership,
        ):
            await validate_owner_team_for_create(session, await _principal("admin"), _TEAM_ID)
        membership.assert_not_awaited()


class TestValidateTeamTransitionForUpdate:
    """PATCH-parity guard for update endpoints lacking a team gate (connectors)."""

    async def test_reassignment_without_membership_raises_403(self) -> None:
        session = configure_mock_session(AsyncMock())
        with (
            patch("modulo.api.team_scope.team_membership_exists", new=AsyncMock(return_value=False)),
            pytest.raises(HTTPException) as exc_info,
        ):
            await validate_team_transition_for_update(
                session,
                await _principal(),
                current_owner_team_id=uuid.uuid4(),
                current_visibility="team",
                new_owner_team_id=_TEAM_ID,
                new_visibility="team",
            )
        assert exc_info.value.status_code == 403

    async def test_reassignment_to_foreign_org_team_raises_404(self) -> None:
        session = configure_mock_session(AsyncMock())
        result = MagicMock()
        result.first.return_value = None
        with (
            patch.object(session, "execute", new=AsyncMock(return_value=result)),
            patch("modulo.api.team_scope.team_membership_exists", new=AsyncMock(return_value=True)),
            pytest.raises(HTTPException) as exc_info,
        ):
            await validate_team_transition_for_update(
                session,
                await _principal(),
                current_owner_team_id=uuid.uuid4(),
                current_visibility="team",
                new_owner_team_id=_TEAM_ID,
                new_visibility="team",
            )
        assert exc_info.value.status_code == 404

    async def test_admin_bypasses_membership(self) -> None:
        session = configure_mock_session(AsyncMock())
        with patch("modulo.api.team_scope.team_membership_exists", new=AsyncMock()) as membership:
            await validate_team_transition_for_update(
                session,
                await _principal("admin"),
                current_owner_team_id=uuid.uuid4(),
                current_visibility="team",
                new_owner_team_id=_TEAM_ID,
                new_visibility="team",
            )
        membership.assert_not_awaited()

    async def test_team_visibility_without_owner_team_raises_422(self) -> None:
        session = configure_mock_session(AsyncMock())
        with pytest.raises(HTTPException) as exc_info:
            await validate_team_transition_for_update(
                session,
                await _principal(),
                current_owner_team_id=None,
                current_visibility="org",
                new_owner_team_id=None,
                new_visibility="team",
            )
        assert exc_info.value.status_code == 422

    async def test_unchanged_org_resource_is_noop(self) -> None:
        session = configure_mock_session(AsyncMock())
        with (
            patch.object(session, "execute", new=AsyncMock()) as execute,
            patch("modulo.api.team_scope.team_membership_exists", new=AsyncMock()) as membership_exists,
        ):
            await validate_team_transition_for_update(
                session,
                await _principal(),
                current_owner_team_id=None,
                current_visibility="org",
                new_owner_team_id=None,
                new_visibility="org",
            )
        execute.assert_not_awaited()
        membership_exists.assert_not_awaited()
