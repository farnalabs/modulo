"""Unit tests for /api/v1/connectors endpoints.

Credentials (raw credential strings) must NEVER appear in responses.
Only `has_credentials: true/false` is exposed.
"""

import ast
import json
import logging
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import modulo.api.routes.connectors as connectors_module
from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_FERNET_KEY = Fernet.generate_key().decode()
_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_CONNECTOR_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_CRUD_PATCH_PREFIX = "modulo.api.routes.connectors."


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


@pytest.fixture(autouse=True)
def _stub_get_connector_instance() -> Generator[None, None, None]:
    """The IDOR ownership check reads the connector via ``get_connector_instance``
    before the write CRUD, but the write-path cases only mock
    ``update_connector_instance`` / ``delete_connector_instance``. Supply a
    same-org connector so the ownership check passes for the legitimate
    (same-org) principal these tests use."""
    with patch(
        "modulo.api.routes.connectors.get_connector_instance",
        return_value=_make_connector(),
    ):
        yield


def _make_connector(credentials_ciphertext: bytes = b"encrypted", tier: str = "native") -> MagicMock:
    ci = MagicMock()
    ci.id = _CONNECTOR_ID
    ci.organisation_id = _ORG_ID
    ci.name = "Test Connector"
    ci.connector_type_id = "filesystem"
    ci.credentials_ciphertext = credentials_ciphertext
    ci.config_json = {}
    ci.allowed_operations = []
    ci.status = "active"
    ci.visibility = "org"
    ci.owner_team_id = None
    ci.tier = tier
    ci.created_at = _NOW
    ci.updated_at = _NOW
    # Nullable degraded markers: a bare MagicMock would auto-create these as
    # non-serialisable mocks, so mirror a healthy ORM row explicitly.
    ci.degraded_at = None
    ci.last_skip_error = None
    return ci


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
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


@pytest.fixture
def viewer_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="vieweruser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="viewer"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


_CREATE_BODY = {
    "name": "Test Connector",
    "connector_type_id": "filesystem",
    "credentials": '{"token": "secret123"}',
}


def _crud_cases() -> list[dict[str, object]]:
    page_result = MagicMock(items=[_make_connector()], total=1, page=1, page_size=20, next_cursor=None)
    connector = _make_connector()
    updated = _make_connector()
    updated.name = "Updated"
    return [
        {
            "id": "list",
            "method": "GET",
            "url": "/api/v1/connectors",
            "body": None,
            "patches": [("list_connector_instances", page_result)],
            "expected_status": 200,
            "check": lambda resp: resp.json()["total"] == 1,
        },
        {
            "id": "get",
            "method": "GET",
            "url": f"/api/v1/connectors/{_CONNECTOR_ID}",
            "body": None,
            "patches": [("get_connector_instance", connector)],
            "expected_status": 200,
            "check": lambda resp: "credentials_ciphertext" not in resp.json() and resp.json()["has_credentials"],
        },
        {
            "id": "get_not_found",
            "method": "GET",
            "url": f"/api/v1/connectors/{uuid.uuid4()}",
            "body": None,
            "patches": [("get_connector_instance", None)],
            "expected_status": 404,
        },
        {
            "id": "update",
            "method": "PATCH",
            "url": f"/api/v1/connectors/{_CONNECTOR_ID}",
            "body": {"name": "Updated"},
            "patches": [("get_connector_instance", connector), ("update_connector_instance", updated)],
            "expected_status": 200,
            "check": lambda resp: resp.json()["name"] == "Updated",
        },
        {
            "id": "update_not_found",
            "method": "PATCH",
            "url": f"/api/v1/connectors/{uuid.uuid4()}",
            "body": {"name": "x"},
            "patches": [("get_connector_instance", None), ("update_connector_instance", None)],
            "expected_status": 404,
        },
        {
            "id": "delete",
            "method": "DELETE",
            "url": f"/api/v1/connectors/{_CONNECTOR_ID}",
            "body": None,
            "patches": [("delete_connector_instance", True)],
            "expected_status": 204,
        },
        {
            "id": "delete_not_found",
            "method": "DELETE",
            "url": f"/api/v1/connectors/{uuid.uuid4()}",
            "body": None,
            "patches": [("delete_connector_instance", False)],
            "expected_status": 404,
        },
    ]


@pytest.mark.parametrize("case", _crud_cases(), ids=lambda c: c["id"])
def test_crud(client: TestClient, case: dict[str, object]) -> None:
    method = case["method"]
    url = case["url"]
    body = case.get("body")
    expected_status = case["expected_status"]
    check = case.get("check")

    patchers = []
    for func_name, ret in case["patches"]:
        patchers.append(patch(f"{_CRUD_PATCH_PREFIX}{func_name}", return_value=ret))
    patchers.append(patch(f"{_CRUD_PATCH_PREFIX}set_rls_org"))
    patchers.append(patch(f"{_CRUD_PATCH_PREFIX}set_rls_user_context"))

    for p in patchers:
        p.start()

    try:
        if method == "GET":
            resp = client.get(url)
        elif method == "POST":
            resp = client.post(url, json=body or {})
        elif method == "PATCH":
            resp = client.patch(url, json=body or {})
        elif method == "DELETE":
            resp = client.delete(url)
        elif method == "PUT":
            resp = client.put(url, json=body or {})
        else:
            raise ValueError(f"Unsupported method: {method}")

        assert resp.status_code == expected_status, f"Expected {expected_status}, got {resp.status_code}: {resp.text}"
        if check:
            assert check(resp)
    finally:
        for p in patchers:
            p.stop()


def test_create_connector_does_not_expose_credentials(client: TestClient) -> None:
    connector = _make_connector(credentials_ciphertext=b"encrypted_bytes")
    with (
        patch("modulo.api.routes.connectors.create_connector_instance", return_value=connector),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=_CREATE_BODY)

    assert resp.status_code == 201
    body = resp.json()
    assert "credentials_ciphertext" not in body
    assert "credentials" not in body
    assert body["has_credentials"] is True


def _make_github_body(token: str) -> dict[str, str]:
    return {
        "name": "GitHub Connector",
        "connector_type_id": "github",
        "credentials": token,
        "config_json": {},
    }


