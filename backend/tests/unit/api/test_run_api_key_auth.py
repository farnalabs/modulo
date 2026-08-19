"""Unit tests for org API key (``mk_``) auth on run trigger/read endpoints."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from modulo.auth.dependencies import InvalidToken, get_current_tenant_user_or_api_key
from modulo.auth.jwt import create_access_token
from modulo.settings import get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_KEY = "mk_12345678_" + "x" * 32


def _make_session(key_record: object | None = None) -> AsyncMock:
    if key_record is None:
        key_record = _fake_key()
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=key_record)
    session.execute = AsyncMock(return_value=result)
    return session


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
            return_value=_FakeFactory(_make_session()),
        ),
        patch("modulo.auth.api_key.validate_api_key", return_value=_fake_key("runner")) as validate_patch,
        patch(
            "modulo.auth.dependencies.resolve_role_from_membership",
            new=AsyncMock(return_value="runner"),
        ),
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
            return_value=_FakeFactory(_make_session()),
        ),
        patch("modulo.auth.api_key.validate_api_key", side_effect=ApiKeyInvalidError("bad key")),
        pytest.raises(InvalidToken),
    ):
        await get_current_tenant_user_or_api_key(
            credentials=_credentials(_KEY),
            settings=settings,
        )


@pytest.mark.asyncio
async def test_api_key_unknown_prefix_raises_401() -> None:
    """An org lookup that returns no organisation must reject the key with 401,
    never fall through to validate_api_key."""
    settings = get_settings()
    session = _make_session()
    session.execute.return_value.scalar_one_or_none.return_value = None
    with (
        patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
        patch(
            "modulo.api.dependencies.get_or_create_session_factory",
            return_value=_FakeFactory(session),
        ),
        patch("modulo.auth.api_key.validate_api_key") as validate_patch,
        pytest.raises(InvalidToken),
    ):
        await get_current_tenant_user_or_api_key(
            credentials=_credentials(_KEY),
            settings=settings,
        )

    validate_patch.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_key_postgres_dialect_uses_lookup_function() -> None:
    """On Postgres the org is resolved through the SECURITY DEFINER lookup
    function rather than a prefix scan (org_api_keys has RLS enabled)."""
    settings = get_settings()
    session = _make_session()
    session.get_bind = MagicMock()
    session.get_bind.return_value.dialect.name = "postgresql"

    with (
        patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
        patch(
            "modulo.api.dependencies.get_or_create_session_factory",
            return_value=_FakeFactory(session),
        ),
        patch("modulo.auth.api_key.validate_api_key", return_value=_fake_key("runner")),
        patch(
            "modulo.auth.dependencies.resolve_role_from_membership",
            new=AsyncMock(return_value="runner"),
        ),
    ):
        principal = await get_current_tenant_user_or_api_key(
            credentials=_credentials(_KEY),
            settings=settings,
        )

    assert principal.organisation_id == _ORG_ID
    assert principal.org_role == "runner"
    first_stmt = str(session.execute.await_args_list[0].args[0])
    assert "lookup_api_key_org" in first_stmt


@pytest.mark.asyncio
async def test_api_key_operator_role_principal_is_accepted() -> None:
    settings = get_settings()
    with (
        patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
        patch(
            "modulo.api.dependencies.get_or_create_session_factory",
            return_value=_FakeFactory(_make_session()),
        ),
        patch("modulo.auth.api_key.validate_api_key", return_value=_fake_key("operator")),
        patch(
            "modulo.auth.dependencies.resolve_role_from_membership",
            new=AsyncMock(return_value="operator"),
        ),
    ):
        principal = await get_current_tenant_user_or_api_key(
            credentials=_credentials(_KEY),
            settings=settings,
        )

    assert principal.organisation_id == _ORG_ID
    assert principal.account_id == _USER_ID
    assert principal.org_role == "operator"
    assert principal.is_system_admin is False


@pytest.mark.asyncio
async def test_api_key_db_error_returns_503() -> None:
    from sqlalchemy.exc import SQLAlchemyError

    settings = get_settings()
    session = _make_session()
    session.execute = AsyncMock(side_effect=SQLAlchemyError("db down"))
    with (
        patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
        patch(
            "modulo.api.dependencies.get_or_create_session_factory",
            return_value=_FakeFactory(session),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await get_current_tenant_user_or_api_key(
            credentials=_credentials(_KEY),
            settings=settings,
        )

    assert exc_info.value.status_code == 503


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
    with patch("modulo.auth.dependencies._verify_identity", new=AsyncMock(return_value=None)):
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
