"""Tests for POST /api/v1/auth/accept-invite (FAR-461)."""

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.routes.auth import router as auth_router
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_INVITATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_STRONG_PASSWORD = "C0rr3ct-Horse-Battery"
_GENERIC_DETAIL = "Invalid or expired invitation"


def _override(admin_password: str = "testpass") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password=admin_password,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
    )


def _make_invitation() -> MagicMock:
    invitation = MagicMock()
    invitation.id = _INVITATION_ID
    invitation.organisation_id = _ORG_ID
    invitation.email = "invited@example.com"
    invitation.display_name = "Invited User"
    invitation.org_role = "runner"
    return invitation


def _make_account(*, auth_provider: str = "local", password_hash: str | None = None) -> MagicMock:
    account = MagicMock()
    account.id = _USER_ID
    account.email = "invited@example.com"
    account.display_name = "Invited User"
    account.active = True
    account.auth_provider = auth_provider
    account.password_hash = password_hash
    return account


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", _VALID_32)
    monkeypatch.setenv("FERNET_KEY", _VALID_32)
    get_settings.cache_clear()


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    result_mock.scalars.return_value = scalars_mock
    session.execute.return_value = result_mock
    return session


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(auth_router)
    return _app


@pytest.fixture
def client(mock_session: AsyncMock, app: FastAPI) -> AsyncGenerator[TestClient, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _override
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _base_patches(invitation: MagicMock) -> dict[str, object]:
    """Common patched CRUD surface: valid token lookup + successful CAS."""
    return {
        "hash_invitation_token": MagicMock(return_value=_VALID_32),
        "get_valid_by_token_hash": AsyncMock(return_value=invitation),
        "consume_invitation": AsyncMock(return_value=True),
        "set_rls_org": AsyncMock(),
        "append_audit_event": AsyncMock(),
    }


def test_accept_invite_creates_account_and_membership(client: TestClient) -> None:
    invitation = _make_invitation()
    patches = _base_patches(invitation)
    create_membership = AsyncMock(return_value=MagicMock())
    with patch.dict(
        "modulo.api.routes.auth.__dict__",
        {
            **patches,
            "validate_password_strength": MagicMock(),
            "get_account_by_email": AsyncMock(return_value=None),
            "create_account": AsyncMock(return_value=_make_account()),
            "get_membership_by_account_and_org": AsyncMock(return_value=None),
            "create_membership": create_membership,
        },
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "tok", "password": _STRONG_PASSWORD})
    assert resp.status_code == 200
    body = resp.json()
    assert body["existing_account"] is False
    # The pre-authenticated holder acts on behalf of the INVITING org.
    patches["set_rls_org"].assert_awaited_once_with(ANY, _ORG_ID)  # type: ignore[attr-defined]
    # Membership is created with the invitation's role.
    kwargs = create_membership.await_args.kwargs
    assert kwargs["account_id"] == _USER_ID
    assert kwargs["org_id"] == _ORG_ID
    assert kwargs["role"] == "runner"
    # CAS consumption ran as the final DB statement and the audit event fired.
    patches["consume_invitation"].assert_awaited_once()
    patches["append_audit_event"].assert_awaited_once()


def test_accept_invite_existing_account_keeps_password_and_flags(client: TestClient) -> None:
    """Branch (d): existing local password is untouched; UI gets the flag."""
    invitation = _make_invitation()
    existing_password_hash = "$2b$12$existing"
    account = _make_account(auth_provider="local", password_hash=existing_password_hash)
    patches = _base_patches(invitation)
    with patch.dict(
        "modulo.api.routes.auth.__dict__",
        {
            **patches,
            "validate_password_strength": MagicMock(),
            "get_account_by_email": AsyncMock(return_value=account),
            "get_membership_by_account_and_org": AsyncMock(return_value=None),
            "create_membership": AsyncMock(return_value=MagicMock()),
        },
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "tok", "password": _STRONG_PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["existing_account"] is True
    assert account.password_hash == existing_password_hash


def test_accept_invite_existing_member_does_not_duplicate_membership(client: TestClient) -> None:
    invitation = _make_invitation()
    account = _make_account(auth_provider="local", password_hash="$2b$12$existing")
    patches = _base_patches(invitation)
    create_membership = AsyncMock(return_value=MagicMock())
    with patch.dict(
        "modulo.api.routes.auth.__dict__",
        {
            **patches,
            "validate_password_strength": MagicMock(),
            "get_account_by_email": AsyncMock(return_value=account),
            "get_membership_by_account_and_org": AsyncMock(return_value=MagicMock(deactivated_at=None)),
            "create_membership": create_membership,
        },
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "tok", "password": _STRONG_PASSWORD})
    assert resp.status_code == 200
    create_membership.assert_not_awaited()