def test_create_github_fine_grained_token_accepted(client: TestClient) -> None:
    """A fine-grained PAT (github_pat_ prefix) is not rejected by the classic scope check."""
    token = "github_pat_11ABC"
    connector = _make_connector(credentials_ciphertext=b"encrypted_bytes")
    with (
        patch("modulo.api.routes.connectors.GitHubConnector.verify_scopes", new=AsyncMock(return_value=set())),
        patch("modulo.api.routes.connectors.create_connector_instance", return_value=connector),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=_make_github_body(token))

    assert resp.status_code == 201


def test_create_github_fine_grained_missing_permission_reports_fine_grained_detail(client: TestClient) -> None:
    """Fine-grained missing permissions surface the PRD §7.11 permission set, not classic scopes."""
    token = "github_pat_11ABC"
    with (
        patch(
            "modulo.api.routes.connectors.GitHubConnector.verify_scopes",
            new=AsyncMock(return_value={"contents:write", "pull_requests:write"}),
        ),
        patch("modulo.api.routes.connectors.create_connector_instance"),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=_make_github_body(token))

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "fine-grained permissions" in detail
    assert "pull_requests:write" in detail
    assert "repo" not in detail
    assert "contents:write" in detail


def test_create_github_classic_missing_scope_reports_classic_detail(client: TestClient) -> None:
    """Classic tokens keep the classic OAuth-scope rejection detail."""
    token = "ghp_classic123"
    with (
        patch(
            "modulo.api.routes.connectors.GitHubConnector.verify_scopes",
            new=AsyncMock(return_value={"repo"}),
        ),
        patch("modulo.api.routes.connectors.create_connector_instance"),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=_make_github_body(token))

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "OAuth scopes" in detail
    assert "repo" in detail


def test_create_connector_encrypts_credentials(client: TestClient) -> None:
    captured: list[bytes] = []

    async def fake_create(session: object, **kwargs: object) -> MagicMock:
        captured.append(kwargs["credentials_ciphertext"])  # type: ignore[arg-type]
        return _make_connector(credentials_ciphertext=kwargs["credentials_ciphertext"])  # type: ignore[arg-type]

    with (
        patch("modulo.api.routes.connectors.create_connector_instance", new=fake_create),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        client.post("/api/v1/connectors", json=_CREATE_BODY)

    assert captured, "create_connector_instance was not called"
    ciphertext = captured[0]
    decrypted = Fernet(_FERNET_KEY.encode()).decrypt(ciphertext).decode()
    assert decrypted == '{"token": "secret123"}'
    assert b"secret123" not in ciphertext


def test_connector_no_credentials_shows_false(client: TestClient) -> None:
    connector = _make_connector(credentials_ciphertext=b"")
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=connector),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/connectors/{_CONNECTOR_ID}")
    assert resp.json()["has_credentials"] is False


def test_list_connectors_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/connectors")
    assert resp.status_code in (401, 403)


