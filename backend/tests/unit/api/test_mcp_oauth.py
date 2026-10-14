"""Unit tests for /api/v1/mcp/oauth/* endpoints."""

import uuid
from collections.abc import AsyncGenerator, Callable, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.audit_coverage import audit_session
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 6, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _stub_audit_session() -> Generator[None, None, None]:
    """FAR-1472: the fail-closed ``audited(...)`` dependency writes its event on a
    fresh ``audit_session`` (a real engine — no database in the unit tier), so
    stub that seam; the dependency itself still runs."""

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
        modulo_public_url="https://modulo.example.com",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_admin_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )


def _make_runner_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="runner",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="runner",
    )


@pytest.fixture
def admin_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = _make_admin_principal
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def runner_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = _make_runner_principal
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def viewer_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="viewer",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def _team_query_result(found: bool) -> MagicMock:
    """A DB result whose ``first()`` reports a row (found) or none."""
    result = MagicMock()
    result.first.return_value = (uuid.uuid4(),) if found else None
    return result


def _make_team_mock_session(*, team_exists: bool, is_member: bool) -> AsyncMock:
    """A mock session whose ``execute`` answers the two team-binding queries.

    ``_validate_oauth_team_binding`` runs inside the transaction and issues two
    SELECTs: the ``Team.id`` existence probe and (for runners) the
    ``TeamMembership`` membership probe via ``team_membership_exists``. Both go
    through ``session.execute``, so branch on the FROM clause.
    """
    session = _make_mock_session()

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        sql = str(stmt)
        if "FROM team_memberships" in sql:
            return _team_query_result(is_member)
        return _team_query_result(team_exists)

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _client_with_session(
    mock_session: AsyncMock,
    principal_factory: Callable[[], AuthenticatedPrincipal],
) -> TestClient:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = principal_factory
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    return TestClient(app)


_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_REG_PAYLOAD = {
    "name": "App",
    "redirect_uris": ["http://localhost/cb"],
    "scopes": ["trigger:run"],
}


# ---------------------------------------------------------------------------
# GET /api/v1/mcp/oauth/scopes - the registration picker's source of truth
# ---------------------------------------------------------------------------


