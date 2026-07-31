"""Unit tests for org API key (``mk_``) auth on run trigger/read endpoints."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.security import HTTPAuthorizationCredentials

from modulo.auth.dependencies import InvalidToken, get_current_tenant_user_or_api_key
from modulo.auth.jwt import create_access_token
from modulo.settings import get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_KEY = "mk_12345678_" + "x" * 32


class _FakeFactory:
    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        return False


def _fake_key(role: str = "runner"):
    return SimpleNamespace(
        name="smoke-test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        role=role,
    )


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.mark.asyncio
async def test_api_key_runner_principal_is_accepted() -> None:
    settings = get_settings()
    with (
        patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()) as engine_patch,
        patch(
            "modulo.api.dependencies.get_or_create_session_factory",
            return_value=_FakeFactory(AsyncMock()),
        ),
        patch("modulo.auth.api_key.validate_api_key", return_value=_fake_key("runner")) as validate_patch,
    ):
        principal = await get_current_tenant_user_or_api_key(
            credentials=_credentials(_KEY),
            settings=settings,
        )
        validate_patch.assert_awaited_once()
        engine_patch.assert_called_once_with(settings)

    assert principal.organisation_id == _ORG_ID
    assert principal.account_id == _USER_ID
    assert principal.org_role == "runner"
    assert principal.is_system_admin is False


@pytest.mark.asyncio
async def test_api_key_invalid_raises_401() -> None:
    from modulo.auth.api_key import ApiKeyInvalidError

    settings = get_settings()
    with (
        patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
        patch(
            "modulo.api.dependencies.get_or_create_session_factory",
            return_value=_FakeFactory(AsyncMock()),
        ),
        patch("modulo.auth.api_key.validate_api_key", side_effect=ApiKeyInvalidError("bad key")),
        pytest.raises(InvalidToken),
    ):
        await get_current_tenant_user_or_api_key(
            credentials=_credentials(_KEY),
            settings=settings,
        )


@pytest.mark.asyncio
async def test_api_key_admin_role_is_rejected() -> None:
    settings = get_settings()
    with (
        patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
        patch(
            "modulo.api.dependencies.get_or_create_session_factory",
            return_value=_FakeFactory(AsyncMock()),
        ),
        patch("modulo.auth.api_key.validate_api_key", return_value=_fake_key("admin")),
        pytest.raises(InvalidToken),
    ):
        await get_current_tenant_user_or_api_key(
            credentials=_credentials(_KEY),
            settings=settings,
        )


@pytest.mark.asyncio
async def test_jwt_principal_still_accepted() -> None:
    settings = get_settings()
    token = create_access_token(
        subject="testuser",
        secret_key=settings.secret_key,
        organisation_id=str(_ORG_ID),
        account_id=str(_USER_ID),
        org_role="admin",
    )
    with patch("modulo.auth.dependencies._verify_identity", new=AsyncMock()):
        principal = await get_current_tenant_user_or_api_key(
            credentials=_credentials(token),
            settings=settings,
        )

    assert principal.organisation_id == _ORG_ID
    assert principal.account_id == _USER_ID
    assert principal.org_role == "admin"


@pytest.mark.asyncio
async def test_missing_credentials_raises_401() -> None:
    settings = get_settings()
    with pytest.raises(InvalidToken):
        await get_current_tenant_user_or_api_key(credentials=None, settings=settings)


@pytest.mark.asyncio
async def test_garbage_jwt_raises_401() -> None:
    settings = get_settings()
    with pytest.raises(InvalidToken):
        await get_current_tenant_user_or_api_key(
            credentials=_credentials("not-a-token"),
            settings=settings,
        )