def test_list_connectors_default_excludes_in_dev(client: TestClient) -> None:
    """The list endpoint defaults to the CRUD in_dev exclusion (excluded_tiers=None)."""
    page_result = MagicMock(items=[_make_connector()], total=1, page=1, page_size=20, next_cursor=None)
    with (
        patch("modulo.api.routes.connectors.list_connector_instances", return_value=page_result) as mock_list,
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/connectors")
    assert resp.status_code == 200
    assert mock_list.await_args is not None
    assert mock_list.await_args.kwargs["excluded_tiers"] is None


def test_list_connectors_include_in_dev_passes_empty_exclusions(client: TestClient) -> None:
    """?include_in_dev=true reveals In-Dev connectors in the actual response JSON."""
    in_dev = _make_connector(tier="in_dev")
    page_result = MagicMock(items=[in_dev], total=1, page=1, page_size=20, next_cursor=None)
    with (
        patch("modulo.api.routes.connectors.list_connector_instances", return_value=page_result) as mock_list,
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/connectors", params={"include_in_dev": "true"})
    assert resp.status_code == 200
    assert mock_list.await_args is not None
    assert not mock_list.await_args.kwargs["excluded_tiers"]
    tiers = [item["tier"] for item in resp.json()["items"]]
    assert "in_dev" in tiers, f"Expected an in_dev connector in the response, got tiers: {tiers}"


def test_list_connectors_include_in_dev_denied_for_viewer(viewer_client: TestClient) -> None:
    """Viewers can list connectors but must NOT be able to reveal In-Dev items."""
    resp = viewer_client.get("/api/v1/connectors", params={"include_in_dev": "true"})
    assert resp.status_code == 403
    assert "connector.list.in_dev" in resp.json()["detail"]


def test_list_connectors_include_in_dev_operator_reveals_in_dev(client: TestClient) -> None:
    """An operator+ principal (admin fixture) can list In-Dev connectors."""
    in_dev = _make_connector(tier="in_dev")
    page_result = MagicMock(items=[in_dev], total=1, page=1, page_size=20, next_cursor=None)
    with (
        patch("modulo.api.routes.connectors.list_connector_instances", return_value=page_result),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/connectors", params={"include_in_dev": "true"})
    assert resp.status_code == 200
    assert resp.json()["items"][0]["tier"] == "in_dev"


def test_list_connectors_include_in_dev_false_keeps_exclusion(client: TestClient) -> None:
    """?include_in_dev=false behaves exactly like omitting the parameter."""
    page_result = MagicMock(items=[_make_connector()], total=1, page=1, page_size=20, next_cursor=None)
    with (
        patch("modulo.api.routes.connectors.list_connector_instances", return_value=page_result) as mock_list,
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/connectors", params={"include_in_dev": "false"})
    assert resp.status_code == 200
    assert mock_list.await_args is not None
    assert mock_list.await_args.kwargs["excluded_tiers"] is None


def _foreign_org_connector() -> MagicMock:
    """A connector owned by a different organisation than the test principal."""
    foreign = MagicMock()
    foreign.id = _CONNECTOR_ID
    foreign.organisation_id = uuid.uuid4()
    return foreign


def test_update_connector_foreign_org_returns_404(client: TestClient) -> None:
    """IDOR regression: a foreign-org principal must not update a connector it
    does not own. The ownership check must raise 404 before any write."""
    with patch(
        "modulo.api.routes.connectors.get_connector_instance",
        return_value=_foreign_org_connector(),
    ):
        resp = client.patch(f"/api/v1/connectors/{_CONNECTOR_ID}", json={})
    assert resp.status_code == 404


def test_delete_connector_foreign_org_returns_404(client: TestClient) -> None:
    """IDOR regression: a foreign-org principal must not delete a connector it
    does not own."""
    with patch(
        "modulo.api.routes.connectors.get_connector_instance",
        return_value=_foreign_org_connector(),
    ):
        resp = client.delete(f"/api/v1/connectors/{_CONNECTOR_ID}")
    assert resp.status_code == 404


def test_connector_health_check_passes_db_session_and_reports_real_status(client: TestClient) -> None:
    """Regression (FAR-519): the health check must build the secrets backend with
    the DB session so the connector's stored credentials can actually be
    decrypted — and it must report the REAL health status instead of a blanket
    502.

    Without ``session=session`` the FernetSecretsBackend raises
    ``RuntimeError('no DB session')`` on ``get_secret``, so every health probe
    errored into the 502 fallback. This test asserts the session is passed to
    the backend factory AND that a healthy probe returns ``ok: true``.
    """
    captured: dict[str, object] = {}
    backend_holder: dict[str, object] = {}

    class _RecordingSecretsBackend:
        def __init__(self, **kwargs: object) -> None:
            captured["session"] = kwargs.get("session")
            self.get_secret_keys: list[str] = []

        async def get_secret(self, key: str) -> str:
            self.get_secret_keys.append(key)
            return '{"token": "secret123"}'

        async def set_secret(self, key: str, value: str) -> None:
            return None

        async def delete_secret(self, key: str) -> None:
            return None

    class _FakeConnector:
        async def health_check(self) -> SimpleNamespace:
            return SimpleNamespace(ok=True, detail="connected")

    class _FakeHub:
        def __init__(self, secrets_backend: object, **kwargs: object) -> None:
            backend_holder["backend"] = secrets_backend
            self._secrets_backend = secrets_backend
            self._connector = _FakeConnector()

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def initialise(self, instances: object, **kwargs: object) -> None:
            for inst in instances:  # type: ignore[union-attr]
                await self._secrets_backend.get_secret(str(cast(Any, inst).id))

        def get(self, connector_id: uuid.UUID) -> _FakeConnector:
            return self._connector

    def fake_create_secrets_backend(
        *,
        fernet_key: str | None = None,
        session: object = None,
        old_key: str | None = None,
        backend_name: str | None = None,
    ) -> _RecordingSecretsBackend:
        return _RecordingSecretsBackend(fernet_key=fernet_key, session=session)

    with (
        patch("modulo.api.routes.connectors.create_secrets_backend", fake_create_secrets_backend),
        patch("modulo.api.routes.connectors.ConnectorHub", _FakeHub),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/connectors/{_CONNECTOR_ID}/health")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["detail"] == "connected"
    assert captured["session"] is not None, "secrets backend must be built with the DB session"
    assert isinstance(backend_holder["backend"], _RecordingSecretsBackend)
    assert backend_holder["backend"].get_secret_keys == [str(_CONNECTOR_ID)]


def test_connector_health_check_unhealthy_reports_ok_false_in_band(client: TestClient) -> None:
    """A failing health check is reported in-band (``ok: false`` with detail),
    NOT as an HTTP 502 — the probe outcome is a response, not a transport error."""

    class _FakeConnector:
        async def health_check(self) -> SimpleNamespace:
            return SimpleNamespace(ok=False, detail="connection refused")

    class _FakeHub:
        def __init__(self, secrets_backend: object, **kwargs: object) -> None:
            self._connector = _FakeConnector()

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def initialise(self, instances: object, **kwargs: object) -> None:
            return None

        def get(self, connector_id: uuid.UUID) -> _FakeConnector:
            return self._connector

    with (
        patch("modulo.api.routes.connectors.create_secrets_backend", return_value=MagicMock()),
        patch("modulo.api.routes.connectors.ConnectorHub", _FakeHub),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/connectors/{_CONNECTOR_ID}/health")

    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
    assert resp.json()["detail"] == "connection refused"


class _ScopedSession:
    """Fake session that mimics asyncpg ``SET LOCAL`` (``set_config(... is_local=true)``)
    semantics: the organisation context is ONLY visible inside an
    ``async with session.begin():`` block and reverts at commit.

    This is the Postgres behaviour that made the pre-fix health check a 502 —
    the RLS org scope was set in block #1, committed, then the decrypt
    (``get_secret``) ran in block #2 with no org scope. ``FernetSecretsBackend``
    reads the org from ``current_setting`` / ``session.info``; when it is absent
    it raises, the connector is skipped and the probe 502s.
    """

    def __init__(self) -> None:
        self.info: dict[str, object] = {}
        self._in_tx = False

    def in_transaction(self) -> bool:
        return self._in_tx

    def get_bind(self) -> Any:
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def begin(self) -> "_ScopedTx":
        return _ScopedTx(self)

    async def execute(self, *_: Any, **__: Any) -> Any:
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        result.scalar.return_value = None
        result.scalar_one.return_value = None
        result.first.return_value = None
        result.all.return_value = []
        result.scalars.return_value.all.return_value = []
        return result


class _ScopedTx:
    def __init__(self, session: _ScopedSession) -> None:
        self._session = session

    async def __aenter__(self) -> _ScopedSession:
        self._session._in_tx = True
        return self._session

    async def __aexit__(self, *_: object) -> bool:
        self._session._in_tx = False
        # SET LOCAL reverts at transaction end.
        self._session.info.pop("org_id", None)
        self._session.info.pop("user_id", None)
        self._session.info.pop("org_role", None)
        return False


def test_connector_health_check_keeps_org_scope_active_at_decrypt_time(client: TestClient) -> None:
    """Regression (FAR-519): the health-check decrypt must run in the SAME
    transaction that set the RLS org scope.

    On real Postgres ``set_rls_org`` uses ``SET LOCAL`` (is_local=true), which
    reverts on commit. If the connector is built and its credentials decrypted
    AFTER the ``async with session.begin():`` block that set the org has
    committed, ``get_secret`` sees no org and raises RuntimeError — the
    connector is skipped and the probe 502s. This test exercises the REAL
    ``set_rls_org`` against a transaction-scoped fake session so the org is
    present iff the decrypt runs inside the same org-scoped transaction. A
    healthy 200 proves the fix; the pre-fix ordering would read org=None and
    502.
    """
    scoped_session = _ScopedSession()
    backend_holder: dict[str, object] = {}

    async def override_session() -> AsyncGenerator[Any, None]:
        yield scoped_session

    app.dependency_overrides[get_db_session] = override_session

    class _OrgAwareSecretsBackend:
        def __init__(self, session: Any, **_: object) -> None:
            self._session = session
            self.seen_orgs: list[object] = []

        async def get_secret(self, key: str) -> str:
            org = self._session.info.get("org_id")
            self.seen_orgs.append(org)
            if org is None:
                raise RuntimeError("RLS organisation context not set at decrypt time")
            return '{"token": "secret123"}'

        async def set_secret(self, key: str, value: str) -> None:
            return None

        async def delete_secret(self, key: str) -> None:
            return None

    class _FakeConnector:
        async def health_check(self) -> SimpleNamespace:
            return SimpleNamespace(ok=True, detail="connected")

    class _FakeHub:
        def __init__(self, secrets_backend: object, **_: object) -> None:
            backend_holder["backend"] = secrets_backend
            self._secrets_backend = secrets_backend
            self._connector = _FakeConnector()

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> bool:
            return False

        async def initialise(self, instances: object, **_: object) -> None:
            for inst in instances:  # type: ignore[union-attr]
                await self._secrets_backend.get_secret(str(cast(Any, inst).id))

        def get(self, connector_id: uuid.UUID) -> _FakeConnector:
            return self._connector

    def fake_create_secrets_backend(
        *,
        fernet_key: str | None = None,
        session: object = None,
        old_key: str | None = None,
        backend_name: str | None = None,
    ) -> _OrgAwareSecretsBackend:
        return _OrgAwareSecretsBackend(session=session)

    with (
        patch("modulo.api.routes.connectors.create_secrets_backend", fake_create_secrets_backend),
        patch("modulo.api.routes.connectors.ConnectorHub", _FakeHub),
    ):
        resp = client.get(f"/api/v1/connectors/{_CONNECTOR_ID}/health")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["detail"] == "connected"
    backend = backend_holder["backend"]
    assert isinstance(backend, _OrgAwareSecretsBackend)
    # The SAME session that scoped the org must be passed to the backend, AND the
    # org must still be present at decrypt time — i.e. the decrypt ran inside the
    # org-scoping transaction. The pre-fix code read org=None here and 502'd.
    assert backend.seen_orgs == [_ORG_ID]


def _nested_secret_config() -> dict[str, object]:
    return {
        "headers": {"Authorization": "Bearer github_pat_1111111111111111111111", "token": "abc123"},
        "base_url": "https://user:pass@example.com",
        "operations": {"get": {"params": {"api_key": "sk-123456"}}},
    }


def test_get_connector_masks_nested_config_json(client: TestClient) -> None:
    """GET must mask a secret buried in a nested header / base_url / operation."""
    connector = _make_connector()
    connector.config_json = _nested_secret_config()
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=connector),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/connectors/{_CONNECTOR_ID}")
    assert resp.status_code == 200
    cfg = resp.json()["config_json"]
    assert cfg["headers"]["Authorization"] == f"Bearer {SENSITIVE_VALUE_MASK}"
    assert cfg["headers"]["token"] == SENSITIVE_VALUE_MASK
    assert cfg["base_url"] == f"https://user:{SENSITIVE_VALUE_MASK}@example.com"
    assert cfg["operations"]["get"]["params"]["api_key"] == SENSITIVE_VALUE_MASK


def test_list_connectors_masks_nested_config_json(client: TestClient) -> None:
    """The low-privilege connector.list surface must not leak nested secrets."""
    connector = _make_connector()
    connector.config_json = _nested_secret_config()
    page_result = MagicMock(items=[connector], total=1, page=1, page_size=20, next_cursor=None)
    with (
        patch("modulo.api.routes.connectors.list_connector_instances", return_value=page_result),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/connectors")
    assert resp.status_code == 200
    cfg = resp.json()["items"][0]["config_json"]
    assert cfg["headers"]["Authorization"] == f"Bearer {SENSITIVE_VALUE_MASK}"
    assert cfg["base_url"] == f"https://user:{SENSITIVE_VALUE_MASK}@example.com"
    assert cfg["operations"]["get"]["params"]["api_key"] == SENSITIVE_VALUE_MASK


def test_patch_masked_nested_value_does_not_overwrite_secret(client: TestClient) -> None:
    """A masked nested value read via GET must not clobber the stored secret
    when PATCHed back (read-modify-write round-trip guard)."""
    stored = _nested_secret_config()
    connector = _make_connector()
    connector.config_json = stored
    captured: list[dict[str, object]] = []

    async def fake_update(session: object, connector_id: object, updates: dict[str, object]) -> MagicMock:
        captured.append(updates)
        return connector

    masked_payload = {
        "config_json": {
            "headers": {"Authorization": f"Bearer {SENSITIVE_VALUE_MASK}", "token": SENSITIVE_VALUE_MASK},
            "base_url": f"https://user:{SENSITIVE_VALUE_MASK}@example.com",
            "operations": {"get": {"params": {"api_key": SENSITIVE_VALUE_MASK}}},
        }
    }
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=connector),
        patch("modulo.api.routes.connectors.update_connector_instance", new=fake_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/connectors/{_CONNECTOR_ID}", json=masked_payload)

    assert resp.status_code == 200
    assert captured, "update_connector_instance was not called"
    merged = captured[0]["config_json"]
    assert isinstance(merged, dict)
    assert isinstance(merged["headers"], dict)
    assert isinstance(merged["operations"], dict)
    assert merged["headers"]["Authorization"] == "Bearer github_pat_1111111111111111111111"
    assert merged["headers"]["token"] == "abc123"
    assert merged["base_url"] == "https://user:pass@example.com"
    assert isinstance(merged["operations"]["get"], dict)
    assert isinstance(merged["operations"]["get"]["params"], dict)
    assert merged["operations"]["get"]["params"]["api_key"] == "sk-123456"


def test_patch_config_json_merges_top_level(client: TestClient) -> None:
    """A non-masked PATCH still merges (does not replace) the stored config."""
    connector = _make_connector()
    connector.config_json = {"name": "Stored", "tokens": ["a"]}
    captured: list[dict[str, object]] = []

    async def fake_update(session: object, connector_id: object, updates: dict[str, object]) -> MagicMock:
        captured.append(updates)
        return connector

    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=connector),
        patch("modulo.api.routes.connectors.update_connector_instance", new=fake_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/connectors/{_CONNECTOR_ID}", json={"config_json": {"tokens": ["a", "b"]}})

    assert resp.status_code == 200
    assert captured
    merged = captured[0]["config_json"]
    assert merged["name"] == "Stored"
    assert merged["tokens"] == ["a", "b"]


def _make_rest_connector(credentials_ciphertext: bytes) -> MagicMock:
    ci = _make_connector(credentials_ciphertext=credentials_ciphertext)
    ci.connector_type_id = "rest"
    return ci


def _encrypt_creds(creds: dict[str, object]) -> bytes:
    return Fernet(_FERNET_KEY.encode()).encrypt(json.dumps(creds).encode())


def test_patch_identity_only_edit_preserves_secret_and_updates_identity(client: TestClient) -> None:
    """PATCH with credentials containing an identity change but NO secret value
    must leave the stored secret intact AND apply the new identity (so
    ``_normalise_auth`` reads the new identity)."""
    stored = {"auth_mode": "api_key", "api_key": "secret123", "in": "header", "header_name": "X-Key"}
    existing = _make_rest_connector(_encrypt_creds(stored))
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        if "credentials_ciphertext" in updates:
            existing.credentials_ciphertext = updates["credentials_ciphertext"]
        return existing

    mock_update.side_effect = fake_update
    incoming = {"auth_mode": "api_key", "api_key": "", "in": "header", "header_name": "X-Key-V2"}
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": json.dumps(incoming), "name": "Test Connector"},
        )
    assert resp.status_code == 200
    decoded = json.loads(Fernet(_FERNET_KEY.encode()).decrypt(captured["updates"]["credentials_ciphertext"]).decode())
    assert decoded["api_key"] == "secret123", f"Secret must be preserved, got {decoded}"
    assert decoded["header_name"] == "X-Key-V2", f"Identity must be updated, got {decoded}"
    assert decoded["auth_mode"] == "api_key"


def test_patch_with_real_secret_replaces_it(client: TestClient) -> None:
    """PATCH with a real (non-empty) secret value replaces the stored secret."""
    stored = {"auth_mode": "api_key", "api_key": "old-secret", "in": "header", "header_name": "X-Key"}
    existing = _make_rest_connector(_encrypt_creds(stored))
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        if "credentials_ciphertext" in updates:
            existing.credentials_ciphertext = updates["credentials_ciphertext"]
        return existing

    mock_update.side_effect = fake_update
    incoming = {"auth_mode": "api_key", "api_key": "new-secret", "in": "header", "header_name": "X-Key"}
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/connectors/{_CONNECTOR_ID}", json={"credentials": json.dumps(incoming)})
    assert resp.status_code == 200
    decoded = json.loads(Fernet(_FERNET_KEY.encode()).decrypt(captured["updates"]["credentials_ciphertext"]).decode())
    assert decoded["api_key"] == "new-secret", f"Secret must be replaced, got {decoded}"


def test_patch_identity_only_edit_round_trip_reads_new_identity(client: TestClient) -> None:
    """After an identity-only PATCH, the persisted credential, when decrypted and
    passed to the REST connector's ``_normalise_auth``, yields the NEW identity."""
    from modulo.connectors.rest import RestConnector

    stored = {"auth_mode": "api_key", "api_key": "secret123", "in": "header", "header_name": "X-Key"}
    existing = _make_rest_connector(_encrypt_creds(stored))
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        if "credentials_ciphertext" in updates:
            existing.credentials_ciphertext = updates["credentials_ciphertext"]
        return existing

    mock_update.side_effect = fake_update
    incoming = {"auth_mode": "api_key", "api_key": "", "in": "header", "header_name": "X-Key-V2"}
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/connectors/{_CONNECTOR_ID}", json={"credentials": json.dumps(incoming)})
    assert resp.status_code == 200
    stored_after = json.loads(
        Fernet(_FERNET_KEY.encode()).decrypt(captured["updates"]["credentials_ciphertext"]).decode()
    )
    auth = RestConnector._normalise_auth(stored_after)
    assert auth["header_name"] == "X-Key-V2"
    assert auth["api_key"] == "secret123"


def test_patch_non_rest_full_replace_not_overlaid(client: TestClient) -> None:
    """Non-REST connectors keep historical FULL-REPLACE semantics: a partial
    credential dict REPLACES the stored credential outright (no identity-vs-secret
    overlay), so the secret the incoming payload omits is dropped — unchanged
    behaviour from before FAR-466."""
    stored = {"bot_token": "secret123", "other": "x"}
    existing = _make_connector(_encrypt_creds(stored))  # connector_type_id = "filesystem"
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        if "credentials_ciphertext" in updates:
            existing.credentials_ciphertext = updates["credentials_ciphertext"]
        return existing

    mock_update.side_effect = fake_update
    incoming = {"other": "y"}  # partial — omits bot_token
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/connectors/{_CONNECTOR_ID}", json={"credentials": json.dumps(incoming)})
    assert resp.status_code == 200
    decoded = json.loads(Fernet(_FERNET_KEY.encode()).decrypt(captured["updates"]["credentials_ciphertext"]).decode())
    assert decoded == {"other": "y"}, f"Non-REST must FULL-REPLACE (no overlay), got {decoded}"
    assert "bot_token" not in decoded, f"Non-REST full-replace must drop an omitted field, got {decoded}"


def test_patch_undecryptable_ciphertext_does_not_wipe_secret(client: TestClient) -> None:
    """A stored ciphertext that EXISTS but cannot be decrypted must never be
    silently degraded to {} and re-encrypted as a secret-free overlay (which
    would wipe the secret). The PATCH fails loudly (5xx) and does NOT commit a
    credential write."""
    existing = _make_rest_connector(b"not-a-valid-fern-ciphertext")
    mock_update = AsyncMock()
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": json.dumps({"auth_mode": "api_key", "api_key": "", "header_name": "X-Key"})},
        )
    assert resp.status_code == 500
    assert "could not be decrypted" in resp.json()["detail"]
    # The secret was never re-encrypted & written: update_connector_instance did not run.
    mock_update.assert_not_awaited()


def test_patch_rest_complete_replacement_recovers_undecryptable_ciphertext(client: TestClient) -> None:
    """A COMPLETE incoming credential payload (every required secret present with
    a real, non-empty, non-masked value) is encrypted verbatim WITHOUT touching
    the stored ciphertext — the recovery path for legacy rows whose stored
    ciphertext is undecryptable (previously a permanent 500 even for a full
    replacement)."""
    existing = _make_rest_connector(b"not-a-valid-fern-ciphertext")
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        if "credentials_ciphertext" in updates:
            existing.credentials_ciphertext = updates["credentials_ciphertext"]
        return existing

    mock_update.side_effect = fake_update
    incoming = {"auth_mode": "api_key", "api_key": "new-secret", "in": "header", "header_name": "X-Key"}
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/connectors/{_CONNECTOR_ID}", json={"credentials": json.dumps(incoming)})
    assert resp.status_code == 200
    decoded = json.loads(Fernet(_FERNET_KEY.encode()).decrypt(captured["updates"]["credentials_ciphertext"]).decode())
    assert decoded == incoming, f"Complete payload must be stored verbatim, got {decoded}"
    mock_update.assert_awaited_once()


def test_patch_rest_complete_bearer_replacement_recovers_undecryptable_ciphertext(client: TestClient) -> None:
    """The verbatim recovery path is auth_mode-agnostic: a complete bearer
    replacement (token present) also succeeds against an undecryptable stored
    ciphertext and drops the previous api_key credential entirely."""
    existing = _make_rest_connector(b"not-a-valid-fern-ciphertext")
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        if "credentials_ciphertext" in updates:
            existing.credentials_ciphertext = updates["credentials_ciphertext"]
        return existing

    mock_update.side_effect = fake_update
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": json.dumps({"auth_mode": "bearer", "token": "t"})},
        )
    assert resp.status_code == 200
    decoded = json.loads(Fernet(_FERNET_KEY.encode()).decrypt(captured["updates"]["credentials_ciphertext"]).decode())
    assert decoded == {"auth_mode": "bearer", "token": "t"}, f"Verbatim payload expected, got {decoded}"


def test_patch_rest_masked_secret_on_undecryptable_ciphertext_still_500(client: TestClient) -> None:
    """A masked secret means "keep the stored value" — on an undecryptable stored
    ciphertext the overlay path must still fail loudly (500). The verbatim
    recovery path must never swallow a masked placeholder (that would wipe the
    stored secret with the literal mask string on the next successful decrypt
    cycle)."""
    existing = _make_rest_connector(b"not-a-valid-fern-ciphertext")
    mock_update = AsyncMock()
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={
                "credentials": json.dumps(
                    {"auth_mode": "api_key", "api_key": SENSITIVE_VALUE_MASK, "in": "header", "header_name": "X-Key"}
                )
            },
        )
    assert resp.status_code == 500
    assert "could not be decrypted" in resp.json()["detail"]
    mock_update.assert_not_awaited()


def test_patch_undecryptable_ciphertext_logs_server_side(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """The StoredCredentialDecryptError handler must log server-side before the
    500 — a silent re-raise leaves no trace for operators to diagnose."""
    existing = _make_rest_connector(b"not-a-valid-fern-ciphertext")
    mock_update = AsyncMock()
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
        caplog.at_level(logging.ERROR, logger="modulo.api.routes.connectors"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": json.dumps({"auth_mode": "api_key", "api_key": "", "header_name": "X-Key"})},
        )
    assert resp.status_code == 500
    assert "stored_credential_decrypt_failed" in caplog.text
    mock_update.assert_not_awaited()


def test_patch_non_rest_name_only_null_credentials_no_500(client: TestClient) -> None:
    """A non-REST name-only edit posting credentials: null (empty config textarea)
    must NOT 500 — the credential write is skipped and the name is updated."""
    existing = _make_connector()  # connector_type_id = "filesystem"
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        return existing

    mock_update.side_effect = fake_update
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"name": "Renamed", "credentials": None},
        )
    assert resp.status_code == 200
    assert captured["updates"].get("name") == "Renamed"
    assert "credentials_ciphertext" not in captured["updates"]


def test_patch_rest_overlay_bearer_without_token_rejected(client: TestClient) -> None:
    """A direct PATCH overlaying auth_mode=bearer onto an api_key connector with
    no token must be rejected (422) and must NOT persist the broken credential.
    The UI blocks this; the API must not silently save a connector whose first
    run raises at ``RestConnector._normalise_auth`` (bearer requires token)."""
    stored = {"auth_mode": "api_key", "api_key": "secret123", "in": "header", "header_name": "X-Key"}
    existing = _make_rest_connector(_encrypt_creds(stored))
    mock_update = AsyncMock()
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": json.dumps({"auth_mode": "bearer"})},
        )
    assert resp.status_code == 422
    assert "REST bearer auth requires creds['token']" in resp.json()["detail"]
    mock_update.assert_not_awaited()


