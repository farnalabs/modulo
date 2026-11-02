"""Focused branch coverage for the S3776 decomposition of API-key resolution.

``get_current_tenant_user_or_api_key`` was split into ``_resolve_api_key_principal``,
``_lookup_api_key_org``, ``_validate_api_key_in_org`` and
``_bind_api_key_principal``. Each gate keeps its original ordering; these tests
pin every arm: org lookup miss, invalid key, unreadable grants, Postgres and
generic org lookup, and the two membership-clamp rejections.
"""

import contextlib
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from modulo.auth.api_key import ApiKeyGrantsUnavailableError, ApiKeyInvalidError
from modulo.auth.dependencies import (
    OrganisationMembershipNotFound,
    _lookup_api_key_org,
    _validate_api_key_in_org,
    get_current_tenant_user_or_api_key,
)
from modulo.auth.jwt import CLIENT_KIND_PROGRAMMATIC
from modulo.settings import Settings

_API_KEY = "mk_12345678_" + "x" * 32


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _credentials() -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=_API_KEY)


def _key(role: str = "admin", team_id: uuid.UUID | None = None) -> Any:
    return SimpleNamespace(
        account_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        role=role,
        name="ci-key",
        team_id=team_id,
    )


def _factory(session: Any) -> MagicMock:
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


@contextlib.contextmanager
def _patched_engine(factory: MagicMock) -> Any:
    """Replace the DB engine/session-factory constructors for one resolution."""
    with (
        patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
        patch("modulo.api.dependencies.get_or_create_session_factory", return_value=factory),
    ):
        yield


async def test_api_key_org_lookup_miss_is_rejected_as_invalid_token() -> None:
    factory = MagicMock()
    with (
        _patched_engine(factory),
        patch("modulo.auth.dependencies._lookup_api_key_org", AsyncMock(return_value=None)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await get_current_tenant_user_or_api_key(_credentials(), _settings())

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or expired token"


async def test_api_key_validation_failure_is_rejected_as_invalid_token() -> None:
    key = _key()
    factory = MagicMock()
    with (
        _patched_engine(factory),
        patch("modulo.auth.dependencies._lookup_api_key_org", AsyncMock(return_value=key.organisation_id)),
        patch(
            "modulo.auth.dependencies._validate_api_key_in_org",
            AsyncMock(side_effect=ApiKeyInvalidError()),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await get_current_tenant_user_or_api_key(_credentials(), _settings())

    assert exc_info.value.status_code == 401


async def test_unreadable_grants_fail_closed_with_503() -> None:
    key = _key()
    factory = MagicMock()
    with (
        _patched_engine(factory),
        patch("modulo.auth.dependencies._lookup_api_key_org", AsyncMock(return_value=key.organisation_id)),
        patch("modulo.auth.dependencies._validate_api_key_in_org", AsyncMock(return_value=(key, "admin", "human"))),
        patch(
            "modulo.auth.api_key.resolve_key_grants",
            AsyncMock(side_effect=ApiKeyGrantsUnavailableError()),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await get_current_tenant_user_or_api_key(_credentials(), _settings())

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "API key grants temporarily unavailable."


async def test_missing_live_membership_is_rejected() -> None:
    key = _key()
    factory = MagicMock()
    with (
        _patched_engine(factory),
        patch("modulo.auth.dependencies._lookup_api_key_org", AsyncMock(return_value=key.organisation_id)),
        patch("modulo.auth.dependencies._validate_api_key_in_org", AsyncMock(return_value=(key, None, None))),
        patch("modulo.auth.api_key.resolve_key_grants", AsyncMock(return_value=None)),
        pytest.raises(OrganisationMembershipNotFound),
    ):
        await get_current_tenant_user_or_api_key(_credentials(), _settings())


async def test_unclampable_role_is_rejected() -> None:
    key = _key(role="not-a-role")
    factory = MagicMock()
    with (
        _patched_engine(factory),
        patch("modulo.auth.dependencies._lookup_api_key_org", AsyncMock(return_value=key.organisation_id)),
        patch("modulo.auth.dependencies._validate_api_key_in_org", AsyncMock(return_value=(key, "admin", "human"))),
        patch("modulo.auth.api_key.resolve_key_grants", AsyncMock(return_value=None)),
        pytest.raises(OrganisationMembershipNotFound),
    ):
        await get_current_tenant_user_or_api_key(_credentials(), _settings())


async def test_resolved_principal_carries_api_key_markers() -> None:
    team_id = uuid.uuid4()
    key = _key(role="operator", team_id=team_id)
    grants = frozenset({"pipeline.list"})
    factory = MagicMock()
    with (
        _patched_engine(factory),
        patch("modulo.auth.dependencies._lookup_api_key_org", AsyncMock(return_value=key.organisation_id)),
        patch("modulo.auth.dependencies._validate_api_key_in_org", AsyncMock(return_value=(key, "operator", "human"))),
        patch("modulo.auth.api_key.resolve_key_grants", AsyncMock(return_value=grants)),
    ):
        principal = await get_current_tenant_user_or_api_key(_credentials(), _settings())

    assert principal.username == "ci-key"
    assert principal.organisation_id == key.organisation_id
    assert principal.account_id == key.account_id
    assert principal.org_role == "operator"
    assert principal.via_api_key is True
    assert principal.client_kind == CLIENT_KIND_PROGRAMMATIC
    assert principal.team_id == team_id
    assert principal.key_grants == grants


async def test_lookup_api_key_org_uses_postgres_security_definer() -> None:
    org_id = uuid.uuid4()
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: org_id))

    with patch("modulo.db.rls._ensure_active_transaction", AsyncMock(return_value="postgresql")):
        resolved = await _lookup_api_key_org(_factory(session), _API_KEY)

    assert resolved == org_id


async def test_lookup_api_key_org_generic_backend_without_row_returns_none() -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))

    with patch("modulo.db.rls._ensure_active_transaction", AsyncMock(return_value="sqlite")):
        resolved = await _lookup_api_key_org(_factory(session), _API_KEY)

    assert resolved is None


async def test_lookup_api_key_org_generic_backend_returns_row_org() -> None:
    org_id = uuid.uuid4()
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(organisation_id=org_id))
    )

    with patch("modulo.db.rls._ensure_active_transaction", AsyncMock(return_value="sqlite")):
        resolved = await _lookup_api_key_org(_factory(session), _API_KEY)

    assert resolved == org_id


async def test_validate_api_key_in_org_reads_live_role() -> None:
    key = _key()
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))

    with (
        patch("modulo.auth.api_key.validate_api_key", AsyncMock(return_value=key)),
        patch("modulo.db.rls.set_rls_org", AsyncMock()),
        patch(
            "modulo.db.crud.org_membership.resolve_live_role_and_kind",
            AsyncMock(return_value=("viewer", "human")),
        ),
    ):
        resolved_key, live_role, owner_kind = await _validate_api_key_in_org(
            _factory(session), _API_KEY, key.organisation_id
        )

    assert resolved_key is key
    assert live_role == "viewer"
    assert owner_kind == "human"