def test_accept_invite_passwordless_local_account_is_adopted(client: TestClient) -> None:
    """Branch (c): a pre-provisioned local account without a password gains one."""
    invitation = _make_invitation()
    account = _make_account(auth_provider="local", password_hash=None)
    patches = _base_patches(invitation)
    with patch.dict(
        "modulo.api.routes.auth.__dict__",
        {
            **patches,
            "validate_password_strength": MagicMock(),
            "get_account_by_email": AsyncMock(return_value=account),
            "get_membership_by_account_and_org": AsyncMock(return_value=None),
            "create_membership": AsyncMock(return_value=MagicMock()),
        },
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "tok", "password": _STRONG_PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["existing_account"] is False
    assert isinstance(account.password_hash, str)
    assert account.password_hash.startswith("$2b$")


def test_accept_invite_sso_account_never_gains_local_password(client: TestClient) -> None:
    """Branch (b): SSO/SCIM accounts are rejected with 409, no hash written."""
    invitation = _make_invitation()
    account = _make_account(auth_provider="oidc", password_hash=None)
    patches = _base_patches(invitation)
    with patch.dict(
        "modulo.api.routes.auth.__dict__",
        {**patches, "validate_password_strength": MagicMock(), "get_account_by_email": AsyncMock(return_value=account)},
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "tok", "password": _STRONG_PASSWORD})
    assert resp.status_code == 409
    assert account.password_hash is None


def test_accept_invite_invalid_token_generic_400(client: TestClient) -> None:
    with patch.dict(
        "modulo.api.routes.auth.__dict__",
        {"get_valid_by_token_hash": AsyncMock(return_value=None)},
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "bogus", "password": _STRONG_PASSWORD})
    assert resp.status_code == 400
    assert resp.json()["detail"] == _GENERIC_DETAIL


@pytest.mark.parametrize(
    "flavour",
    ["expired", "consumed", "revoked"],
)
def test_accept_invite_non_live_invitation_flavours_share_byte_identical_rejection(
    client: TestClient, flavour: str
) -> None:
    """Expired / consumed / revoked rows never match the liveness predicate, so
    ``get_valid_by_token_hash`` returns None for every non-live flavour and the
    API response is byte-identical (the lookup cannot distinguish them — by
    design)."""
    with patch.dict(
        "modulo.api.routes.auth.__dict__",
        {"get_valid_by_token_hash": AsyncMock(return_value=None)},
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": f"tok-{flavour}", "password": _STRONG_PASSWORD})
    assert resp.status_code == 400
    assert resp.content == f'{{"detail":"{_GENERIC_DETAIL}"}}'.encode()


def test_accept_invite_weak_password_422(client: TestClient) -> None:
    invitation = _make_invitation()
    patches = _base_patches(invitation)
    consume_invitation = AsyncMock(return_value=True)
    with patch.dict(
        "modulo.api.routes.auth.__dict__",
        {**patches, "consume_invitation": consume_invitation},
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "tok", "password": "12345678"})
    assert resp.status_code == 422
    consume_invitation.assert_not_awaited()


def test_accept_invite_cas_loss_aborts_with_generic_400(client: TestClient) -> None:
    """A lost compare-and-swap race aborts enrollment with the generic message."""
    invitation = _make_invitation()
    patches = _base_patches(invitation)
    patches["consume_invitation"] = AsyncMock(return_value=False)
    with patch.dict(
        "modulo.api.routes.auth.__dict__",
        {
            **patches,
            "validate_password_strength": MagicMock(),
            "get_account_by_email": AsyncMock(return_value=None),
            "create_account": AsyncMock(return_value=_make_account()),
            "get_membership_by_account_and_org": AsyncMock(return_value=None),
            "create_membership": AsyncMock(return_value=MagicMock()),
        },
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "tok", "password": _STRONG_PASSWORD})
    assert resp.status_code == 400
    assert resp.json()["detail"] == _GENERIC_DETAIL


def test_accept_invite_records_rate_limit_failure_on_denial(client: TestClient) -> None:
    fake_limiter = MagicMock()
    fake_limiter.record_failure = AsyncMock()
    with (
        patch("modulo.api.routes.auth.get_auth_rate_limiter", return_value=fake_limiter),
        patch.dict(
            "modulo.api.routes.auth.__dict__",
            {"get_valid_by_token_hash": AsyncMock(return_value=None)},
        ),
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "tok", "password": _STRONG_PASSWORD})
    assert resp.status_code == 400
    fake_limiter.record_failure.assert_awaited_once()


def test_accept_invite_works_without_rate_limiter(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.auth.get_auth_rate_limiter", return_value=None),
        patch.dict(
            "modulo.api.routes.auth.__dict__",
            {"get_valid_by_token_hash": AsyncMock(return_value=None)},
        ),
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "tok", "password": _STRONG_PASSWORD})
    assert resp.status_code == 400


def test_accept_invite_audit_failure_is_fail_open(client: TestClient) -> None:
    invitation = _make_invitation()
    patches = _base_patches(invitation)
    patches["append_audit_event"] = AsyncMock(side_effect=RuntimeError("audit down"))
    with patch.dict(
        "modulo.api.routes.auth.__dict__",
        {
            **patches,
            "validate_password_strength": MagicMock(),
            "get_account_by_email": AsyncMock(return_value=None),
            "create_account": AsyncMock(return_value=_make_account()),
            "get_membership_by_account_and_org": AsyncMock(return_value=None),
            "create_membership": AsyncMock(return_value=MagicMock()),
        },
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "tok", "password": _STRONG_PASSWORD})
    assert resp.status_code == 200