def test_patch_rest_overlay_bearer_with_token_succeeds(client: TestClient) -> None:
    """A PATCH supplying a complete bearer credential succeeds and persists it
    (the connector's auth contract is satisfied after the overlay)."""
    stored = {"auth_mode": "api_key", "api_key": "secret123", "in": "header", "header_name": "X-Key"}
    existing = _make_rest_connector(_encrypt_creds(stored))
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        if "credentials_ciphertext" in updates:
            existing.credentials_ciphertext = updates["credentials_ciphertext"]
        return existing

    mock_update.side_effect = fake_update
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": json.dumps({"auth_mode": "bearer", "token": "t"})},
        )
    assert resp.status_code == 200
    decoded = json.loads(Fernet(_FERNET_KEY.encode()).decrypt(captured["updates"]["credentials_ciphertext"]).decode())
    assert decoded["auth_mode"] == "bearer"
    assert decoded["token"] == "t"


def _make_github_patch_connector() -> MagicMock:
    ci = _make_connector(credentials_ciphertext=_encrypt_creds({"token": "old"}))
    ci.connector_type_id = "github"
    return ci


def test_patch_github_raw_token_not_validated(client: TestClient) -> None:
    """The GitHub raw-token (non-JSON credential) PATCH path keeps its historical
    semantics — it is persisted verbatim and is NOT run through the REST
    auth-contract validation, so a raw token is never rejected as a broken REST
    credential."""
    existing = _make_github_patch_connector()
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        if "credentials_ciphertext" in updates:
            existing.credentials_ciphertext = updates["credentials_ciphertext"]
        return existing

    mock_update.side_effect = fake_update
    token = "ghp_rawtokenvalue"
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.GitHubConnector.verify_scopes", new=AsyncMock(return_value=set())),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": token},
        )
    assert resp.status_code == 200
    decrypted = Fernet(_FERNET_KEY.encode()).decrypt(captured["updates"]["credentials_ciphertext"]).decode()
    assert decrypted == token  # persisted verbatim, not overlaid/validated


