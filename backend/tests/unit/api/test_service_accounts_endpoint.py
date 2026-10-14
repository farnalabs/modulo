"""Unit tests for /api/v1/service-accounts (FAR-1478, ADR 056 / ADR 058).

Route-shape tests: provisioning, the structural admin-role rejection, the
decommission transaction shape, and the key-mint ownership rules. The real
trigger / CHECK / TTL behaviour against Postgres lives in
``tests/integration/``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes.service_accounts import _validate_service_org_role
from modulo.auth.api_key import ApiKeyTtlError
from modulo.auth.dependencies import get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.audit_coverage import audit_session
from modulo.core.principals import KIND_SERVICE
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_SA_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_KEY_ID = uuid.uuid4()
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _stub_audit_session() -> Generator[None, None, None]:
    """The fail-closed ``audited(...)`` dependency writes on a fresh
    ``audit_session`` (a real engine — none in the unit tier): stub the seam."""

    async def _override() -> AsyncGenerator[AsyncMock, None]:
        session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        yield session

    app.dependency_overrides[audit_session] = _override
    yield
    app.dependency_overrides.pop(audit_session, None)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_public_url="http://localhost:8000",
        modulo_license_key="test-license-key",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_client(org_role: str = "admin") -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="adminuser",
        organisation_id=_ORG_ID,
        account_id=_ADMIN_ID,
        org_role=org_role,
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client() -> Generator[TestClient, None, None]:
    yield from _make_client("admin")


@pytest.fixture
def operator_client() -> Generator[TestClient, None, None]:
    yield from _make_client("operator")


def _service_account(decommissioned: bool = False) -> MagicMock:
    account = MagicMock()
    account.id = _SA_ID
    account.kind = KIND_SERVICE
    account.display_name = "ci-bot"
    account.active = not decommissioned
    account.decommissioned_at = _NOW if decommissioned else None
    account.created_at = _NOW
    account.is_break_glass = False
    return account


def _membership(role: str = "operator") -> MagicMock:
    membership = MagicMock()
    membership.role = role
    membership.deactivated_at = None
    return membership


def _api_key() -> MagicMock:
    key = MagicMock()
    key.id = _KEY_ID
    key.name = "ci key"
    key.role = "runner"
    key.lookup_prefix = "abcd1234"
    key.created_at = _NOW
    key.scope = "org"
    return key


def _tenant_principal(
    *, org_role: str = "admin", via_api_key: bool = False, account_id: uuid.UUID = _ADMIN_ID
) -> TenantPrincipal:
    return TenantPrincipal(
        username="principal",
        organisation_id=_ORG_ID,
        account_id=account_id,
        org_role=org_role,
        via_api_key=via_api_key,
    )


# ---------------------------------------------------------------------------
# Structural admin-role rejection (the shared ceiling helper)
# ---------------------------------------------------------------------------


class TestServiceRoleCeiling:
    def test_admin_is_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _validate_service_org_role("admin")
        assert exc.value.status_code == 422
        detail = str(exc.value.detail)
        assert "admin" in detail
        assert "operator" in detail

    def test_operator_runner_viewer_pass(self) -> None:
        for role in ("operator", "runner", "viewer"):
            assert _validate_service_org_role(role) == role

    def test_unknown_role_is_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _validate_service_org_role("superuser")
        assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/service-accounts — provisioning
# ---------------------------------------------------------------------------


class TestCreateServiceAccount:
    def test_create_returns_201_and_stamps_kind(self, admin_client: TestClient) -> None:
        account = _service_account()
        with (
            patch(
                "modulo.api.routes.service_accounts.create_service_account",
                AsyncMock(return_value=account),
            ) as create,
            patch(
                "modulo.api.routes.service_accounts.create_membership",
                AsyncMock(return_value=_membership("operator")),
            ),
            patch("modulo.core.audit_logger.append_audit_event", new=AsyncMock()),
        ):
            resp = admin_client.post("/api/v1/service-accounts", json={"name": "ci-bot", "role": "operator"})

        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == str(_SA_ID)
        assert body["kind"] == KIND_SERVICE
        assert body["role"] == "operator"
        assert body["name"] == "ci-bot"
        create.assert_awaited_once()

    def test_create_emits_service_account_created_audit(self, admin_client: TestClient) -> None:
        account = _service_account()
        audit = AsyncMock(return_value=MagicMock())
        with (
            patch("modulo.api.routes.service_accounts.create_service_account", AsyncMock(return_value=account)),
            patch(
                "modulo.api.routes.service_accounts.create_membership",
                AsyncMock(return_value=_membership("operator")),
            ),
            patch("modulo.core.audit_logger.append_audit_event", new=audit),
        ):
            resp = admin_client.post("/api/v1/service-accounts", json={"name": "ci-bot"})

        assert resp.status_code == 201
        rich = [c for c in audit.await_args_list if c.kwargs.get("event_type") == "service_account_created"]
        assert len(rich) == 1
        kwargs = rich[0].kwargs
        assert kwargs["resource_type"] == "service_account"
        assert kwargs["resource_id"] == _SA_ID
        assert kwargs["payload_json"]["role"] == "operator"

    def test_create_with_admin_role_is_rejected_structurally(self, admin_client: TestClient) -> None:
        with patch(
            "modulo.api.routes.service_accounts.create_service_account",
            AsyncMock(),
        ) as create:
            resp = admin_client.post("/api/v1/service-accounts", json={"name": "ci-bot", "role": "admin"})

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "admin" in detail
        assert "operator" in detail
        create.assert_not_called()

    def test_create_with_unknown_role_is_rejected(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/v1/service-accounts", json={"name": "ci-bot", "role": "root"})
        assert resp.status_code == 422

    def test_create_with_blank_name_is_rejected(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/v1/service-accounts", json={"name": "   "})
        assert resp.status_code == 422

    def test_create_non_admin_is_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post("/api/v1/service-accounts", json={"name": "ci-bot"})
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET list / read, PATCH rename
# ---------------------------------------------------------------------------


class TestReadAndUpdate:
    def test_list_returns_service_accounts(self, admin_client: TestClient) -> None:
        rows = [(_service_account(), _membership("operator"))]
        with patch("modulo.api.routes.service_accounts._list_service_accounts", AsyncMock(return_value=(rows, 1))):
            resp = admin_client.get("/api/v1/service-accounts")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == str(_SA_ID)
        assert body["items"][0]["role"] == "operator"

    def test_get_returns_one(self, admin_client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.service_accounts.get_account_by_id",
                AsyncMock(return_value=_service_account()),
            ),
            patch(
                "modulo.api.routes.service_accounts.get_membership_by_account_and_org",
                AsyncMock(return_value=_membership()),
            ),
        ):
            resp = admin_client.get(f"/api/v1/service-accounts/{_SA_ID}")

        assert resp.status_code == 200
        assert resp.json()["id"] == str(_SA_ID)

    def test_get_human_account_is_404(self, admin_client: TestClient) -> None:
        human = _service_account()
        human.kind = "human"
        with (
            patch("modulo.api.routes.service_accounts.get_account_by_id", AsyncMock(return_value=human)),
            patch(
                "modulo.api.routes.service_accounts.get_membership_by_account_and_org",
                AsyncMock(return_value=_membership()),
            ),
        ):
            resp = admin_client.get(f"/api/v1/service-accounts/{_SA_ID}")
        assert resp.status_code == 404

    def test_patch_rename(self, admin_client: TestClient) -> None:
        account = _service_account()
        with (
            patch("modulo.api.routes.service_accounts.get_account_by_id", AsyncMock(return_value=account)),
            patch(
                "modulo.api.routes.service_accounts.get_membership_by_account_and_org",
                AsyncMock(return_value=_membership()),
            ),
            patch("modulo.core.audit_logger.append_audit_event", new=AsyncMock()) as audit,
        ):
            resp = admin_client.patch(f"/api/v1/service-accounts/{_SA_ID}", json={"name": "renamed-bot"})

        assert resp.status_code == 200
        assert resp.json()["name"] == "renamed-bot"
        rich = [c for c in audit.await_args_list if c.kwargs.get("event_type") == "service_account_updated"]
        assert len(rich) == 1

    def test_patch_role_change(self, admin_client: TestClient) -> None:
        membership = _membership("viewer")
        with (
            patch(
                "modulo.api.routes.service_accounts.get_account_by_id",
                AsyncMock(return_value=_service_account()),
            ),
            patch(
                "modulo.api.routes.service_accounts.get_membership_by_account_and_org",
                AsyncMock(return_value=membership),
            ),
            patch("modulo.core.audit_logger.append_audit_event", new=AsyncMock()),
        ):
            resp = admin_client.patch(f"/api/v1/service-accounts/{_SA_ID}", json={"role": "runner"})

        assert resp.status_code == 200
        assert membership.role == "runner"

    def test_patch_admin_role_is_rejected(self, admin_client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.service_accounts.get_account_by_id",
                AsyncMock(return_value=_service_account()),
            ),
            patch(
                "modulo.api.routes.service_accounts.get_membership_by_account_and_org",
                AsyncMock(return_value=_membership()),
            ),
        ):
            resp = admin_client.patch(f"/api/v1/service-accounts/{_SA_ID}", json={"role": "admin"})

        assert resp.status_code == 422

    def test_patch_decommissioned_is_409(self, admin_client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.service_accounts.get_account_by_id",
                AsyncMock(return_value=_service_account(decommissioned=True)),
            ),
            patch(
                "modulo.api.routes.service_accounts.get_membership_by_account_and_org",
                AsyncMock(return_value=_membership()),
            ),
        ):
            resp = admin_client.patch(f"/api/v1/service-accounts/{_SA_ID}", json={"name": "x"})

        assert resp.status_code == 409

    def test_patch_without_fields_is_422(self, admin_client: TestClient) -> None:
        resp = admin_client.patch(f"/api/v1/service-accounts/{_SA_ID}", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Decommission
# ---------------------------------------------------------------------------


def _decommission_execute_side_effect(revoked: int, team_memberships: list[Any]) -> Any:
    update_result = MagicMock()
    update_result.rowcount = revoked
    select_result = MagicMock()
    select_result.scalars.return_value = iter(team_memberships)
    return [update_result, select_result]


class TestDecommission:
    def test_decommission_revokes_keys_and_tombstones(self) -> None:
        account = _service_account()
        membership = _membership()
        team = MagicMock()
        mock_session = _make_mock_session()
        mock_session.execute = AsyncMock(side_effect=_decommission_execute_side_effect(2, [team]))

        async def override_session() -> AsyncMock:
            return mock_session

        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="adminuser",
            organisation_id=_ORG_ID,
            account_id=_ADMIN_ID,
            org_role="admin",
        )
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        audit = AsyncMock(return_value=MagicMock())
        try:
            with (
                patch("modulo.api.routes.service_accounts.get_account_by_id", AsyncMock(return_value=account)),
                patch(
                    "modulo.api.routes.service_accounts.get_membership_by_account_and_org",
                    AsyncMock(return_value=membership),
                ),
                patch("modulo.core.audit_logger.append_audit_event", new=audit),
            ):
                resp = TestClient(app).post(f"/api/v1/service-accounts/{_SA_ID}/decommission")
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["revoked_keys"] == 2
        assert body["team_memberships_removed"] == 1
        assert account.decommissioned_at is not None
        assert account.active is False
        assert membership.deactivated_at is not None
        rich = [c for c in audit.await_args_list if c.kwargs.get("event_type") == "service_account_decommissioned"]
        assert len(rich) == 1

    def test_decommission_twice_is_409(self, admin_client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.service_accounts.get_account_by_id",
                AsyncMock(return_value=_service_account(decommissioned=True)),
            ),
            patch(
                "modulo.api.routes.service_accounts.get_membership_by_account_and_org",
                AsyncMock(return_value=_membership()),
            ),
        ):
            resp = admin_client.post(f"/api/v1/service-accounts/{_SA_ID}/decommission")
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /{id}/keys — ownership is the service principal
# ---------------------------------------------------------------------------


@pytest.fixture
def mint_client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    """Admin JWT client whose principal is resolved through the any-credential dependency."""
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: _tenant_principal()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app), mock_session
    app.dependency_overrides.clear()


@pytest.fixture
def self_mint_client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    """Service principal authenticating with its own ``mk_`` credential."""
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: _tenant_principal(
        org_role="operator", via_api_key=True, account_id=_SA_ID
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app), mock_session
    app.dependency_overrides.clear()


class TestMintServiceAccountKey:
    def test_admin_bootstrap_mint_owns_the_service_principal(self, mint_client: tuple[TestClient, AsyncMock]) -> None:
        client, _ = mint_client
        key = _api_key()
        with (
            patch("modulo.api.routes.service_accounts.get_account_by_id", AsyncMock(return_value=_service_account())),
            patch(
                "modulo.api.routes.service_accounts.get_membership_by_account_and_org",
                AsyncMock(return_value=_membership()),
            ),
            patch(
                "modulo.api.routes.service_accounts.resolve_role_from_membership",
                AsyncMock(return_value="operator"),
            ),
            patch(
                "modulo.api.routes.service_accounts.create_api_key",
                AsyncMock(return_value=(key, "mk_full_key")),
            ) as mint,
            patch("modulo.core.audit_logger.append_audit_event", new=AsyncMock()) as audit,
        ):
            resp = client.post(f"/api/v1/service-accounts/{_SA_ID}/keys", json={"name": "ci key", "role": "runner"})

        assert resp.status_code == 201
        body = resp.json()
        assert body["key_value"] == "mk_full_key"
        assert body["owner_account_id"] == str(_SA_ID)
        assert body["scope"] == "org"
        kwargs = mint.await_args.kwargs
        assert kwargs["account_id"] == _SA_ID
        assert kwargs["scope"] == "org"
        rich = [c for c in audit.await_args_list if c.kwargs.get("event_type") == "api_key_created"]
        assert len(rich) == 1
        assert rich[0].kwargs["payload_json"]["owner_account_id"] == str(_SA_ID)

    def test_service_principal_mints_its_own_key(self, self_mint_client: tuple[TestClient, AsyncMock]) -> None:
        client, _ = self_mint_client
        key = _api_key()
        with (
            patch("modulo.api.routes.service_accounts.get_account_by_id", AsyncMock(return_value=_service_account())),
            patch(
                "modulo.api.routes.service_accounts.get_membership_by_account_and_org",
                AsyncMock(return_value=_membership()),
            ),
            patch(
                "modulo.api.routes.service_accounts.resolve_role_from_membership",
                AsyncMock(return_value="operator"),
            ),
            patch(
                "modulo.api.routes.service_accounts.create_api_key",
                AsyncMock(return_value=(key, "mk_full_key")),
            ),
            patch("modulo.core.audit_logger.append_audit_event", new=AsyncMock()),
        ):
            resp = client.post(f"/api/v1/service-accounts/{_SA_ID}/keys", json={"name": "ci key"})

        assert resp.status_code == 201
        assert resp.json()["owner_account_id"] == str(_SA_ID)

    def test_api_key_cannot_mint_for_another_principal(self, self_mint_client: tuple[TestClient, AsyncMock]) -> None:
        client, _ = self_mint_client
        other_id = uuid.uuid4()
        resp = client.post(f"/api/v1/service-accounts/{other_id}/keys", json={"name": "ci key"})
        assert resp.status_code == 403

    def test_non_admin_jwt_cannot_mint_for_a_service_account(self, mint_client: tuple[TestClient, AsyncMock]) -> None:
        client, _ = mint_client
        app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: _tenant_principal(org_role="operator")
        resp = client.post(f"/api/v1/service-accounts/{_SA_ID}/keys", json={"name": "ci key"})
        assert resp.status_code == 403

    def test_ttl_ceiling_maps_to_422(self, mint_client: tuple[TestClient, AsyncMock]) -> None:
        client, _ = mint_client
        expires_at = (datetime.now(UTC) + timedelta(days=400)).isoformat()
        with (
            patch("modulo.api.routes.service_accounts.get_account_by_id", AsyncMock(return_value=_service_account())),
            patch(
                "modulo.api.routes.service_accounts.get_membership_by_account_and_org",
                AsyncMock(return_value=_membership()),
            ),
            patch(
                "modulo.api.routes.service_accounts.resolve_role_from_membership",
                AsyncMock(return_value="operator"),
            ),
            patch(
                "modulo.api.routes.service_accounts.create_api_key",
                AsyncMock(side_effect=ApiKeyTtlError("Keys owned by a service account expire within 365 days")),
            ),
        ):
            resp = client.post(
                f"/api/v1/service-accounts/{_SA_ID}/keys",
                json={"name": "ci key", "expires_at": expires_at},
            )
        assert resp.status_code == 422
        assert "365" in resp.json()["detail"]

    def test_decommissioned_target_is_409(self, mint_client: tuple[TestClient, AsyncMock]) -> None:
        client, _ = mint_client
        with (
            patch(
                "modulo.api.routes.service_accounts.get_account_by_id",
                AsyncMock(return_value=_service_account(decommissioned=True)),
            ),
            patch(
                "modulo.api.routes.service_accounts.get_membership_by_account_and_org",
                AsyncMock(return_value=_membership()),
            ),
        ):
            resp = client.post(f"/api/v1/service-accounts/{_SA_ID}/keys", json={"name": "ci key"})
        assert resp.status_code == 409

    def test_role_above_target_live_role_is_403(self, mint_client: tuple[TestClient, AsyncMock]) -> None:
        client, _ = mint_client
        with (
            patch("modulo.api.routes.service_accounts.get_account_by_id", AsyncMock(return_value=_service_account())),
            patch(
                "modulo.api.routes.service_accounts.get_membership_by_account_and_org",
                AsyncMock(return_value=_membership()),
            ),
            patch(
                "modulo.api.routes.service_accounts.resolve_role_from_membership",
                AsyncMock(return_value="viewer"),
            ),
        ):
            resp = client.post(f"/api/v1/service-accounts/{_SA_ID}/keys", json={"name": "ci key", "role": "runner"})
        assert resp.status_code == 403

    def test_admin_key_role_is_422(self, mint_client: tuple[TestClient, AsyncMock]) -> None:
        client, _ = mint_client
        resp = client.post(f"/api/v1/service-accounts/{_SA_ID}/keys", json={"name": "ci key", "role": "admin"})
        assert resp.status_code == 422

    def test_user_scope_is_rejected(self, mint_client: tuple[TestClient, AsyncMock]) -> None:
        client, _ = mint_client
        resp = client.post(
            f"/api/v1/service-accounts/{_SA_ID}/keys",
            json={"name": "ci key", "scope": "user"},
        )
        assert resp.status_code == 422

    def test_grants_with_flag_off_are_rejected(self, mint_client: tuple[TestClient, AsyncMock]) -> None:
        client, _ = mint_client
        with patch("modulo.api.routes.service_accounts.api_key_grants_enabled", AsyncMock(return_value=False)):
            resp = client.post(
                f"/api/v1/service-accounts/{_SA_ID}/keys",
                json={"name": "ci key", "grants": ["pipeline.list"]},
            )
        assert resp.status_code == 422

    def test_grants_flow_through_the_registry_mint_cap(self, mint_client: tuple[TestClient, AsyncMock]) -> None:
        client, mock_session = mint_client
        key = _api_key()
        with (
            patch("modulo.api.routes.service_accounts.get_account_by_id", AsyncMock(return_value=_service_account())),
            patch(
                "modulo.api.routes.service_accounts.get_membership_by_account_and_org",
                AsyncMock(return_value=_membership()),
            ),
            patch(
                "modulo.api.routes.service_accounts.resolve_role_from_membership",
                AsyncMock(return_value="operator"),
            ),
            patch("modulo.api.routes.service_accounts.api_key_grants_enabled", AsyncMock(return_value=True)),
            patch(
                "modulo.api.routes.service_accounts.enforce_grants_mint_cap_for",
                AsyncMock(),
            ) as grant_cap,
            patch(
                "modulo.api.routes.service_accounts.create_api_key",
                AsyncMock(return_value=(key, "mk_full_key")),
            ),
            patch("modulo.core.audit_logger.append_audit_event", new=AsyncMock()),
        ):
            resp = client.post(
                f"/api/v1/service-accounts/{_SA_ID}/keys",
                json={"name": "ci key", "grants": ["pipeline.list"]},
            )
        assert resp.status_code == 201
        grant_cap.assert_awaited_once()
        assert grant_cap.await_args.args[0] is mock_session
        assert grant_cap.await_args.args[1] == _SA_ID
        assert grant_cap.await_args.args[3] == ["pipeline.list"]
