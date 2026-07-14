"""Unit tests for SCIM auth: token validation, plan context, feature gating."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.models.problem import ProblemException
from modulo.auth.scim_auth import (
    ScimPrincipal,
    get_scim_plan_context,
    get_scim_principal,
    require_scim_feature,
)
from modulo.settings import Settings


def _settings(scim_token: str = "valid-scim-token", default_org_id: str = "") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_scim_token=scim_token,
        modulo_scim_default_org_id=default_org_id,
    )


def _credentials(token: str = "valid-scim-token") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _make_session(org_id: uuid.UUID | None = None) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = MagicMock(id=org_id) if org_id else None
    session.execute.return_value = result
    return session


class TestGetScimPrincipal:
    async def test_valid_token_with_default_org_id(self) -> None:
        org_id = uuid.uuid4()
        settings = _settings(default_org_id=str(org_id))
        session = _make_session()
        principal = await get_scim_principal(_credentials(), settings, session)
        assert isinstance(principal, ScimPrincipal)
        assert principal.organisation_id == org_id

    async def test_valid_token_without_default_org_lookup(self) -> None:
        org_id = uuid.uuid4()
        settings = _settings()
        session = _make_session(org_id=org_id)
        principal = await get_scim_principal(_credentials(), settings, session)
        assert isinstance(principal, ScimPrincipal)
        assert principal.organisation_id == org_id

    async def test_missing_credentials_returns_401(self) -> None:
        settings = _settings()
        session = _make_session()
        with pytest.raises(HTTPException) as exc:
            await get_scim_principal(None, settings, session)
        assert exc.value.status_code == 401
        assert "Missing SCIM token" in exc.value.detail

    async def test_missing_scim_token_returns_501(self) -> None:
        settings = _settings(scim_token="")
        session = _make_session()
        with pytest.raises(HTTPException) as exc:
            await get_scim_principal(_credentials(), settings, session)
        assert exc.value.status_code == 501
        assert "SCIM is not configured" in exc.value.detail

    async def test_wrong_token_returns_401(self) -> None:
        settings = _settings()
        session = _make_session()
        with pytest.raises(HTTPException) as exc:
            await get_scim_principal(_credentials("wrong-token"), settings, session)
        assert exc.value.status_code == 401
        assert "Invalid SCIM token" in exc.value.detail

    async def test_invalid_uuid_in_default_org_id_returns_500(self) -> None:
        settings = _settings(default_org_id="not-a-uuid")
        session = _make_session()
        with pytest.raises(HTTPException) as exc:
            await get_scim_principal(_credentials(), settings, session)
        assert exc.value.status_code == 500
        assert "not a valid UUID" in exc.value.detail

    async def test_no_org_in_db_returns_500(self) -> None:
        settings = _settings()
        session = _make_session(org_id=None)
        with pytest.raises(HTTPException) as exc:
            await get_scim_principal(_credentials(), settings, session)
        assert exc.value.status_code == 500
        assert "No organisation exists" in exc.value.detail


class TestGetScimPlanContext:
    async def test_programming_error_returns_501(self) -> None:
        principal = ScimPrincipal(organisation_id=uuid.uuid4())
        settings = _settings()
        session = AsyncMock(spec=AsyncSession)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(side_effect=ProgrammingError("mock", {}, None))
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        with pytest.raises(HTTPException) as exc:
            await get_scim_plan_context(principal, settings, session)
        assert exc.value.status_code == 501
        assert "Run database migrations" in exc.value.detail


class TestRequireScimFeature:
    async def test_feature_disabled_returns_problem(self) -> None:
        ctx = MagicMock()
        ctx.feature_enabled.return_value = False
        with pytest.raises(ProblemException) as exc:
            await require_scim_feature(ctx)
        assert "scim" in exc.value.detail.lower()