def test_patch_rest_bare_string_credential_rejected(client: TestClient) -> None:
    """A raw non-JSON (bare-string) credential payload on a REST connector must be
    rejected (422), NOT encrypted verbatim. REST credentials are always a JSON
    object; a bare string would make ``_normalise_auth`` (which calls ``.get()``)
    blow up on the first run. The raw-token path is legit only for non-REST
    connectors (e.g. github), which the test above covers."""
    existing = _make_rest_connector(_encrypt_creds({"auth_mode": "api_key", "api_key": "secret123"}))
    mock_update = AsyncMock()
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": "raw-token-value"},
        )
    assert resp.status_code == 422
    assert "must be a JSON object" in resp.json()["detail"]
    mock_update.assert_not_awaited()


def test_create_rest_bearer_without_token_rejected(client: TestClient) -> None:
    """POST a REST connector with auth_mode=bearer but NO token must be rejected
    (422) and NOT saved. REST credentials are validated at the create boundary so
    a direct POST cannot persist a broken credential the connector rejects at run
    time."""
    body = {
        "name": "REST Connector",
        "connector_type_id": "rest",
        "credentials": json.dumps({"auth_mode": "bearer"}),
        "config_json": {},
    }
    with (
        patch("modulo.api.routes.connectors.create_connector_instance") as mock_create,
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=body)
    assert resp.status_code == 422
    assert "REST bearer auth requires creds['token']" in resp.json()["detail"]
    mock_create.assert_not_awaited()


