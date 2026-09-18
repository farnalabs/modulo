"""Unit tests for the analytics route team-boundary resolver (#1795).

``_resolve_scoped_team_ids`` is the fail-closed boundary that a non-admin
caller's analytics queries are constrained to:
* an org admin is unscoped (``None``) — no session is opened,
* any other caller gets the tuple of their OWN ``TeamMembership.team_id``
  rows, resolved inside a session pinned to the caller's org (``set_rls_org``),
* a DB failure is a 503, never an unconstrained boundary.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.routes import analytics as analytics_routes

_ORG = uuid.uuid4()
_ACCOUNT = uuid.uuid4()
_TEAM_A = uuid.uuid4()
_TEAM_B = uuid.uuid4()


class _Ctx:
    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _principal(org_role: str = "viewer") -> SimpleNamespace:
    return SimpleNamespace(
        organisation_id=_ORG,
        account_id=_ACCOUNT,
        org_role=org_role,
    )


def _factory(exec_result: Any = None, exc: Exception | None = None) -> MagicMock:
    """A factory returning a session whose execute() yields the given result."""
    session = MagicMock()
    session.begin.return_value = _Ctx()
    if exc is not None:
        session.execute = AsyncMock(side_effect=exc)
    else:
        result = MagicMock()
        result.scalars.return_value.all.return_value = [_TEAM_A, _TEAM_B]
        session.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


class TestResolveScopedTeamIds:
    @pytest.mark.anyio
    async def test_org_admin_is_unscoped_without_session(self) -> None:
        factory = _factory()
        result = await analytics_routes._resolve_scoped_team_ids(factory, org_id=_ORG, principal=_principal("admin"))
        assert result is None
        factory.assert_not_called(), "an unscoped (admin) caller must not hit the DB"

    @pytest.mark.anyio
    async def test_member_boundary_is_own_teams_pins_org_rls_context(self) -> None:
        factory = _factory()
        with patch.object(analytics_routes, "set_rls_org", new_callable=AsyncMock) as rls:
            result = await analytics_routes._resolve_scoped_team_ids(factory, org_id=_ORG, principal=_principal())
        assert result == (_TEAM_A, _TEAM_B)
        rls.assert_awaited_once()
        session = factory.return_value.__aenter__.return_value
        session.execute.assert_awaited_once()

    @pytest.mark.anyio
    async def test_db_failure_fails_closed_with_503(self) -> None:
        # A resolver error must NEVER degrade to an unconstrained boundary —
        # the query is denied with 503 instead.
        factory = _factory(exc=SQLAlchemyError("boom"))
        rls = patch.object(analytics_routes, "set_rls_org", new_callable=AsyncMock)
        with rls, pytest.raises(HTTPException) as excinfo:
            await analytics_routes._resolve_scoped_team_ids(factory, org_id=_ORG, principal=_principal())
        assert excinfo.value.status_code == 503

    @pytest.mark.anyio
    async def test_member_without_account_id_is_empty_boundary(self) -> None:
        principal = _principal()
        principal.account_id = None
        factory = _factory()
        result = await analytics_routes._resolve_scoped_team_ids(factory, org_id=_ORG, principal=principal)
        assert result == ()
        factory.assert_not_called()
