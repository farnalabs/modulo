"""Router-level unit tests for /api/v1/environment-profiles (CRUD + sandbox test).

The `/environment-profiles` router is the single surviving Environment Profiles
surface (FAR-551 collapsed the duplicate `/api/v1/environments` router into it).
These tests exercise the router in isolation with a mocked session + CRUD layer.
"""

import json
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.runtime_provider import ProviderNotConfiguredError
from modulo.core.runtime_provider.hub import RuntimeProviderHub
from modulo.db.crud.base import PageResult
from modulo.db.models.environment_profile import PROVIDER_TYPES
from modulo.settings import Settings, get_settings
from modulo.util import WorkspaceNetworkValidationError
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PROFILE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")

_ROUTES = "modulo.api.routes.environment_profiles"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    authz_result = MagicMock()
    authz_result.scalar_one_or_none = MagicMock(return_value=True)
    session.execute = AsyncMock(return_value=authz_result)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _fake_profile(**overrides: Any) -> MagicMock:
    p = MagicMock()
    p.id = overrides.get("id", _PROFILE_ID)
    p.organisation_id = overrides.get("organisation_id", _ORG_ID)
    p.name = overrides.get("name", "test-profile")
    p.description = overrides.get("description", "A test profile")
    p.provider_type = overrides.get("provider_type", "local_docker")
    p.image_ref = overrides.get("image_ref", "python:3.12-slim")
    p.capabilities = overrides.get("capabilities", ["docker"])
    p.capabilities_json = overrides.get("capabilities", ["docker"])
    p.config_json = overrides.get("config_json", {})
    p.egress_policy = overrides.get("egress_policy", "allow_all")
    p.network_policy = overrides.get("network_policy", "outbound")
    p.initialisation_strategy = overrides.get("initialisation_strategy", "git_clone")
    p.secret_refs_json = overrides.get("secret_refs", [])
    p.timeout_seconds = overrides.get("timeout_seconds", 3600)
    p.resource_limits_json = overrides.get("resource_limits", {})
    p.persistence_policy = overrides.get("persistence_policy", "ephemeral")
    p.status = overrides.get("status", "active")
    p.visibility = overrides.get("visibility", "org")
    p.owner_team_id = overrides.get("owner_team_id")
    p.is_active = overrides.get("is_active", True)
    p.created_by = overrides.get("created_by", _USER_ID)
    p.created_at = overrides.get("created_at", datetime(2026, 1, 1, tzinfo=UTC))
    p.updated_at = overrides.get("updated_at", datetime(2026, 1, 1, tzinfo=UTC))
    return p


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="tenant", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


_ENV_AUTH_CASES = [
    ("GET", "/api/v1/environment-profiles"),
    ("POST", "/api/v1/environment-profiles"),
    ("GET", f"/api/v1/environment-profiles/{_PROFILE_ID}"),
    ("PUT", f"/api/v1/environment-profiles/{_PROFILE_ID}"),
    ("DELETE", f"/api/v1/environment-profiles/{_PROFILE_ID}"),
    ("POST", f"/api/v1/environment-profiles/{_PROFILE_ID}/test"),
]


@pytest.mark.parametrize(("method", "url"), _ENV_AUTH_CASES, ids=["list", "create", "get", "update", "delete", "test"])
def test_endpoints_unauthenticated(unauth_client: TestClient, method: str, url: str) -> None:
    resp = getattr(unauth_client, method.lower())(url)
    assert resp.status_code in (401, 403), f"Expected 401/403 for {method} {url}, got {resp.status_code}"