def test_create_rest_bearer_with_token_succeeds(client: TestClient) -> None:
    """POST a REST connector with a complete bearer credential succeeds (201)."""
    body = {
        "name": "REST Connector",
        "connector_type_id": "rest",
        "credentials": json.dumps({"auth_mode": "bearer", "token": "t"}),
        "config_json": {},
    }
    connector = _make_rest_connector(b"encrypted_bytes")
    with (
        patch("modulo.api.routes.connectors.create_connector_instance", return_value=connector),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=body)
    assert resp.status_code == 201
    assert resp.json()["has_credentials"] is True


def test_update_connector_null_credentials_skips_write(client: TestClient) -> None:
    """FAR-495 QA regression: PATCH {"credentials": null} must NOT 500. An explicit
    ``null`` credential is treated as "no credential change" — the stored secret is
    left intact (never wiped or re-encrypted) and the request succeeds. The
    historical bug was ``_encrypt(None, ...)`` raising ``AttributeError`` → unhandled
    500; the fix is to skip the credential write, not to reject with 422."""
    existing = _make_connector()  # has stored credentials
    captured: dict[str, Any] = {}
    mock_update = AsyncMock()

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        return existing

    mock_update.side_effect = fake_update
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/connectors/{_CONNECTOR_ID}", json={"credentials": None})
    assert resp.status_code == 200
    assert "credentials_ciphertext" not in captured["updates"], (
        "Explicit null credentials must NOT wipe the stored secret"
    )