def test_accept_invite_tombstoned_membership_is_reactivated(client: TestClient) -> None:
    """A tombstoned membership (deactivated_at set) is not a live membership:
    acceptance reactivates it with the invitation's role instead of skipping —
    the row is preserved but the holder gains access."""
    invitation = _make_invitation()
    account = _make_account(auth_provider="local", password_hash="$2b$12$existing")
    tombstoned = MagicMock(deactivated_at="2026-08-01T00:00:00+00:00")
    patches = _base_patches(invitation)
    reactivate = AsyncMock(return_value=MagicMock())
    create_membership = AsyncMock(return_value=MagicMock())
    with patch.dict(
        "modulo.api.routes.auth.__dict__",
        {
            **patches,
            "validate_password_strength": MagicMock(),
            "get_account_by_email": AsyncMock(return_value=account),
            "get_membership_by_account_and_org": AsyncMock(return_value=tombstoned),
            "reactivate_membership": reactivate,
            "create_membership": create_membership,
        },
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "tok", "password": _STRONG_PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["existing_account"] is True
    create_membership.assert_not_awaited()
    reactivate.assert_awaited_once_with(ANY, tombstoned, "runner")


def test_accept_invite_record_success_failure_is_fail_open(client: TestClient) -> None:
    """A limiter outage AFTER the enrollment commit must not turn the succeeded
    operation into a 500 (the token is consumed; a retry would 400)."""
    fake_limiter = MagicMock()
    fake_limiter.record_failure = AsyncMock()
    fake_limiter.record_success = AsyncMock(side_effect=RuntimeError("redis down"))
    invitation = _make_invitation()
    patches = _base_patches(invitation)
    with (
        patch("modulo.api.routes.auth.get_auth_rate_limiter", return_value=fake_limiter),
        patch.dict(
            "modulo.api.routes.auth.__dict__",
            {
                **patches,
                "validate_password_strength": MagicMock(),
                "get_account_by_email": AsyncMock(return_value=None),
                "create_account": AsyncMock(return_value=_make_account()),
                "get_membership_by_account_and_org": AsyncMock(return_value=None),
                "create_membership": AsyncMock(return_value=MagicMock()),
            },
        ),
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "tok", "password": _STRONG_PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["existing_account"] is False
    fake_limiter.record_success.assert_awaited_once()


def test_accept_invite_integrity_error_conflicts(client: TestClient) -> None:
    """A DB-level IntegrityError (e.g. a unique/check violation) maps to 409, never 500."""
    invitation = _make_invitation()
    patches = _base_patches(invitation)
    patches["create_account"] = AsyncMock(side_effect=IntegrityError("dup", None, None))
    with patch.dict(
        "modulo.api.routes.auth.__dict__",
        {
            **patches,
            "validate_password_strength": MagicMock(),
            "get_account_by_email": AsyncMock(return_value=None),
            "get_membership_by_account_and_org": AsyncMock(return_value=None),
            "create_membership": AsyncMock(return_value=MagicMock()),
        },
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "tok", "password": _STRONG_PASSWORD})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "This email cannot be enrolled with this invitation."


def test_accept_invite_programming_error_501(client: TestClient) -> None:
    """A ProgrammingError (missing migration column) degrades to 501."""
    invitation = _make_invitation()
    patches = _base_patches(invitation)
    patches["create_account"] = AsyncMock(side_effect=ProgrammingError("no column", None, None))
    with patch.dict(
        "modulo.api.routes.auth.__dict__",
        {
            **patches,
            "validate_password_strength": MagicMock(),
            "get_account_by_email": AsyncMock(return_value=None),
            "get_membership_by_account_and_org": AsyncMock(return_value=None),
            "create_membership": AsyncMock(return_value=MagicMock()),
        },
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "tok", "password": _STRONG_PASSWORD})
    assert resp.status_code == 501
    assert resp.json()["detail"] == "Feature is not available. Run database migrations to enable it."


def test_accept_invite_sqlalchemy_error_503(client: TestClient) -> None:
    """A generic SQLAlchemyError degrades to 503."""
    invitation = _make_invitation()
    patches = _base_patches(invitation)
    patches["create_account"] = AsyncMock(side_effect=SQLAlchemyError("down"))
    with patch.dict(
        "modulo.api.routes.auth.__dict__",
        {
            **patches,
            "validate_password_strength": MagicMock(),
            "get_account_by_email": AsyncMock(return_value=None),
            "get_membership_by_account_and_org": AsyncMock(return_value=None),
            "create_membership": AsyncMock(return_value=MagicMock()),
        },
    ):
        resp = client.post("/api/v1/auth/accept-invite", json={"token": "tok", "password": _STRONG_PASSWORD})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Invitation acceptance is temporarily unavailable. Please try again."