class TestListProfiles:
    URL = "/api/v1/environment-profiles"

    def test_list_profiles_returns_paginated(self, client: TestClient) -> None:
        fake = _fake_profile()
        with (
            patch(f"{_ROUTES}.list_environment_profiles") as mock_list,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_list.return_value = PageResult(items=[fake], total=1, page=1, page_size=20)
            resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["page"] == 1
        assert data["page_size"] == 20
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "test-profile"
        assert data["items"][0]["image_ref"] == "python:3.12-slim"

    def test_list_profiles_empty(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.list_environment_profiles") as mock_list,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_list.return_value = PageResult(items=[], total=0, page=1, page_size=20)
            resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert not data["items"]


class TestCreateProfile:
    URL = "/api/v1/environment-profiles"

    PAYLOAD: ClassVar[dict[str, Any]] = {
        "name": "new-env",
        "provider_type": "e2b",
        "image_ref": "ubuntu:22.04",
        "capabilities": ["docker", "gpu"],
    }

    def test_create_profile_returns_201(self, client: TestClient) -> None:
        fake = _fake_profile(
            name="new-env", provider_type="e2b", image_ref="ubuntu:22.04", capabilities=["docker", "gpu"]
        )
        with (
            patch(f"{_ROUTES}.create_environment_profile") as mock_create,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_create.return_value = fake
            resp = client.post(self.URL, json=self.PAYLOAD)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "new-env"
        assert data["provider_type"] == "e2b"
        assert data["image_ref"] == "ubuntu:22.04"
        assert data["capabilities"] == ["docker", "gpu"]

    def test_create_profile_missing_provider_type_returns_422(self, client: TestClient) -> None:
        """provider_type is explicitly required ? no server-side local_docker default (FAR-587)."""
        with patch(f"{_ROUTES}.set_rls_org"):
            resp = client.post(self.URL, json={"name": "incomplete"})
        assert resp.status_code == 422
        assert "provider_type" in resp.text

    @pytest.mark.parametrize("provider_type", ["dangertier", "heroku", "", "LOCAL_DOCKER"])
    def test_create_profile_invalid_provider_type_returns_422(self, provider_type: str, client: TestClient) -> None:
        """provider_type is vocabulary-validated at the API boundary (FAR-587).

        An invalid value must 422 at the boundary — it must never reach the DB
        CHECK and surface there as a misleading 409.
        """
        with patch(f"{_ROUTES}.set_rls_org"):
            resp = client.post(self.URL, json={"name": "bad-type", "provider_type": provider_type})
        assert resp.status_code == 422
        assert "provider_type" in resp.text

    @pytest.mark.parametrize("provider_type", sorted(PROVIDER_TYPES))
    def test_create_profile_validates_only_the_vocabulary(self, provider_type: str, client: TestClient) -> None:
        """Every CHECK-vocabulary value passes the boundary validation.

        The boundary pattern (routes) must accept exactly the model CHECK
        vocabulary — otherwise a valid DB type would be un-reachable via the
        API. Parametrized from the PROVIDER_TYPES constant (FAR-595) so a
        new vocabulary member is exercised here automatically.
        """
        fake = _fake_profile(provider_type=provider_type)
        with (
            patch(f"{_ROUTES}.create_environment_profile") as mock_create,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_create.return_value = fake
            resp = client.post(self.URL, json={"name": "typed", "provider_type": provider_type})
        assert resp.status_code == 201

    def test_update_profile_invalid_provider_type_returns_422(self, client: TestClient) -> None:
        """The update boundary validates provider_type against the same vocabulary."""
        with (
            patch(f"{_ROUTES}.update_environment_profile"),
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            resp = client.put(f"{self.URL}/{_PROFILE_ID}", json={"provider_type": "dangertier"})
        assert resp.status_code == 422
        assert "provider_type" in resp.text

    def test_create_profile_conflict(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.create_environment_profile") as mock_create,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_create.side_effect = IntegrityError("mock", "mock", "mock")
            resp = client.post(self.URL, json={"name": "dup", "provider_type": "e2b"})
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    @pytest.mark.parametrize("persistence", ["retained", "cache"])
    def test_create_runner_docker_non_ephemeral_returns_422(self, persistence: str, client: TestClient) -> None:
        """The Bundled Runner LOCKS persistence to ephemeral (FAR-590 D4): the
        crud validator's ValueError maps to a typed 422, never a 500."""
        with (
            patch(f"{_ROUTES}.create_environment_profile", new_callable=AsyncMock, side_effect=ValueError("locked")),
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            resp = client.post(
                self.URL, json={"name": "runner", "provider_type": "runner_docker", "persistence_policy": persistence}
            )
        assert resp.status_code == 422

    def test_create_profile_invalid_workspace_network_returns_422(self, client: TestClient) -> None:
        """A dangerous workspace_network rejected at the CRUD boundary maps to a
        typed 422 (FAR-1020) — the route must not surface it as a 500."""
        with (
            patch(
                f"{_ROUTES}.create_environment_profile",
                new_callable=AsyncMock,
                side_effect=WorkspaceNetworkValidationError("host"),
            ),
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            resp = client.post(
                self.URL,
                json={"name": "runner", "provider_type": "runner_docker", "config_json": {"workspace_network": "host"}},
            )
        assert resp.status_code == 422
        assert "workspace_network" in resp.json()["detail"]

    @pytest.mark.parametrize("persistence", ["retained", "cache"])
    def test_update_runner_docker_non_ephemeral_returns_422(self, persistence: str, client: TestClient) -> None:
        """The update path re-validates the merged row (validator parity)."""
        with (
            patch(f"{_ROUTES}.update_environment_profile", new_callable=AsyncMock, side_effect=ValueError("locked")),
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            resp = client.put(f"{self.URL}/{_PROFILE_ID}", json={"persistence_policy": persistence})
        assert resp.status_code == 422

    def test_update_profile_invalid_workspace_network_returns_422(self, client: TestClient) -> None:
        """The update route maps a CRUD WorkspaceNetworkValidationError to 422 (FAR-1020)."""
        with (
            patch(
                f"{_ROUTES}.update_environment_profile",
                new_callable=AsyncMock,
                side_effect=WorkspaceNetworkValidationError("host"),
            ),
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            resp = client.put(f"{self.URL}/{_PROFILE_ID}", json={"config_json": {"workspace_network": "host"}})
        assert resp.status_code == 422
        assert "workspace_network" in resp.json()["detail"]


class TestGetProfile:
    URL = "/api/v1/environment-profiles"

    def test_get_profile_returns_200(self, client: TestClient) -> None:
        fake = _fake_profile()
        with (
            patch(f"{_ROUTES}.get_environment_profile") as mock_get,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_get.return_value = fake
            resp = client.get(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-profile"
        assert data["image_ref"] == "python:3.12-slim"

    def test_get_profile_not_found(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.get_environment_profile") as mock_get,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_get.return_value = None
            resp = client.get(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"


class TestUpdateProfile:
    URL = "/api/v1/environment-profiles"

    def test_update_profile_returns_200(self, client: TestClient) -> None:
        fake = _fake_profile(name="updated-name")
        with (
            patch(f"{_ROUTES}.update_environment_profile") as mock_update,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_update.return_value = fake
            resp = client.put(f"{self.URL}/{_PROFILE_ID}", json={"name": "updated-name"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "updated-name"

    def test_update_profile_not_found(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.update_environment_profile") as mock_update,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_update.return_value = None
            resp = client.put(f"{self.URL}/{_PROFILE_ID}", json={"name": "nope"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"

    def test_update_profile_conflict(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.get_environment_profile"),
            patch(f"{_ROUTES}.update_environment_profile") as mock_update,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_update.side_effect = IntegrityError("mock", "mock", "mock")
            resp = client.put(f"{self.URL}/{_PROFILE_ID}", json={"name": "dup"})
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]


class TestScopeChangeBindingGuard:
    """FAR-1558 F2: a profile scope change must never strand a binding.

    The pipeline -> profile team rule has three writers (bind time, pipeline
    scope change, profile scope change); this class pins the third — the one
    that could otherwise undo a valid bind AFTER the fact.
    """

    URL = "/api/v1/environment-profiles"
    _TEAM_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
    _TEAM_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")

    @staticmethod
    def _pipeline_row(owner_team_id: uuid.UUID | None) -> MagicMock:
        row = MagicMock()
        row.id = uuid.uuid4()
        row.owner_team_id = owner_team_id
        return row

    @staticmethod
    def _install_session(
        *,
        profile: MagicMock | None,
        bound: list[MagicMock],
        queries: list[str],
    ) -> None:
        """Serve the guard's two SELECTs and record every statement run.

        ``queries`` is the caller's recorder so a test can assert a lookup
        never happened (the "no scope change, no query" contract).
        """
        session = _make_mock_session()

        def _execute(stmt: Any, *args: Any, **kwargs: Any) -> MagicMock:
            sql = str(stmt)
            queries.append(sql)
            result = MagicMock()
            # Case-insensitive: SQLAlchemy renders ``FROM pipelines`` but the
            # needle must never be matched against an upper-cased haystack.
            if "from pipelines" in sql.lower():
                result.all.return_value = bound
                return result
            if "environment_profiles" in sql:
                result.scalar_one_or_none.return_value = profile
                return result
            # authz_enforce and anything else the strict default used to serve.
            result.scalar_one_or_none.return_value = True
            return result

        session.execute = AsyncMock(side_effect=_execute)

        async def override_session() -> AsyncMock:
            yield session

        app.dependency_overrides[get_db_session] = override_session

    def _put(
        self,
        client: TestClient,
        body: dict[str, Any],
        *,
        profile: MagicMock | None,
        bound: list[MagicMock],
        queries: list[str],
    ) -> tuple[Any, MagicMock]:
        self._install_session(profile=profile, bound=bound, queries=queries)
        with (
            patch(f"{_ROUTES}.update_environment_profile") as mock_update,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_update.return_value = profile
            resp = client.put(f"{self.URL}/{_PROFILE_ID}", json=body)
        return resp, mock_update

    def test_mismatched_binding_refuses_the_flip_before_any_write(self, client: TestClient) -> None:
        """Org-visible profile with a team-B binding, flipped team-A-private."""
        queries: list[str] = []
        resp, mock_update = self._put(
            client,
            {"visibility": "team", "owner_team_id": str(self._TEAM_A)},
            profile=_fake_profile(visibility="org", owner_team_id=None),
            bound=[self._pipeline_row(self._TEAM_B)],
            queries=queries,
        )
        assert resp.status_code == 422, resp.text
        assert "environment_profile_binding_team_mismatch" in resp.json()["detail"]
        # Fail closed BEFORE the mutation: the CRUD write never runs.
        mock_update.assert_not_called()

    def test_matching_binding_allows_the_flip(self, client: TestClient) -> None:
        """The mirror direction: same flip, but every binding already matches."""
        queries: list[str] = []
        resp, mock_update = self._put(
            client,
            {"visibility": "team", "owner_team_id": str(self._TEAM_A)},
            profile=_fake_profile(visibility="org", owner_team_id=None),
            bound=[self._pipeline_row(self._TEAM_A), self._pipeline_row(self._TEAM_A)],
            queries=queries,
        )
        assert resp.status_code == 200, resp.text
        mock_update.assert_called_once()
        # Discriminating: the check RAN and passed — a skipped guard would also
        # return 200, so the pipeline lookup must be observed.
        assert any("from pipelines" in q.lower() for q in queries), queries

    def test_team_flip_with_no_bindings_is_allowed(self, client: TestClient) -> None:
        """Nothing is bound, so nothing can be stranded."""
        queries: list[str] = []
        resp, mock_update = self._put(
            client,
            {"visibility": "team", "owner_team_id": str(self._TEAM_A)},
            profile=_fake_profile(visibility="org", owner_team_id=None),
            bound=[],
            queries=queries,
        )
        assert resp.status_code == 200, resp.text
        mock_update.assert_called_once()
        assert any("from pipelines" in q.lower() for q in queries), queries

    def test_team_flip_without_an_owner_team_refuses_when_bound(self, client: TestClient) -> None:
        """Team-private with NO owner team is owned by nobody — fail closed."""
        queries: list[str] = []
        resp, mock_update = self._put(
            client,
            {"visibility": "team"},
            profile=_fake_profile(visibility="org", owner_team_id=None),
            bound=[self._pipeline_row(self._TEAM_A)],
            queries=queries,
        )
        assert resp.status_code == 422, resp.text
        assert "environment_profile_binding_team_mismatch" in resp.json()["detail"]
        mock_update.assert_not_called()

    def test_moving_the_owner_team_off_a_matching_binding_is_refused(self, client: TestClient) -> None:
        """A team-private profile whose owner team moves away from its bindings."""
        queries: list[str] = []
        resp, mock_update = self._put(
            client,
            {"owner_team_id": str(self._TEAM_B)},
            profile=_fake_profile(visibility="team", owner_team_id=self._TEAM_A),
            bound=[self._pipeline_row(self._TEAM_A)],
            queries=queries,
        )
        assert resp.status_code == 422, resp.text
        assert "environment_profile_binding_team_mismatch" in resp.json()["detail"]
        mock_update.assert_not_called()

    def test_assigning_an_owner_team_while_org_visible_is_never_checked(self, client: TestClient) -> None:
        """Org-visible profiles are compatible with every pipeline, so the
        binding query must not even run (and cannot block the change)."""
        queries: list[str] = []
        resp, mock_update = self._put(
            client,
            {"owner_team_id": str(self._TEAM_A)},
            profile=_fake_profile(visibility="org", owner_team_id=None),
            bound=[self._pipeline_row(self._TEAM_B)],
            queries=queries,
        )
        assert resp.status_code == 200, resp.text
        mock_update.assert_called_once()
        assert not any("from pipelines" in q.lower() for q in queries), queries

    def test_a_non_scope_change_never_consults_bindings(self, client: TestClient) -> None:
        """A plain field edit (name) must not query pipelines at all."""
        queries: list[str] = []
        resp, mock_update = self._put(
            client,
            {"name": "renamed"},
            profile=_fake_profile(visibility="team", owner_team_id=self._TEAM_A),
            bound=[self._pipeline_row(self._TEAM_B)],
            queries=queries,
        )
        assert resp.status_code == 200, resp.text
        mock_update.assert_called_once()
        assert not any("environment_profiles" in q for q in queries), queries
        assert not any("from pipelines" in q.lower() for q in queries), queries

    def test_null_visibility_is_not_treated_as_team_private(self, client: TestClient) -> None:
        """``visibility: null`` cannot satisfy the team branch of the rule —
        it falls through to the org arm and the write path rejects it (NOT
        NULL) instead of being mis-validated here."""
        queries: list[str] = []
        resp, mock_update = self._put(
            client,
            {"visibility": None},
            profile=_fake_profile(visibility="org", owner_team_id=None),
            bound=[self._pipeline_row(self._TEAM_B)],
            queries=queries,
        )
        # The guard does not refuse it (org arm), and the CRUD stub accepts it.
        assert resp.status_code == 200, resp.text
        mock_update.assert_called_once()
        assert not any("from pipelines" in q.lower() for q in queries), queries

    def test_scope_change_on_a_missing_profile_defers_to_the_update_404(self, client: TestClient) -> None:
        """A scope change for a profile that does not exist is not the guard's
        job: it returns early and the update path raises the 404.

        The guard only refuses a scope change that would STRAND an existing
        binding; when the profile row is absent there is nothing to strand, so
        it must not raise the binding-mismatch 422 (nor run the team-blind
        pipeline scan) — the following update produces the canonical 404.
        """
        queries: list[str] = []
        resp, mock_update = self._put(
            client,
            {"visibility": "team", "owner_team_id": str(self._TEAM_A)},
            profile=None,
            bound=[self._pipeline_row(self._TEAM_B)],
            queries=queries,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Environment profile not found"
        mock_update.assert_called_once()
        # Early return BEFORE the binding scan: the pipelines query never ran.
        assert not any("from pipelines" in q.lower() for q in queries), queries


class TestDeleteProfile:
    URL = "/api/v1/environment-profiles"

    def test_delete_profile_returns_204(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.soft_delete_environment_profile") as mock_delete,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_delete.return_value = _fake_profile()
            resp = client.delete(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 204

    def test_delete_profile_not_found(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.soft_delete_environment_profile") as mock_delete,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_delete.return_value = None
            resp = client.delete(f"{self.URL}/{_PROFILE_ID}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"


class TestRestoreProfile:
    URL = "/api/v1/environment-profiles"

    def test_restore_profile_returns_200(self, client: TestClient) -> None:
        fake = _fake_profile()
        with (
            patch(f"{_ROUTES}.restore_environment_profile") as mock_restore,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_restore.return_value = fake
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/restore")
        assert resp.status_code == 200
        assert resp.json()["name"] == "test-profile"

    def test_restore_profile_not_found(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.restore_environment_profile") as mock_restore,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_restore.return_value = None
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/restore")
        assert resp.status_code == 404


class TestProfileTestEndpoint:
    URL = "/api/v1/environment-profiles"

    def test_profile_test_profile_not_found(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.get_environment_profile") as mock_get,
            patch(f"{_ROUTES}.set_rls_org"),
        ):
            mock_get.return_value = None
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Environment profile not found"

    @staticmethod
    def _stub_hub(provider_type: str) -> RuntimeProviderHub:
        hub = RuntimeProviderHub()
        provider = MagicMock()
        provider.create_workspace = AsyncMock(return_value="ws-ref-001")
        provider.exec_command = AsyncMock(return_value=MagicMock(exit_code=0, stdout="ok", stderr="", duration_ms=1))
        provider.destroy_workspace = AsyncMock()
        provider.close = AsyncMock()
        hub.register(provider_type, provider)
        return hub

    def test_profile_test_streams_sse(self, client: TestClient) -> None:
        fake = _fake_profile()
        hub = self._stub_hub(str(fake.provider_type))
        with (
            patch(f"{_ROUTES}.get_environment_profile") as mock_get,
            patch(f"{_ROUTES}.set_rls_org"),
            patch(f"{_ROUTES}._get_hub", return_value=hub),
        ):
            mock_get.return_value = fake
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert "command_complete" in resp.text
        assert "destroyed" in resp.text

    def test_profile_test_unconfigured_provider_streams_failed_event(self, client: TestClient) -> None:
        """No silent local fallback: an unresolvable profile surfaces the typed error (FAR-587)."""
        fake = _fake_profile()
        hub = RuntimeProviderHub()  # nothing registered
        with (
            patch(f"{_ROUTES}.get_environment_profile") as mock_get,
            patch(f"{_ROUTES}.set_rls_org"),
            patch(f"{_ROUTES}._get_hub", return_value=hub),
        ):
            mock_get.return_value = fake
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert "failed" in resp.text
        expected = str(ProviderNotConfiguredError("local_docker", "MODULO_DOCKER_HOST"))
        assert expected in resp.text
        assert "provisioning" in resp.text

    def test_profile_test_selected_on_docker_refuses_not_fail_open(self, client: TestClient) -> None:
        """FAR-1085 regression: profile 'selected' on a Docker-tier profile must
        refuse (SandboxTierRefusedError) rather than silently granting outbound.

        Before the fix, _build_workspace_spec mapped the refusal to
        egress_policy='outbound' — a fail-open that gave full internet to a
        profile that asked for an allowlist.
        """
        fake = _fake_profile(network_policy="selected")
        hub = self._stub_hub("local_docker")
        with (
            patch(f"{_ROUTES}.get_environment_profile") as mock_get,
            patch(f"{_ROUTES}.set_rls_org"),
            patch(f"{_ROUTES}._get_hub", return_value=hub),
        ):
            mock_get.return_value = fake
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 200
        body = resp.text
        assert "failed" in body
        assert "refused" in body.lower()
        # Must NOT have proceeded to provisioning/provisioned — the refusal
        # fires before the provider is called.
        assert "provisioned" not in body
        assert "command_complete" not in body

    def test_profile_test_local_docker_resolves_as_docker_tier(self, client: TestClient) -> None:
        """FAR-1085 regression: local_docker must map to tier 'docker', not 'local'.

        Before the fix, local_docker mapped to 'local' which accepted the
        default posture but refused everything else. As a Docker alias it
        must map to 'docker' — which accepts default+deny_all but refuses
        'selected' (no host-allowlist mechanism).
        """
        # local_docker + outbound (maps to default) should succeed on docker tier
        fake = _fake_profile(network_policy="outbound")
        hub = self._stub_hub("local_docker")
        with (
            patch(f"{_ROUTES}.get_environment_profile") as mock_get,
            patch(f"{_ROUTES}.set_rls_org"),
            patch(f"{_ROUTES}._get_hub", return_value=hub),
        ):
            mock_get.return_value = fake
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 200
        assert "command_complete" in resp.text
        assert "destroyed" in resp.text

    def test_profile_test_unknown_provider_type_refuses_not_fail_open(self, client: TestClient) -> None:
        """FAR-1085 regression: an unrecognised provider_type must refuse, not
        silently default to the enforceable 'e2b' tier.

        Before the fix, _tier_map.get(provider_type, "e2b") mapped any unknown
        provider to e2b — the fail-open the reviewer flagged. An unknown tier
        must fail closed with a refusal. (FAR-1051 note: this uses a type that
        is NOT in the PROVIDER_TYPES vocabulary — ``kubernetes`` is a real
        tier now and is covered by its own test below.)
        """
        fake = _fake_profile(provider_type="quantum_entangler", network_policy="outbound")
        hub = self._stub_hub("quantum_entangler")
        with (
            patch(f"{_ROUTES}.get_environment_profile") as mock_get,
            patch(f"{_ROUTES}.set_rls_org"),
            patch(f"{_ROUTES}._get_hub", return_value=hub),
        ):
            mock_get.return_value = fake
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 200
        assert "failed" in resp.text
        assert "refused" in resp.text.lower()
        assert "unknown tier" in resp.text.lower()
        # Must NOT have provisioned — the refusal fires before the provider call.
        assert "provisioned" not in resp.text

    def test_profile_test_kubernetes_resolves_as_the_kubernetes_tier(self, client: TestClient) -> None:
        """FAR-1051: a kubernetes profile is no longer an 'unknown tier'.

        The tier is sourced from the provider registry, so the posture this
        tier CAN enforce (the unrestricted default behind ``outbound``)
        provisions normally instead of refusing a supported configuration.
        """
        fake = _fake_profile(provider_type="kubernetes", network_policy="outbound")
        hub = self._stub_hub("kubernetes")
        with (
            patch(f"{_ROUTES}.get_environment_profile") as mock_get,
            patch(f"{_ROUTES}.set_rls_org"),
            patch(f"{_ROUTES}._get_hub", return_value=hub),
        ):
            mock_get.return_value = fake
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 200
        assert "command_complete" in resp.text
        assert "destroyed" in resp.text
        assert "unknown tier" not in resp.text.lower()

    def test_profile_test_kubernetes_none_refuses_naming_networkpolicy(self, client: TestClient) -> None:
        """FAR-1051: the unenforceable posture on Kubernetes refuses (fail
        closed) and the refusal names where the control actually lives —
        never a silent downgrade to outbound."""
        fake = _fake_profile(provider_type="kubernetes", network_policy="none")
        hub = self._stub_hub("kubernetes")
        with (
            patch(f"{_ROUTES}.get_environment_profile") as mock_get,
            patch(f"{_ROUTES}.set_rls_org"),
            patch(f"{_ROUTES}._get_hub", return_value=hub),
        ):
            mock_get.return_value = fake
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 200
        body = resp.text.lower()
        assert "failed" in body
        assert "refused" in body
        assert "networkpolicy" in body
        assert "provisioned" not in body

    def test_profile_test_unexpected_provider_error_streams_failed(self, client: TestClient) -> None:
        """An unexpected provider error mid-stream surfaces the generic failed
        event (never a 500) and is not mistaken for a tier refusal."""
        fake = _fake_profile()
        hub = RuntimeProviderHub()
        provider = MagicMock()
        provider.create_workspace = AsyncMock(side_effect=RuntimeError("provider exploded"))
        provider.close = AsyncMock()
        hub.register(str(fake.provider_type), provider)
        with (
            patch(f"{_ROUTES}.get_environment_profile") as mock_get,
            patch(f"{_ROUTES}.set_rls_org"),
            patch(f"{_ROUTES}._get_hub", return_value=hub),
        ):
            mock_get.return_value = fake
            resp = client.post(f"{self.URL}/{_PROFILE_ID}/test")
        assert resp.status_code == 200
        assert "failed" in resp.text
        assert "check server logs" in resp.text


def test_get_hub_builds_fresh_hub() -> None:
    """_get_hub() returns a live RuntimeProviderHub built from process settings."""
    from modulo.api.routes.environment_profiles import _get_hub

    hub = _get_hub()

    assert isinstance(hub, RuntimeProviderHub)


def test_egress_tier_for_provider_type_sources_aliases_and_fails_closed() -> None:
    """FAR-1085: the egress tier is sourced from the provider registry.

    ``local_docker`` is a Docker alias (``DockerRuntimeProvider.provider_aliases``)
    so it resolves to the 'docker' tier; ``k8s`` is the Kubernetes alias
    (FAR-1051) so it resolves to 'kubernetes'; an empty or unknown provider
    type resolves to ``None`` so the caller fails closed.
    """
    from modulo.api.routes.environment_profiles import _egress_tier_for_provider_type

    assert _egress_tier_for_provider_type("e2b") == "e2b"
    assert _egress_tier_for_provider_type("runner_docker") == "docker"
    assert _egress_tier_for_provider_type("local_docker") == "docker"
    assert _egress_tier_for_provider_type("local") == "local"
    assert _egress_tier_for_provider_type("") is None
    assert _egress_tier_for_provider_type("quantum_entangler") is None
    # FAR-1051: the Kubernetes provider maps to its own tier, alias included.
    assert _egress_tier_for_provider_type("kubernetes") == "kubernetes"
    assert _egress_tier_for_provider_type("k8s") == "kubernetes"


def test_egress_tier_for_provider_type_skips_unimportable_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """An optional provider extra that is not installed is skipped, not fatal.

    The Docker provider imports ``aiodocker`` (an optional extra); when that
    module is absent the alias scan must continue rather than raising, so an
    E2B profile still resolves while the unimportable provider is ignored.
    """
    import importlib

    from modulo.api.routes.environment_profiles import _egress_tier_for_provider_type

    real_import_module = importlib.import_module

    def _import_module(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "modulo.core.runtime_provider.e2b":
            raise ImportError("e2b extra not installed")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _import_module)

    assert _egress_tier_for_provider_type("e2b") is None
    assert _egress_tier_for_provider_type("local_docker") == "docker"


# ---------------------------------------------------------------------------
# FAR-1050: canonical -> WorkspaceSpec egress mapping must be LOSSLESS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("canonical", "expected_spec_value", "expected_allows_internet"),
    [
        pytest.param(None, "outbound", True, id="default-allows"),
        pytest.param("deny_all", "none", False, id="deny-all-denies"),
        pytest.param("selected", "selected", False, id="selected-denies"),
        pytest.param("bogus_policy", "none", False, id="unknown-fails-closed"),
    ],
)
def test_spec_egress_for_canonical_is_lossless(
    canonical: str | None,
    expected_spec_value: str,
    expected_allows_internet: bool,
) -> None:
    """FAR-1050 regression: canonical 'selected' must NOT collapse into the
    permissive 'outbound' (the ADR 040 fail-open defect class)."""
    from modulo.core.pipeline_engine.egress import spec_egress_for_canonical
    from modulo.core.runtime_provider.e2b import _egress_allows_internet

    mapped = spec_egress_for_canonical(canonical)

    assert mapped == expected_spec_value
    assert _egress_allows_internet(mapped) is expected_allows_internet


def test_build_workspace_spec_profile_none_denies_internet() -> None:
    """profile network_policy='none' -> canonical deny_all -> spec 'none' -> E2B denies internet."""
    from modulo.api.routes.environment_profiles import _build_workspace_spec
    from modulo.core.runtime_provider.e2b import _egress_allows_internet

    spec = _build_workspace_spec(_fake_profile(network_policy="none", provider_type="e2b"))

    assert spec.egress_policy == "none"
    assert _egress_allows_internet(spec.egress_policy) is False


def test_build_workspace_spec_profile_outbound_allows_internet() -> None:
    """profile network_policy='outbound' -> canonical None -> spec 'outbound' -> E2B allows internet."""
    from modulo.api.routes.environment_profiles import _build_workspace_spec
    from modulo.core.runtime_provider.e2b import _egress_allows_internet

    spec = _build_workspace_spec(_fake_profile(network_policy="outbound", provider_type="e2b"))

    assert spec.egress_policy == "outbound"
    assert _egress_allows_internet(spec.egress_policy) is True


def test_build_workspace_spec_timeout_default_matches_template_constant() -> None:
    """FAR-1494: the sandbox-test route's workspace-spec timeout DEFAULT is
    sourced from the shared template constant. This is the coupling guard for
    the site this PR changed (``_build_workspace_spec``, used by the
    ``/environment-profiles/{id}/test`` path): a profile whose ``config_json``
    omits ``timeout_seconds`` must fall back to
    ``TEMPLATE_CONFIG_JSON["timeout_seconds"]``. This catches divergence
    between this site and the constant; a pure constant-value drift is not
    detected here (the site and the assertion move together) and is covered by
    the constant-pinning tests in
    ``tests/unit/core/bundled_runner/test_profile.py``.
    """
    from modulo.api.routes.environment_profiles import _build_workspace_spec
    from modulo.core.bundled_runner.profile import TEMPLATE_CONFIG_JSON

    spec = _build_workspace_spec(_fake_profile(provider_type="e2b", config_json={"memory_mb": 1024}))

    assert spec.timeout_seconds == TEMPLATE_CONFIG_JSON["timeout_seconds"]


def test_build_workspace_spec_selected_produces_deny_internet_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAR-1050 end-to-end: a profile with network_policy='selected' must
    produce a WorkspaceSpec whose egress_policy makes
    _egress_allows_internet() return False � never 'outbound'.

    resolve_egress is stubbed to a NON-refusing 'selected' resolution
    (allowlist present) because the sandbox-test route carries no
    profile-level allowlist today, so the real upstream refuses 'selected'
    before the mapper runs. The mapper is what is under test here; the
    refusal behaviour itself is asserted separately by
    test_build_workspace_spec_selected_refuses_without_allowlist.
    """
    from modulo.api.routes.environment_profiles import _build_workspace_spec
    from modulo.core.pipeline_engine.egress import EgressResolution
    from modulo.core.runtime_provider.e2b import _egress_allows_internet

    selected_with_allowlist = EgressResolution(
        policy="selected",
        allowlist=[{"host": "api.github.com", "port": 443}],
        refusal=None,
    )
    monkeypatch.setattr("modulo.core.pipeline_engine.egress.resolve_egress", lambda **_kwargs: selected_with_allowlist)

    spec = _build_workspace_spec(_fake_profile(network_policy="selected", provider_type="e2b"))

    assert spec.egress_policy == "selected"
    assert _egress_allows_internet(spec.egress_policy) is False
    # Guard against the pre-fix collapse: the spec value must never be the
    # permissive dialect value for a restrictive canonical policy.
    assert spec.egress_policy != "outbound"
    # FAR-1050 review follow-up: the selected-mode allowlist must ride the
    # WorkspaceSpec metadata carrier (the key the E2B provider reads), not be
    # dropped — otherwise 'selected' would behave as deny_all.
    assert json.loads(spec.workspace_metadata["egress_allowlist"]) == [{"host": "api.github.com", "port": 443}]


def test_build_workspace_spec_selected_refuses_without_allowlist() -> None:
    """Current real-path behaviour: with no profile-level allowlist the
    upstream resolver refuses 'selected' BEFORE the mapper runs � a loud
    refusal, never a silently permissive spec."""
    from modulo.api.routes.environment_profiles import _build_workspace_spec
    from modulo.core.pipeline_engine.sandbox_errors import SandboxTierRefusedError

    with pytest.raises(SandboxTierRefusedError, match="non-empty allowlist"):
        _build_workspace_spec(_fake_profile(network_policy="selected", provider_type="e2b"))
