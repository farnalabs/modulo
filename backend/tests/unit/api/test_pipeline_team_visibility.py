"""Regression tests for the cross-team pipeline visibility leak (GET).

A user who is a member of Team B must NOT be able to read Team A's
team-private pipeline (``visibility='team'``, ``owner_team_id=Team A``) via
GET /pipelines/{id}. The DB root cause (an OR'd org-only RLS policy that made
the team policy dead weight) is fixed by migration ``0122_fix_team_rls_policies``;
the app-layer defense-in-depth is the ``require_team_membership_or_admin`` gate
on the GET route. These tests exercise the app-layer gate.

A member of Team A CAN read the pipeline; an org admin bypasses the gate; and
org-visible pipelines (``visibility='org'``, ``owner_team_id=None``) are NOT
team-gated even for a non-member.
"""

import uuid
from collections.abc import AsyncGenerator, Callable, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql import Select

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-000000000003")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_pipeline(*, owner_team_id: uuid.UUID | None, visibility: str) -> MagicMock:
    p = MagicMock()
    p.id = _PIPELINE_ID
    p.organisation_id = _ORG_ID
    p.name = "Team Pipeline"
    p.description = None
    p.visibility = visibility
    p.max_concurrent_runs = 5
    p.lock_wait_timeout_seconds = 300
    p.node_timeout_seconds = 300
    p.run_context_defaults = {}
    p.default_autonomy_level = "manual_approval"
    p.max_duration_seconds = None
    p.stale_run_timeout_minutes = 30
    p.rate_limit_config = None
    p.retry_policy = {}
    p.snapshot_count = 0
    p.archived_at = None
    p.owner_team_id = owner_team_id
    p.folder_id = None
    p.account_id = _USER_ID
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


class _ResolverRow:
    """Mutable holder for the team-scope resolver's row.

    The ``require_team_membership_or_admin`` dependency resolves the target
    row's ``owner_team_id``/``visibility`` with a ``SELECT ... FROM pipelines``.
    This holder lets each test set what that resolver sees without rebuilding
    the whole mock session.
    """

    def __init__(self, *, owner_team_id: uuid.UUID | None, visibility: str) -> None:
        self.owner_team_id = owner_team_id
        self.visibility = visibility


def _make_mock_session(resolver: _ResolverRow) -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    base_effect = session.execute.side_effect

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(stmt, Select) and "FROM pipelines" in str(stmt):
            row = MagicMock()
            row.first.return_value = (resolver.owner_team_id, resolver.visibility)
            return row
        return base_effect(stmt, *args, **kwargs)

    session.execute = AsyncMock(side_effect=_execute)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def make_client() -> Generator[Callable[..., tuple[TestClient, _ResolverRow]], None, None]:
    """Factory that builds a TestClient with the given principal + resolver row."""

    def _make(
        *,
        org_role: str = "operator",
        owner_team_id: uuid.UUID | None = None,
        visibility: str = "org",
    ) -> tuple[TestClient, _ResolverRow]:
        resolver = _ResolverRow(owner_team_id=owner_team_id, visibility=visibility)
        mock_session = _make_mock_session(resolver)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_settings] = _make_settings
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
        return TestClient(app), resolver

    yield _make
    app.dependency_overrides.clear()


def _get(client: TestClient, *, membership: bool) -> int:
    with (
        patch("modulo.api.dependencies.team_membership_exists", new=AsyncMock(return_value=membership)),
        patch(
            "modulo.api.routes.pipelines.get_pipeline",
            new=AsyncMock(return_value=_make_pipeline(owner_team_id=_TEAM_A, visibility="team")),
        ),
    ):
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}")
    return resp.status_code


class TestCrossTeamPipelineVisibility:
    def test_non_member_cannot_get_team_private_pipeline(
        self, make_client: Callable[..., tuple[TestClient, _ResolverRow]]
    ) -> None:
        client, _ = make_client(org_role="operator", owner_team_id=_TEAM_A, visibility="team")
        assert _get(client, membership=False) == 403

    def test_member_can_get_team_private_pipeline(
        self, make_client: Callable[..., tuple[TestClient, _ResolverRow]]
    ) -> None:
        client, _ = make_client(org_role="operator", owner_team_id=_TEAM_A, visibility="team")
        assert _get(client, membership=True) == 200

    def test_org_admin_can_get_team_private_pipeline(
        self, make_client: Callable[..., tuple[TestClient, _ResolverRow]]
    ) -> None:
        client, _ = make_client(org_role="admin", owner_team_id=_TEAM_A, visibility="team")
        assert _get(client, membership=False) == 200

    def test_org_visible_pipeline_not_team_gated(
        self, make_client: Callable[..., tuple[TestClient, _ResolverRow]]
    ) -> None:
        client, _ = make_client(org_role="operator", owner_team_id=None, visibility="org")
        with patch(
            "modulo.api.routes.pipelines.get_pipeline",
            new=AsyncMock(return_value=_make_pipeline(owner_team_id=None, visibility="org")),
        ):
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}")
        assert resp.status_code == 200
        assert resp.json()["visibility"] == "org"