def test_connector_response_surfaces_degraded_markers(client: TestClient) -> None:
    """FAR-495 read path: degraded_at / last_skip_error written by
    ``mark_instances_degraded`` must be surfaced on the GET connector response
    (operators can see broken connectors), not left as write-only columns."""
    degraded = _make_connector()
    degraded.degraded_at = _NOW
    degraded.last_skip_error = "ValueError: Missing credential key 'token'"

    with patch(
        "modulo.api.routes.connectors.get_connector_instance",
        return_value=degraded,
    ):
        resp = client.get(f"/api/v1/connectors/{_CONNECTOR_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded_at"] is not None
    assert body["degraded_at"].startswith("2025-01-01T00:00:00")
    assert body["last_skip_error"] == "ValueError: Missing credential key 'token'"


def test_create_rest_invalid_on_unknown_config_rejected(client: TestClient) -> None:
    """FAR-532: an invalid config_json.on_unknown must be rejected at the CREATE
    boundary (422) — pre-fix it saved fine and bricked every bound node at run
    time (RestConnector.__init__ raises on a bad mode)."""
    body = {
        "name": "REST Connector",
        "connector_type_id": "rest",
        "credentials": json.dumps({"auth_mode": "bearer", "token": "t"}),
        "config_json": {"base_url": "https://api.example.com", "on_unknown": "bogus"},
    }
    with (
        patch("modulo.api.routes.connectors.create_connector_instance") as mock_create,
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=body)
    assert resp.status_code == 422
    assert "on_unknown" in resp.json()["detail"]
    mock_create.assert_not_awaited()


def test_patch_rest_invalid_on_unknown_config_rejected(client: TestClient) -> None:
    """FAR-532: an invalid config_json.on_unknown must be rejected at the PATCH
    config boundary (422) and the connector must not be updated."""
    existing = _make_rest_connector(_encrypt_creds({"auth_mode": "bearer", "token": "t"}))
    mock_update = AsyncMock()
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"config_json": {"on_unknown": "bogus"}},
        )
    assert resp.status_code == 422
    assert "on_unknown" in resp.json()["detail"]
    mock_update.assert_not_awaited()


def test_patch_rest_on_unknown_case_insensitive_accepted(client: TestClient) -> None:
    """The boundary check mirrors the connector's own normalisation: a
    case-insensitive match (e.g. a legacy uppercase echo 'FAIL_CLOSED') is
    accepted, not rejected."""
    existing = _make_rest_connector(_encrypt_creds({"auth_mode": "bearer", "token": "t"}))
    captured: dict[str, Any] = {}

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        return existing

    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=fake_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"config_json": {"on_unknown": "FAIL_CLOSED"}},
        )
    assert resp.status_code == 200
    assert captured["updates"]["config_json"]["on_unknown"] == "FAIL_CLOSED"