class TestListOAuthScopes:
    ENDPOINT = "/api/v1/mcp/oauth/scopes"

    def test_offers_only_delegable_registry_keys(self, admin_client: TestClient) -> None:
        """The picker can never offer a scope the registration boundary rejects."""
        from modulo.auth.permissions import PERMISSIONS, is_delegable

        resp = admin_client.get(self.ENDPOINT)

        assert resp.status_code == 200
        items = resp.json()
        assert items, "the delegable vocabulary must not be empty"
        for item in items:
            assert item["key"] in PERMISSIONS
            assert is_delegable(item["key"]) is True
            assert item["min_role"] == PERMISSIONS[item["key"]]
        offered = {item["key"] for item in items}
        # Non-delegable keys are never offered (fail-closed vocabulary).
        assert "org.delete" not in offered
        assert "api_key.create" not in offered
        assert "system.config.manage" not in offered
        assert "org.authz_enforce.manage" not in offered

    def test_hitl_decision_keys_are_offered(self, admin_client: TestClient) -> None:
        """Decision record 2026-10-09: the widened vocabulary reaches the UI."""
        resp = admin_client.get(self.ENDPOINT)
        assert resp.status_code == 200
        offered = {item["key"] for item in resp.json()}
        assert {"hitl.review", "hitl.approve", "hitl.claim", "hitl.reject", "hitl.deliver_manual"} <= offered
        # The widened set is the whole delegable registry, not three legacy scopes.
        assert "run.trigger" in offered
        assert "pipeline.create" in offered
        assert len(offered) > 100

    def test_filtered_to_the_callers_role_level(self, runner_client: TestClient) -> None:
        """A runner is never offered a scope above its own floor (no dead controls)."""
        resp = runner_client.get(self.ENDPOINT)
        assert resp.status_code == 200
        items = resp.json()
        assert items
        levels = {"viewer": 0, "runner": 1, "operator": 2, "admin": 3}
        assert all(levels[item["min_role"]] <= levels["runner"] for item in items)
        assert "run.trigger" in {item["key"] for item in items}
        assert "pipeline.create" not in {item["key"] for item in items}

    def test_sorted_and_role_scoped_result_is_stable(self, admin_client: TestClient) -> None:
        first = [item["key"] for item in admin_client.get(self.ENDPOINT).json()]
        second = [item["key"] for item in admin_client.get(self.ENDPOINT).json()]
        assert first == sorted(first)
        assert first == second

    def test_viewer_gets_403(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get(self.ENDPOINT)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/v1/mcp/oauth/clients
# ---------------------------------------------------------------------------


class TestRegisterOAuthClient:
    ENDPOINT = "/api/v1/mcp/oauth/clients"

    def test_create_returns_201_with_secret(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_client = MagicMock()
            mock_client.id = uuid.uuid4()
            mock_client.client_id = "abc123def4567890"
            mock_client.name = "My App"
            mock_create.return_value = (mock_client, "raw_secret_40_chars_long_here")

            resp = admin_client.post(
                self.ENDPOINT,
                json={
                    "name": "My App",
                    "redirect_uris": ["https://app.example.com/callback"],
                    "scopes": ["trigger:run"],
                },
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["client_id"] == "abc123def4567890"
        assert body["client_secret"] == "raw_secret_40_chars_long_here"
        assert body["name"] == "My App"
        assert "id" in body

    def test_create_rejects_missing_name(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            self.ENDPOINT,
            json={"redirect_uris": ["http://localhost/cb"], "scopes": ["trigger:run"]},
        )
        assert resp.status_code == 422

    def test_create_rejects_empty_redirect_uris(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            self.ENDPOINT,
            json={"name": "App", "redirect_uris": [], "scopes": ["trigger:run"]},
        )
        assert resp.status_code == 422

    def test_create_rejects_empty_scopes(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            self.ENDPOINT,
            json={
                "name": "App",
                "redirect_uris": ["http://localhost/cb"],
                "scopes": [],
            },
        )
        assert resp.status_code == 422

    def test_create_runner_without_team_gets_400(self, runner_client: TestClient) -> None:
        """FAR-1476: a runner may register, but MUST bind a team (never org-wide)."""
        resp = runner_client.post(
            self.ENDPOINT,
            json={
                "name": "App",
                "redirect_uris": ["http://localhost/cb"],
                "scopes": ["trigger:run"],
            },
        )
        assert resp.status_code == 400
        assert "must be bound to a team" in resp.json()["detail"]

    def test_create_disallows_invalid_scopes(self, admin_client: TestClient) -> None:
        with patch("modulo.api.routes.mcp_oauth.set_rls_org"):
            resp = admin_client.post(
                self.ENDPOINT,
                json={
                    "name": "App",
                    "redirect_uris": ["http://localhost/cb"],
                    "scopes": ["unknown:scope"],
                },
            )
        assert resp.status_code == 400

    def test_create_accepts_loopback_http_redirect_uris(self, admin_client: TestClient) -> None:
        """http:// is fine for loopback so local development keeps working."""
        with (
            patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_client = MagicMock()
            mock_client.id = uuid.uuid4()
            mock_client.client_id = "abc123def4567890"
            mock_client.name = "Local App"
            mock_create.return_value = (mock_client, "raw_secret_40_chars_long_here")
            resp = admin_client.post(
                self.ENDPOINT,
                json={
                    "name": "Local App",
                    "redirect_uris": ["http://localhost:5173/cb", "http://127.0.0.1:8080/cb"],
                    "scopes": ["trigger:run"],
                },
            )
        assert resp.status_code == 201
        assert mock_create.call_args.kwargs["redirect_uris"] == "http://localhost:5173/cb http://127.0.0.1:8080/cb"

    def test_create_stores_valid_redirect_uris_verbatim(self, admin_client: TestClient) -> None:
        """What is validated is exactly what is stored (space-joined, lossless)."""
        with (
            patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_client = MagicMock()
            mock_client.id = uuid.uuid4()
            mock_client.client_id = "abc123def4567890"
            mock_client.name = "App"
            mock_create.return_value = (mock_client, "raw_secret_40_chars_long_here")
            resp = admin_client.post(
                self.ENDPOINT,
                json={
                    "name": "App",
                    "redirect_uris": ["https://a.example/cb?next=%2Fhome", "https://b.example/cb"],
                    "scopes": ["trigger:run"],
                },
            )
        assert resp.status_code == 201
        stored = mock_create.call_args.kwargs["redirect_uris"]
        assert stored.split() == ["https://a.example/cb?next=%2Fhome", "https://b.example/cb"]

    @pytest.mark.parametrize(
        "uri",
        [
            "/relative/cb",
            "app.example.com/cb",
            "javascript:alert(document.domain)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "file:///etc/passwd",
            "myapp://cb",
            "https://*.example.com/cb",
            "*",
            "https://a.example/cb#frag",
            "https://user:pw@a.example/cb",
            "http://a.example/cb",
            "https://a.example.com/call back",
            "https://a.example.com/cb\nhttps://evil.example/cb",
        ],
        ids=[
            "relative-path",
            "scheme-less",
            "javascript-scheme",
            "data-scheme",
            "file-scheme",
            "custom-scheme",
            "wildcard-host",
            "bare-wildcard",
            "fragment",
            "userinfo",
            "non-loopback-http",
            "internal-space",
            "embedded-newline",
        ],
    )
    def test_create_rejects_invalid_redirect_uris_with_400(self, admin_client: TestClient, uri: str) -> None:
        """Every previously-accepted invalid value is now a structured 400."""
        with (
            patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = admin_client.post(
                self.ENDPOINT,
                json={"name": "App", "redirect_uris": [uri], "scopes": ["trigger:run"]},
            )
        assert resp.status_code == 400, resp.text
        assert "invalid_redirect_uri" in resp.json()["detail"]
        mock_create.assert_not_called()

    def test_create_rejects_whitespace_entry_before_storage(self, admin_client: TestClient) -> None:
        """A URI with an internal space would round-trip as TWO URIs — reject it."""
        with (
            patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = admin_client.post(
                self.ENDPOINT,
                json={
                    "name": "App",
                    "redirect_uris": ["https://a.example/cb", "https://b.example/call back"],
                    "scopes": ["trigger:run"],
                },
            )
        assert resp.status_code == 400
        assert "whitespace" in resp.json()["detail"]
        mock_create.assert_not_called()

    def test_create_400_names_every_offending_entry(self, admin_client: TestClient) -> None:
        with patch("modulo.api.routes.mcp_oauth.set_rls_org"):
            resp = admin_client.post(
                self.ENDPOINT,
                json={
                    "name": "App",
                    "redirect_uris": ["https://a.example/cb", "javascript:alert(1)", "/relative"],
                    "scopes": ["trigger:run"],
                },
            )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "javascript:alert(1)" in detail
        assert "/relative" in detail

    def test_create_requires_public_url(self, admin_client: TestClient) -> None:
        def _settings_no_url() -> Settings:
            return Settings(
                database_url="postgresql+asyncpg://localhost/test",
                secret_key=_VALID_32,
                fernet_key=_VALID_32,
                modulo_admin_password="testpass",
                modulo_public_url="http://localhost:8000",
            )

        app.dependency_overrides[get_settings] = _settings_no_url
        try:
            with patch("modulo.api.routes.mcp_oauth.set_rls_org"):
                resp = admin_client.post(
                    self.ENDPOINT,
                    json={
                        "name": "App",
                        "redirect_uris": ["http://localhost/cb"],
                        "scopes": ["trigger:run"],
                    },
                )
            assert resp.status_code == 500
            assert "MODULO_PUBLIC_URL" in resp.json()["detail"]
        finally:
            app.dependency_overrides[get_settings] = _make_settings

    def test_create_propagates_http_exception(self, admin_client: TestClient) -> None:
        """An HTTPException raised by the save path is re-raised unchanged."""
        with (
            patch(
                "modulo.api.routes.mcp_oauth.create_oauth_client",
                side_effect=HTTPException(status_code=409, detail="duplicate"),
            ),
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = admin_client.post(
                self.ENDPOINT,
                json={
                    "name": "App",
                    "redirect_uris": ["http://localhost/cb"],
                    "scopes": ["trigger:run"],
                },
            )

        assert resp.status_code == 409
        assert resp.json()["detail"] == "duplicate"


# ---------------------------------------------------------------------------
# Team-bound registration (FAR-1476 slice 1)
# ---------------------------------------------------------------------------


class TestRegisterOAuthClientTeamBinding:
    """The explicit membership rule for OAuth client ``team_id`` (FAR-1476).

    - admin/operator may bind any team in their org, or none (org-wide).
    - runner may bind ONLY a team they are a member of, and MUST bind one.
    - the team must exist in the caller's org (404 otherwise).
    """

    ENDPOINT = "/api/v1/mcp/oauth/clients"

    def test_admin_binds_valid_team_and_persists_team_id(self) -> None:
        """An admin binding a team in their org succeeds and persists team_id."""
        mock_session = _make_team_mock_session(team_exists=True, is_member=False)
        client = _client_with_session(mock_session, _make_admin_principal)
        try:
            with (
                patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
                patch("modulo.api.routes.mcp_oauth.set_rls_org"),
            ):
                mock_client = MagicMock()
                mock_client.id = uuid.uuid4()
                mock_client.client_id = "abc123def4567890"
                mock_client.name = "App"
                mock_create.return_value = (mock_client, "raw_secret")

                resp = client.post(
                    self.ENDPOINT,
                    json={**_REG_PAYLOAD, "team_id": str(_TEAM_ID)},
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 201
        assert mock_create.call_args.kwargs["team_id"] == _TEAM_ID

    def test_admin_binds_team_from_other_org_gets_404(self) -> None:
        """A team outside the caller's org is never persisted (404, fail-closed)."""
        mock_session = _make_team_mock_session(team_exists=False, is_member=False)
        client = _client_with_session(mock_session, _make_admin_principal)
        try:
            with (
                patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
                patch("modulo.api.routes.mcp_oauth.set_rls_org"),
            ):
                resp = client.post(
                    self.ENDPOINT,
                    json={**_REG_PAYLOAD, "team_id": str(_TEAM_ID)},
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 404
        assert "not found in this organisation" in resp.json()["detail"]
        mock_create.assert_not_called()

    def test_runner_binds_member_team_succeeds(self) -> None:
        """A runner CAN register when they bind a team they are a member of."""
        mock_session = _make_team_mock_session(team_exists=True, is_member=True)
        client = _client_with_session(mock_session, _make_runner_principal)
        try:
            with (
                patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
                patch("modulo.api.routes.mcp_oauth.set_rls_org"),
            ):
                mock_client = MagicMock()
                mock_client.id = uuid.uuid4()
                mock_client.client_id = "abc123def4567890"
                mock_client.name = "App"
                mock_create.return_value = (mock_client, "raw_secret")

                resp = client.post(
                    self.ENDPOINT,
                    json={**_REG_PAYLOAD, "team_id": str(_TEAM_ID)},
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 201
        assert mock_create.call_args.kwargs["team_id"] == _TEAM_ID

    def test_runner_binding_non_member_team_gets_403(self) -> None:
        """A runner may NOT bind a team they are not a member of."""
        mock_session = _make_team_mock_session(team_exists=True, is_member=False)
        client = _client_with_session(mock_session, _make_runner_principal)
        try:
            with (
                patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
                patch("modulo.api.routes.mcp_oauth.set_rls_org"),
            ):
                resp = client.post(
                    self.ENDPOINT,
                    json={**_REG_PAYLOAD, "team_id": str(_TEAM_ID)},
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 403
        assert "member" in resp.json()["detail"]
        mock_create.assert_not_called()

    def test_runner_without_team_gets_400(self) -> None:
        """A runner-registered client is NEVER org-wide (no NULL boundary)."""
        mock_session = _make_team_mock_session(team_exists=True, is_member=True)
        client = _client_with_session(mock_session, _make_runner_principal)
        try:
            with (
                patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
                patch("modulo.api.routes.mcp_oauth.set_rls_org"),
            ):
                resp = client.post(self.ENDPOINT, json=dict(_REG_PAYLOAD))
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 400
        assert "must be bound to a team" in resp.json()["detail"]
        mock_create.assert_not_called()

    def test_admin_without_team_stays_org_wide(self) -> None:
        """An admin with no team_id still registers an org-wide client (None)."""
        mock_session = _make_team_mock_session(team_exists=False, is_member=False)
        client = _client_with_session(mock_session, _make_admin_principal)
        try:
            with (
                patch("modulo.api.routes.mcp_oauth.create_oauth_client") as mock_create,
                patch("modulo.api.routes.mcp_oauth.set_rls_org"),
            ):
                mock_client = MagicMock()
                mock_client.id = uuid.uuid4()
                mock_client.client_id = "abc123def4567890"
                mock_client.name = "App"
                mock_create.return_value = (mock_client, "raw_secret")

                resp = client.post(self.ENDPOINT, json=dict(_REG_PAYLOAD))
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 201
        assert mock_create.call_args.kwargs["team_id"] is None


# ---------------------------------------------------------------------------
# GET /api/v1/mcp/oauth/clients
# ---------------------------------------------------------------------------


class TestListOAuthClients:
    ENDPOINT = "/api/v1/mcp/oauth/clients"

    def test_list_returns_200(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.list_oauth_clients") as mock_list,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_list.return_value = [
                {
                    "id": str(uuid.uuid4()),
                    "client_id": "cid1",
                    "name": "App 1",
                    "scopes": ["trigger:run"],
                    "redirect_uris": ["http://localhost/cb"],
                    "created_at": _NOW.isoformat(),
                }
            ]
            resp = admin_client.get(self.ENDPOINT)

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["name"] == "App 1"

    def test_list_empty(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.list_oauth_clients") as mock_list,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_list.return_value = []
            resp = admin_client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert not resp.json()

    def test_list_viewer_gets_403(self, viewer_client: TestClient) -> None:
        """A viewer must not enumerate OAuth clients (redirect_uris/scopes attack surface)."""
        resp = viewer_client.get(self.ENDPOINT)
        assert resp.status_code == 403
        assert "Only admin or operator users can list OAuth clients" in resp.json()["detail"]

    def test_list_propagates_http_exception(self, admin_client: TestClient) -> None:
        """An HTTPException raised by the query path is re-raised unchanged."""
        with (
            patch(
                "modulo.api.routes.mcp_oauth.list_oauth_clients",
                side_effect=HTTPException(status_code=409, detail="duplicate"),
            ),
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = admin_client.get(self.ENDPOINT)

        assert resp.status_code == 409
        assert resp.json()["detail"] == "duplicate"


# ---------------------------------------------------------------------------
# DELETE /api/v1/mcp/oauth/clients/{client_id}
# ---------------------------------------------------------------------------


class TestDeleteOAuthClient:
    ENDPOINT = "/api/v1/mcp/oauth/clients"

    def test_delete_returns_200(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.delete_oauth_client") as mock_delete,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_delete.return_value = True
            resp = admin_client.delete(f"{self.ENDPOINT}/myclient123")

        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_delete_not_found_returns_404(self, admin_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.delete_oauth_client") as mock_delete,
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            mock_delete.return_value = False
            resp = admin_client.delete(f"{self.ENDPOINT}/nonexistent")

        assert resp.status_code == 404

    def test_delete_runner_gets_403(self, runner_client: TestClient) -> None:
        resp = runner_client.delete(f"{self.ENDPOINT}/someclient")
        assert resp.status_code == 403

    def test_delete_propagates_http_exception(self, admin_client: TestClient) -> None:
        """An HTTPException raised by the delete path is re-raised unchanged."""
        with (
            patch(
                "modulo.api.routes.mcp_oauth.delete_oauth_client",
                side_effect=HTTPException(status_code=409, detail="duplicate"),
            ),
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = admin_client.delete(f"{self.ENDPOINT}/myclient123")

        assert resp.status_code == 409
        assert resp.json()["detail"] == "duplicate"


# ---------------------------------------------------------------------------
# Unauthenticated access
# ---------------------------------------------------------------------------


def test_list_returns_401_without_auth() -> None:
    app.dependency_overrides[get_settings] = _make_settings
    resp = TestClient(app).get("/api/v1/mcp/oauth/clients")
    assert resp.status_code in (401, 403)
    app.dependency_overrides.clear()