def test_patch_masked_secret_key_preserves_stored_value(client: TestClient) -> None:
    """FAR-532: ``secret`` is secret material (the REST connector's
    ``_secret_values`` collects it) — a masked ``secret`` echo must preserve the
    stored value, never be overlaid as identity (which would store the literal
    mask string where the connector expects secret material)."""
    stored = {"auth_mode": "api_key", "api_key": "secret123", "secret": "legacy-secret-value"}
    existing = _make_rest_connector(_encrypt_creds(stored))
    captured: dict[str, Any] = {}

    async def fake_update(session: object, connector_id: object, updates: dict[str, Any]) -> MagicMock:
        captured["updates"] = updates
        if "credentials_ciphertext" in updates:
            existing.credentials_ciphertext = updates["credentials_ciphertext"]
        return existing

    incoming = {"auth_mode": "api_key", "api_key": "", "secret": SENSITIVE_VALUE_MASK}
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=fake_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": json.dumps(incoming)},
        )
    assert resp.status_code == 200
    decoded = json.loads(Fernet(_FERNET_KEY.encode()).decrypt(captured["updates"]["credentials_ciphertext"]).decode())
    assert decoded["secret"] == "legacy-secret-value", f"Masked secret must preserve the stored value, got {decoded}"
    assert decoded["api_key"] == "secret123"


def test_connectors_route_module_has_no_assert_statements() -> None:
    """FAR-532: bare `assert` guards vanish under `python -O`. The update
    endpoint's credential-flow invariant (a supplied credential payload is
    non-None) must be an explicit defensive branch (500), never an assert —
    pin the route module to zero Assert nodes."""
    module_file = getattr(connectors_module, "__file__", None)
    assert module_file is not None
    source = Path(module_file).read_text(encoding="utf-8")
    assert_nodes = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Assert)]
    assert not assert_nodes, f"assert statements found at lines {[n.lineno for n in assert_nodes]}"


def _overlay(previous: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Local import shim for the route's ``_credential_overlay``."""
    from modulo.api.routes.connectors import _credential_overlay

    return _credential_overlay(previous, incoming)


def test_overlay_masked_or_empty_secret_preserved() -> None:
    """FAR-504 (a): an empty OR masked secret value is preserved (not overwritten)."""
    from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK

    previous = {"auth_mode": "api_key", "api_key": "secret123", "in": "header", "header_name": "X-Key"}
    for blank in ("", SENSITIVE_VALUE_MASK):
        merged = _overlay(previous, {"auth_mode": "api_key", "api_key": blank, "in": "header"})
        assert merged["api_key"] == "secret123", f"Secret must be preserved on {blank!r}, got {merged}"


def test_overlay_real_new_secret_replaces() -> None:
    """FAR-504 (b): a real (non-empty, non-masked) secret value replaces the stored one."""
    previous = {"auth_mode": "api_key", "api_key": "old-secret", "in": "header", "header_name": "X-Key"}
    merged = _overlay(
        previous, {"auth_mode": "api_key", "api_key": "new-secret", "in": "query", "query_param_name": "token"}
    )
    assert merged["api_key"] == "new-secret"
    assert merged["in"] == "query"
    assert merged["query_param_name"] == "token"


def test_overlay_identity_only_edit_applies_secret_survives() -> None:
    """FAR-504 (c): an identity-only edit (auth_mode/in/header_name/query_param_name)
    applies while the stored secret survives — the connector reads identity from
    the overlaid credential."""
    previous = {"auth_mode": "api_key", "api_key": "secret123", "in": "header", "header_name": "X-Key"}
    merged = _overlay(previous, {"auth_mode": "api_key", "in": "query", "query_param_name": "auth-token"})
    assert merged["auth_mode"] == "api_key"
    assert merged["in"] == "query"
    assert merged["query_param_name"] == "auth-token"
    assert merged["api_key"] == "secret123"


def test_overlay_never_drops_a_stored_secret_or_unknown_key() -> None:
    """FAR-504 (d): the overlay never drops a stored secret or an unknown/legacy
    key that the incoming payload omits — it only adds/updates."""
    previous = {
        "auth_mode": "api_key",
        "api_key": "secret123",
        "in": "header",
        "header_name": "X-Key",
        "legacy_field": "keep-me",
    }
    merged = _overlay(previous, {"auth_mode": "api_key", "in": "header", "header_name": "X-Key-V2"})
    assert merged["api_key"] == "secret123", "stored secret must survive"
    assert merged["legacy_field"] == "keep-me", "unknown/legacy key must survive"
    assert merged["header_name"] == "X-Key-V2", "identity update must apply"


def test_rest_validate_credentials_parity_with_connector() -> None:
    """FAR-504 (1): the connector's authoritative ``validate_credentials`` enforces
    exactly the required-secret contract the API boundary relies on, and it agrees
    with ``_normalise_auth`` (both accept or both reject a given credential dict).
    bearer->token; basic->username+password; api_key->api_key + in header/query."""
    from modulo.connectors.rest import RestConnector

    valid = [
        {"auth_mode": "bearer", "token": "t"},
        {"auth_mode": "api_key", "api_key": "k"},
        {"auth_mode": "api_key", "api_key": "k", "in": "header"},
        {"auth_mode": "api_key", "api_key": "k", "in": "query", "query_param_name": "token"},
        {"auth_mode": "basic", "username": "u", "password": "p"},
    ]
    invalid = [
        {"auth_mode": "bearer"},  # no token
        {"auth_mode": "api_key"},  # no api_key
        {"auth_mode": "basic", "username": "u"},  # no password
        {"auth_mode": "basic", "password": "p"},  # no username
        {"auth_mode": "opaque"},  # unsupported mode
        {"auth_mode": "api_key", "api_key": "k", "in": "cookie"},  # api_key in must be header/query
    ]

    for creds in valid:
        RestConnector.validate_credentials(creds)  # must not raise
        RestConnector._normalise_auth(creds)  # must not raise (parity)
    for creds in invalid:
        with pytest.raises(ValueError):
            RestConnector.validate_credentials(creds)
        with pytest.raises(ValueError):
            RestConnector._normalise_auth(creds)


def test_patch_rest_overlay_rejects_missing_basic_secret(client: TestClient) -> None:
    """FAR-504 parity at the API boundary: a PATCH overlaying auth_mode=basic with
    a username but NO password is rejected (422) and not persisted — the connector's
    contract is enforced before the write."""
    stored = {"auth_mode": "bearer", "token": "secret123"}
    existing = _make_rest_connector(_encrypt_creds(stored))
    mock_update = AsyncMock()
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=existing),
        patch("modulo.api.routes.connectors.update_connector_instance", new=mock_update),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            json={"credentials": json.dumps({"auth_mode": "basic", "username": "u"})},
        )
    assert resp.status_code == 422
    assert "REST basic auth requires creds['username'] and creds['password']" in resp.json()["detail"]
    mock_update.assert_not_awaited()
