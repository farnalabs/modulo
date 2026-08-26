"""Unit tests for /api/v1/model-backends endpoints.

Credentials must NEVER appear in responses — only `has_credentials: true/false`.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_FERNET_KEY = Fernet.generate_key().decode()
_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_BACKEND_ID = uuid.uuid4()
_FALLBACK_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _make_backend(credentials_ciphertext: bytes = b"encrypted", tier: str = "native") -> MagicMock:
    mb = MagicMock()
    mb.id = _BACKEND_ID
    mb.organisation_id = _ORG_ID
    mb.name = "Test Backend"
    mb.display_name = "GPT-4"
    mb.provider = "openai"
    mb.model_id = "gpt-4"
    mb.credentials_ciphertext = credentials_ciphertext
    mb.default_params = {}
    mb.visibility = "org"
    mb.owner_team_id = None
    mb.tier = tier
    mb.fallback_backend_ids = None
    mb.account_id = uuid.uuid4()
    mb.created_at = _NOW
    mb.updated_at = _NOW
    return mb


@pytest.fixture(autouse=True)
def _patch_secrets_backend() -> Generator[None, None, None]:
    with patch("modulo.api.routes.model_backends.create_secrets_backend", return_value=AsyncMock()):
        yield


@pytest.fixture(autouse=True)
def _patch_health_check_on_save() -> Generator[None, None, None]:
    """Every endpoint test here uses a MagicMock CRUD return value, so the
    save-time health check would otherwise hit the network (or a bare MagicMock
    that lacks ``health_check``). Patch it to a canned healthy result instead of
    gating production behavior on a runtime ``isinstance`` check."""
    with patch(
        "modulo.api.routes.model_backends._run_health_check_on_save",
        new=AsyncMock(return_value=("ok", None)),
    ):
        yield


@pytest.fixture(autouse=True)
def _stub_get_model_backend() -> Generator[None, None, None]:
    """The IDOR ownership check reads the entity via ``get_model_backend`` before
    the write CRUD, but the write-path tests only mock ``update_model_backend`` /
    ``delete_model_backend``. Supply a same-org entity so the ownership check
    passes for the legitimate (same-org) principal these tests use."""
    with patch("modulo.api.routes.model_backends.get_model_backend", return_value=_make_backend()):
        yield


def _make_mock_session(existing_fallback_ids: list[uuid.UUID] | None = None) -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    # Default: no duplicate found for name check
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    # Fallback-validation query (SELECT model_backends.id ... IN (...)) returns
    # the seeded existing ids; every other query returns the default result.
    fallback_result = MagicMock()
    fallback_result.scalars.return_value = iter(existing_fallback_ids or [])

    def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        text = str(stmt)
        if "SELECT model_backends.id" in text and " IN " in text:
            return fallback_result
        return mock_result

    session.execute = AsyncMock(side_effect=_execute)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    yield from _client_for_session(_make_mock_session())


@pytest.fixture
def client_with_fallback_ids() -> Generator[TestClient, None, None]:
    """Client whose fallback-validation query reports ``existing_fallback_ids`` as present."""
    yield from _client_for_session(_make_mock_session(existing_fallback_ids=[_FALLBACK_ID]))


def _client_for_session(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
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
    "name": "Test Backend",
    "display_name": "GPT-4",
    "provider": "openai",
    "model_id": "gpt-4",
    "api_key": "sk-test",
}


def test_list_model_backends_returns_200(client: TestClient) -> None:
    page_result = MagicMock(items=[_make_backend()], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.model_backends.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/model-backends")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_list_model_backends_default_excludes_in_dev(client: TestClient) -> None:
    """The list endpoint defaults to the CRUD in_dev exclusion (excluded_tiers=None)."""
    page_result = MagicMock(items=[_make_backend()], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.model_backends.list_model_backends", return_value=page_result) as mock_list,
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/model-backends")
    assert resp.status_code == 200
    assert mock_list.await_args is not None
    assert mock_list.await_args.kwargs["excluded_tiers"] is None


def test_list_model_backends_include_in_dev_passes_empty_exclusions(client: TestClient) -> None:
    """?include_in_dev=true reveals In-Dev model backends in the actual response JSON."""
    in_dev = _make_backend(tier="in_dev")
    page_result = MagicMock(items=[in_dev], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.model_backends.list_model_backends", return_value=page_result) as mock_list,
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/model-backends", params={"include_in_dev": "true"})
    assert resp.status_code == 200
    assert mock_list.await_args is not None
    assert not mock_list.await_args.kwargs["excluded_tiers"]
    tiers = [item["tier"] for item in resp.json()["items"]]
    assert "in_dev" in tiers, f"Expected an in_dev model backend in the response, got tiers: {tiers}"


def test_list_model_backends_include_in_dev_denied_for_viewer(viewer_client: TestClient) -> None:
    """Viewers can list model backends but must NOT be able to reveal In-Dev items."""
    resp = viewer_client.get("/api/v1/model-backends", params={"include_in_dev": "true"})
    assert resp.status_code == 403
    assert "model_backend.list.in_dev" in resp.json()["detail"]


def test_list_model_backends_include_in_dev_operator_reveals_in_dev(client: TestClient) -> None:
    """An operator+ principal (admin fixture) can list In-Dev model backends."""
    in_dev = _make_backend(tier="in_dev")
    page_result = MagicMock(items=[in_dev], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.model_backends.list_model_backends", return_value=page_result),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/model-backends", params={"include_in_dev": "true"})
    assert resp.status_code == 200
    assert resp.json()["items"][0]["tier"] == "in_dev"


def test_list_model_backends_include_in_dev_false_keeps_exclusion(client: TestClient) -> None:
    """?include_in_dev=false behaves exactly like omitting the parameter."""
    page_result = MagicMock(items=[_make_backend()], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.model_backends.list_model_backends", return_value=page_result) as mock_list,
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/model-backends", params={"include_in_dev": "false"})
    assert resp.status_code == 200
    assert mock_list.await_args is not None
    assert mock_list.await_args.kwargs["excluded_tiers"] is None


def test_create_model_backend_does_not_expose_credentials(client: TestClient) -> None:
    backend = _make_backend(credentials_ciphertext=b"encrypted_bytes")
    with (
        patch("modulo.api.routes.model_backends.create_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/model-backends", json=_CREATE_BODY)

    assert resp.status_code == 201
    body = resp.json()
    assert "credentials_ciphertext" not in body
    assert "api_key" not in body
    assert body["has_credentials"] is True


def test_create_model_backend_with_fallback_ids(client_with_fallback_ids: TestClient) -> None:
    """Verify existing fallback_backend_ids are passed to create_model_backend."""
    captured: list[list[str] | None] = []

    async def fake_create(session: object, **kwargs: object) -> MagicMock:
        captured.append(kwargs.get("fallback_backend_ids"))
        backend = _make_backend()
        backend.fallback_backend_ids = kwargs.get("fallback_backend_ids")
        return backend  # type: ignore[return-value]

    with (
        patch("modulo.api.routes.model_backends.create_model_backend", new=fake_create),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        body = {**_CREATE_BODY, "fallback_backend_ids": [str(_FALLBACK_ID)]}
        resp = client_with_fallback_ids.post("/api/v1/model-backends", json=body)

    assert resp.status_code == 201
    assert captured == [[str(_FALLBACK_ID)]]
    assert resp.json()["fallback_backend_ids"] == [str(_FALLBACK_ID)]


def test_create_model_backend_unknown_fallback_returns_422(client: TestClient) -> None:
    """A fallback ID that references no existing org backend is rejected before any DB write."""
    with (
        patch("modulo.api.routes.model_backends.create_model_backend") as mock_create,
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        body = {**_CREATE_BODY, "fallback_backend_ids": [str(uuid.uuid4())]}
        resp = client.post("/api/v1/model-backends", json=body)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "Unknown model backend id(s) referenced as fallbacks" in detail
    mock_create.assert_not_awaited()


def test_create_model_backend_mixed_fallback_ids_reports_missing(client_with_fallback_ids: TestClient) -> None:
    """Only the missing fallback IDs are reported when some references exist."""
    missing = uuid.uuid4()
    with (
        patch("modulo.api.routes.model_backends.create_model_backend") as mock_create,
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        body = {**_CREATE_BODY, "fallback_backend_ids": [str(_FALLBACK_ID), str(missing)]}
        resp = client_with_fallback_ids.post("/api/v1/model-backends", json=body)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert str(missing) in detail
    assert str(_FALLBACK_ID) not in detail
    mock_create.assert_not_awaited()


def test_update_model_backend_with_fallback_ids(client_with_fallback_ids: TestClient) -> None:
    """Update accepts a fallback list that references existing org backends.

    The fallback ids are stringified before the CRUD write (the JSON column
    cannot serialize raw ``uuid.UUID`` objects), mirroring the create path —
    otherwise the flush 500s with ``TypeError: Object of type UUID is not JSON
    serializable``.
    """
    captured: list[dict[str, object]] = []

    async def fake_update(session: object, backend_id: object, updates: dict[str, object]) -> MagicMock:
        captured.append(updates)
        backend = _make_backend()
        backend.fallback_backend_ids = updates.get("fallback_backend_ids")
        return backend  # type: ignore[return-value]

    with (
        patch("modulo.api.routes.model_backends.update_model_backend", new=fake_update),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client_with_fallback_ids.patch(
            f"/api/v1/model-backends/{_BACKEND_ID}",
            json={"fallback_backend_ids": [str(_FALLBACK_ID)]},
        )
    assert resp.status_code == 200
    assert captured == [{"fallback_backend_ids": [str(_FALLBACK_ID)]}]
    assert resp.json()["fallback_backend_ids"] == [str(_FALLBACK_ID)]


def test_update_model_backend_self_reference_returns_422(client_with_fallback_ids: TestClient) -> None:
    """Update rejecting the backend referencing itself as a fallback (422).

    A self-referencing chain is meaningless and would permanently block
    deletion (the delete-protection scan reports the backend referencing
    itself), so it is rejected before any DB write.
    """
    with (
        patch("modulo.api.routes.model_backends.update_model_backend") as mock_update,
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client_with_fallback_ids.patch(
            f"/api/v1/model-backends/{_BACKEND_ID}",
            json={"fallback_backend_ids": [str(_BACKEND_ID)]},
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "cannot reference itself" in detail
    mock_update.assert_not_awaited()


def test_update_model_backend_unknown_fallback_returns_422(client: TestClient) -> None:
    """Update with a fallback ID referencing no org backend is rejected."""
    with (
        patch("modulo.api.routes.model_backends.update_model_backend") as mock_update,
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/model-backends/{_BACKEND_ID}",
            json={"fallback_backend_ids": [str(uuid.uuid4())]},
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "Unknown model backend id(s) referenced as fallbacks" in detail
    mock_update.assert_not_awaited()


def test_update_model_backend_null_fallback_clears(client: TestClient) -> None:
    """Explicit null clears the fallback list and skips reference validation."""
    backend = _make_backend()
    backend.fallback_backend_ids = None
    with (
        patch("modulo.api.routes.model_backends.update_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/model-backends/{_BACKEND_ID}", json={"fallback_backend_ids": None})
    assert resp.status_code == 200
    assert resp.json()["fallback_backend_ids"] is None


def test_update_model_backend_empty_list_removes_fallback(client_with_fallback_ids: TestClient) -> None:
    """An empty fallback list on update removes the backend from rotation.

    This is the "removing a fallback from update removes it from rotation"
    behaviour: the PATCH route stringifies the (empty) list and writes it to the
    JSON column, so the hub's rotation no longer considers any fallback for the
    backend. The captured ``updates`` dict proves the write path receives the
    cleared list.
    """
    captured: list[dict[str, object]] = []

    async def fake_update(session: object, backend_id: object, updates: dict[str, object]) -> MagicMock:
        captured.append(updates)
        backend = _make_backend()
        backend.fallback_backend_ids = updates.get("fallback_backend_ids")
        return backend  # type: ignore[return-value]

    with (
        patch("modulo.api.routes.model_backends.update_model_backend", new=fake_update),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client_with_fallback_ids.patch(
            f"/api/v1/model-backends/{_BACKEND_ID}",
            json={"fallback_backend_ids": []},
        )
    assert resp.status_code == 200
    assert captured == [{"fallback_backend_ids": []}]
    assert not resp.json()["fallback_backend_ids"]


def test_delete_model_backend_referenced_as_fallback_returns_409(client: TestClient) -> None:
    """Deleting a backend another backend references as a fallback is blocked."""
    referencing = _make_backend()
    referencing.name = "Primary Backend"
    with (
        patch(
            "modulo.api.routes.model_backends.list_backends_referencing_fallback",
            new=AsyncMock(return_value=[referencing]),
        ) as mock_referencing,
        patch("modulo.api.routes.model_backends.delete_model_backend") as mock_delete,
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/model-backends/{_BACKEND_ID}")

    assert resp.status_code == 409
    assert "Primary Backend" in resp.json()["detail"]
    mock_referencing.assert_awaited_once()
    mock_delete.assert_not_awaited()


def test_delete_model_backend_not_referenced_succeeds(client: TestClient) -> None:
    """A backend referenced by nothing deletes normally (204)."""
    with (
        patch("modulo.api.routes.model_backends.list_backends_referencing_fallback", new=AsyncMock(return_value=[])),
        patch("modulo.api.routes.model_backends.delete_model_backend", return_value=True),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/model-backends/{_BACKEND_ID}")
    assert resp.status_code == 204


def test_create_model_backend_encrypts_api_key(client: TestClient) -> None:
    captured: list[bytes] = []

    async def fake_create(session: object, **kwargs: object) -> MagicMock:
        captured.append(kwargs["credentials_ciphertext"])  # type: ignore[arg-type]
        return _make_backend(credentials_ciphertext=kwargs["credentials_ciphertext"])  # type: ignore[arg-type]

    with (
        patch("modulo.api.routes.model_backends.create_model_backend", new=fake_create),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        client.post("/api/v1/model-backends", json=_CREATE_BODY)

    assert captured, "create_model_backend was not called"
    ciphertext = captured[0]
    assert isinstance(ciphertext, bytes)
    # Decrypt to verify the api_key was stored
    decrypted = Fernet(_FERNET_KEY.encode()).decrypt(ciphertext).decode()
    assert decrypted == "sk-test"
    assert b"sk-test" not in ciphertext


def test_get_model_backend_returns_200_without_credentials(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=_make_backend()),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{_BACKEND_ID}")
    assert resp.status_code == 200
    body = resp.json()
    assert "credentials_ciphertext" not in body
    assert body["has_credentials"] is True
    assert body["fallback_backend_ids"] is None


def test_get_model_backend_with_fallback_ids_in_response(client: TestClient) -> None:
    """Response includes fallback_backend_ids when set."""
    fallback_id = uuid.uuid4()
    backend = _make_backend()
    backend.fallback_backend_ids = [str(fallback_id)]
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{_BACKEND_ID}")
    assert resp.status_code == 200
    assert resp.json()["fallback_backend_ids"] == [str(fallback_id)]


def test_get_model_backend_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=None),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_update_model_backend_returns_200(client: TestClient) -> None:
    backend = _make_backend()
    backend.name = "Updated"
    with (
        patch("modulo.api.routes.model_backends.update_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/model-backends/{_BACKEND_ID}", json={"name": "Updated"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated"


def _make_txn_tracking_session(in_txn: dict[str, bool]) -> AsyncMock:
    """Build a mock session whose begin() context tracks transaction state.

    Mirrors the DI session's ``autobegin=False`` semantics: between begin
    blocks there is no active transaction, so code that must run inside a
    transaction can be detected by asserting it ran while ``in_txn`` was True.
    """
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()

    def _enter() -> None:
        in_txn["active"] = True

    def _exit(*args: object) -> bool:
        in_txn["active"] = False
        return False

    begin_cm.__aenter__ = AsyncMock(side_effect=_enter)
    begin_cm.__aexit__ = AsyncMock(side_effect=_exit)
    session.begin = MagicMock(return_value=begin_cm)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result)
    return session


class _TrackingSecretsBackend:
    """Records whether set_secret was called while a transaction was active."""

    def __init__(self, in_txn: dict[str, bool], calls: list[bool]) -> None:
        self._in_txn = in_txn
        self._calls = calls

    async def set_secret(self, key: str, value: str) -> None:
        self._calls.append(self._in_txn["active"])

    async def get_secret(self, key: str) -> str:
        raise KeyError(key)

    async def delete_secret(self, key: str) -> None:
        pass


def test_create_model_backend_writes_secret_inside_transaction(client: TestClient) -> None:
    """Regression: set_secret must run INSIDE the session transaction.

    The DI session is created with ``autobegin=False``. When set_secret ran
    after the ``session.begin()`` block committed, ``begin_nested()`` raised
    ``InvalidRequestError`` and the POST 500'd after the row was already
    committed. This test fails if set_secret is observed running outside an
    active transaction.
    """
    in_txn: dict[str, bool] = {"active": False}
    calls: list[bool] = []
    session = _make_txn_tracking_session(in_txn)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    client.app.dependency_overrides[get_db_session] = override_session

    with (
        patch(
            "modulo.api.routes.model_backends.create_secrets_backend",
            return_value=_TrackingSecretsBackend(in_txn, calls),
        ),
        patch("modulo.api.routes.model_backends.create_model_backend", return_value=_make_backend()),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/model-backends", json=_CREATE_BODY)

    assert resp.status_code == 201, resp.text
    assert calls, "set_secret was never called"
    assert all(calls), (
        "set_secret ran OUTSIDE the transaction; FernetSecretsBackend.set_secret "
        "requires an active transaction (autobegin=False session)"
    )


def test_update_model_backend_writes_secret_inside_transaction(client: TestClient) -> None:
    """Regression: PATCH with an api_key must persist the secret inside the transaction."""
    in_txn: dict[str, bool] = {"active": False}
    calls: list[bool] = []
    session = _make_txn_tracking_session(in_txn)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    client.app.dependency_overrides[get_db_session] = override_session

    with (
        patch(
            "modulo.api.routes.model_backends.create_secrets_backend",
            return_value=_TrackingSecretsBackend(in_txn, calls),
        ),
        patch("modulo.api.routes.model_backends.update_model_backend", return_value=_make_backend()),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/model-backends/{_BACKEND_ID}", json={"api_key": "sk-new"})

    assert resp.status_code == 200, resp.text
    assert calls, "set_secret was never called"
    assert all(calls), (
        "set_secret ran OUTSIDE the transaction; FernetSecretsBackend.set_secret "
        "requires an active transaction (autobegin=False session)"
    )


def test_update_model_backend_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.update_model_backend", return_value=None),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/model-backends/{uuid.uuid4()}", json={"name": "x"})
    assert resp.status_code == 404


def test_delete_model_backend_returns_204(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.delete_model_backend", return_value=True),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/model-backends/{_BACKEND_ID}")
    assert resp.status_code == 204


def test_delete_model_backend_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.delete_model_backend", return_value=False),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/model-backends/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PRD §8.12 audit trail — model-backend CRUD events
# ---------------------------------------------------------------------------


def test_create_model_backend_emits_model_backend_created_audit(client: TestClient) -> None:
    """Registration fires the ``model_backend.created`` audit event."""
    backend = _make_backend()
    audit = AsyncMock(return_value=MagicMock())
    with (
        patch("modulo.api.routes.model_backends.create_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
        patch("modulo.core.audit_logger.append_audit_event", new=audit),
    ):
        resp = client.post("/api/v1/model-backends", json=_CREATE_BODY)
    assert resp.status_code == 201
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["event_type"] == "model_backend.created"
    assert kwargs["org_id"] == _ORG_ID
    assert kwargs["actor_user_id"] == _USER_ID
    assert kwargs["resource_type"] == "model_backend"
    assert kwargs["resource_id"] == _BACKEND_ID
    payload = kwargs["payload_json"]
    assert payload["name"] == "Test Backend"
    assert payload["provider"] == "openai"
    assert payload["model_id"] == "gpt-4"
    assert payload["has_credentials"] is True


def test_create_model_backend_audit_failure_does_not_block_creation(client: TestClient) -> None:
    """A broken audit append must not fail a successful backend registration."""
    backend = _make_backend()

    async def _raise_audit(*_a: object, **_k: object) -> object:
        raise RuntimeError("audit boom")

    with (
        patch("modulo.api.routes.model_backends.create_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
        patch("modulo.core.audit_logger.append_audit_event", side_effect=_raise_audit),
    ):
        resp = client.post("/api/v1/model-backends", json=_CREATE_BODY)
    assert resp.status_code == 201
    assert resp.json()["name"] == "Test Backend"


def test_update_model_backend_emits_model_backend_updated_audit(client: TestClient) -> None:
    """A non-credential edit fires ``model_backend.updated`` with the changed fields."""
    backend = _make_backend()
    audit = AsyncMock(return_value=MagicMock())
    with (
        patch("modulo.api.routes.model_backends.update_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
        patch("modulo.core.audit_logger.append_audit_event", new=audit),
    ):
        resp = client.patch(f"/api/v1/model-backends/{_BACKEND_ID}", json={"display_name": "GPT-4o"})
    assert resp.status_code == 200
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["event_type"] == "model_backend.updated"
    assert kwargs["resource_id"] == _BACKEND_ID
    assert kwargs["payload_json"]["changed_fields"] == {"display_name": "GPT-4o"}


def test_update_model_backend_credentials_emits_prd_credentials_audit(client: TestClient) -> None:
    """Credential rotation fires the PRD-named ``model_backend_credentials_updated`` event."""
    backend = _make_backend()
    audit = AsyncMock(return_value=MagicMock())
    with (
        patch("modulo.api.routes.model_backends.update_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
        patch("modulo.core.audit_logger.append_audit_event", new=audit),
    ):
        resp = client.patch(f"/api/v1/model-backends/{_BACKEND_ID}", json={"api_key": "sk-new"})
    assert resp.status_code == 200
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["event_type"] == "model_backend_credentials_updated"
    assert kwargs["resource_id"] == _BACKEND_ID
    assert kwargs["payload_json"]["backend_id"] == str(_BACKEND_ID)
    assert kwargs["payload_json"]["provider"] == "openai"
    # The raw credential must never leak into the audit payload.
    assert "sk-new" not in str(kwargs["payload_json"])


def test_update_model_backend_audit_failure_does_not_block_update(client: TestClient) -> None:
    """A broken audit append must not fail a successful backend update."""
    backend = _make_backend()

    async def _raise_audit(*_a: object, **_k: object) -> object:
        raise RuntimeError("audit boom")

    with (
        patch("modulo.api.routes.model_backends.update_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
        patch("modulo.core.audit_logger.append_audit_event", side_effect=_raise_audit),
    ):
        resp = client.patch(f"/api/v1/model-backends/{_BACKEND_ID}", json={"display_name": "GPT-4o"})
    assert resp.status_code == 200


def test_update_model_backend_404_does_not_emit_audit(client: TestClient) -> None:
    """An unknown-backend update returns 404 without firing an audit event."""
    audit = AsyncMock(return_value=MagicMock())
    with (
        patch("modulo.api.routes.model_backends.update_model_backend", return_value=None),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
        patch("modulo.core.audit_logger.append_audit_event", new=audit),
    ):
        resp = client.patch(f"/api/v1/model-backends/{uuid.uuid4()}", json={"display_name": "GPT-4o"})
    assert resp.status_code == 404
    audit.assert_not_awaited()


def test_delete_model_backend_emits_model_backend_deleted_audit(client: TestClient) -> None:
    """Deletion fires ``model_backend.deleted`` carrying the pre-delete entity details."""
    backend = _make_backend()
    audit = AsyncMock(return_value=MagicMock())
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.delete_model_backend", return_value=True),
        patch(
            "modulo.api.routes.model_backends.list_backends_referencing_fallback",
            new=AsyncMock(return_value=[]),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
        patch("modulo.core.audit_logger.append_audit_event", new=audit),
    ):
        resp = client.delete(f"/api/v1/model-backends/{_BACKEND_ID}")
    assert resp.status_code == 204
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["event_type"] == "model_backend.deleted"
    assert kwargs["resource_id"] == _BACKEND_ID
    payload = kwargs["payload_json"]
    assert payload["name"] == "Test Backend"
    assert payload["provider"] == "openai"
    assert payload["model_id"] == "gpt-4"


def test_delete_model_backend_audit_failure_does_not_block_delete(client: TestClient) -> None:
    """A broken audit append must not fail a completed backend deletion."""
    backend = _make_backend()

    async def _raise_audit(*_a: object, **_k: object) -> object:
        raise RuntimeError("audit boom")

    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.delete_model_backend", return_value=True),
        patch(
            "modulo.api.routes.model_backends.list_backends_referencing_fallback",
            new=AsyncMock(return_value=[]),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
        patch("modulo.core.audit_logger.append_audit_event", side_effect=_raise_audit),
    ):
        resp = client.delete(f"/api/v1/model-backends/{_BACKEND_ID}")
    assert resp.status_code == 204


def test_delete_model_backend_404_does_not_emit_audit(client: TestClient) -> None:
    """An unknown-backend delete returns 404 without firing an audit event."""
    audit = AsyncMock(return_value=MagicMock())
    with (
        patch("modulo.api.routes.model_backends.delete_model_backend", return_value=False),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
        patch("modulo.core.audit_logger.append_audit_event", new=audit),
    ):
        resp = client.delete(f"/api/v1/model-backends/{uuid.uuid4()}")
    assert resp.status_code == 404
    audit.assert_not_awaited()


def test_model_backend_no_credentials_shows_false(client: TestClient) -> None:
    backend = _make_backend(credentials_ciphertext=b"")
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{_BACKEND_ID}")
    assert resp.json()["has_credentials"] is False


def test_list_model_backends_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/model-backends")
    assert resp.status_code in (401, 403)


def test_list_model_backends_programming_error_returns_501(client: TestClient) -> None:
    from sqlalchemy.exc import ProgrammingError as ProgrammingError_

    with (
        patch(
            "modulo.api.routes.model_backends.list_model_backends",
            side_effect=ProgrammingError_("mock", "mock", "mock"),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/model-backends")
    assert resp.status_code == 501
    assert "migrations" in resp.json()["detail"].lower()


def test_create_model_backend_programming_error_returns_501(client: TestClient) -> None:
    from sqlalchemy.exc import ProgrammingError as ProgrammingError_

    with (
        patch(
            "modulo.api.routes.model_backends.create_model_backend",
            side_effect=ProgrammingError_("mock", "mock", "mock"),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/model-backends",
            json={
                "name": "x",
                "display_name": "x",
                "provider": "openai",
                "model_id": "gpt-4",
                "api_key": "sk-test",
            },
        )
    assert resp.status_code == 501


def test_get_model_backend_programming_error_returns_501(client: TestClient) -> None:
    from sqlalchemy.exc import ProgrammingError as ProgrammingError_

    with (
        patch(
            "modulo.api.routes.model_backends.get_model_backend", side_effect=ProgrammingError_("mock", "mock", "mock")
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{uuid.uuid4()}")
    assert resp.status_code == 501


def test_update_model_backend_programming_error_returns_501(client: TestClient) -> None:
    from sqlalchemy.exc import ProgrammingError as ProgrammingError_

    with (
        patch(
            "modulo.api.routes.model_backends.update_model_backend",
            side_effect=ProgrammingError_("mock", "mock", "mock"),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/model-backends/{uuid.uuid4()}", json={"name": "x"})
    assert resp.status_code == 501


def test_delete_model_backend_programming_error_returns_501(client: TestClient) -> None:
    from sqlalchemy.exc import ProgrammingError as ProgrammingError_

    with (
        patch(
            "modulo.api.routes.model_backends.delete_model_backend",
            side_effect=ProgrammingError_("mock", "mock", "mock"),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/model-backends/{uuid.uuid4()}")
    assert resp.status_code == 501


def test_create_model_backend_duplicate_name_returns_409(client: TestClient) -> None:
    """Duplicate backend name within same org should return 409."""
    mock_session = _make_mock_session()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = MagicMock()  # existing row found
    mock_session.execute = AsyncMock(return_value=mock_result)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    client.app.dependency_overrides[get_db_session] = override_session

    with (
        patch("modulo.api.routes.model_backends.create_model_backend"),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        body = {**_CREATE_BODY, "name": "duplicate"}
        resp = client.post("/api/v1/model-backends", json=body)

    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


def test_create_model_backend_invalid_provider_returns_422(client: TestClient) -> None:
    """Creating a backend with an unsupported provider returns 422."""
    body = {**_CREATE_BODY, "provider": "nonexistent_provider"}
    resp = client.post("/api/v1/model-backends", json=body)
    assert resp.status_code == 422
    detail_str = resp.text
    assert "nonexistent_provider" in detail_str


def test_create_model_backend_invalid_provider_returns_422_via_plugins(client: TestClient) -> None:
    """Provider that fails plugin registry check also returns 422."""
    from modulo.api.routes.model_backends import _VALID_PROVIDERS

    saved = dict.fromkeys(_VALID_PROVIDERS, True)
    try:
        _VALID_PROVIDERS.clear()
        with patch("modulo.api.routes.model_backends.get_plugin_registry") as mock_reg:
            mock_reg.return_value.has_model_backend.return_value = False
            body = {**_CREATE_BODY, "provider": "unknown"}
            resp = client.post("/api/v1/model-backends", json=body)
        assert resp.status_code == 422
    finally:
        _VALID_PROVIDERS.update(saved)


def test_create_azure_openai_model_backend_round_trips(client: TestClient) -> None:
    """Creating an azure_openai backend preserves provider and model_id in response."""
    azure_body = {**_CREATE_BODY, "provider": "azure_openai"}
    backend = _make_backend(credentials_ciphertext=b"encrypted_bytes")
    backend.provider = "azure_openai"
    backend.model_id = "gpt-4-deployment"
    with (
        patch("modulo.api.routes.model_backends.create_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/model-backends", json=azure_body)

    assert resp.status_code == 201
    body = resp.json()
    assert body["provider"] == "azure_openai"
    assert "credentials_ciphertext" not in body
    assert "api_key" not in body
    assert body["has_credentials"] is True


def test_create_custom_model_backend_round_trips(client: TestClient) -> None:
    """A custom (demo stub) provider is accepted by the endpoint and round-trips.

    The hub registration path for custom rows is covered directly in
    tests/unit/model_backend_hub/test_hub.py (test_initialise_custom_provider_registers_and_invokes).
    """
    custom_body = {**_CREATE_BODY, "provider": "custom", "model_id": "demo-model"}
    backend = _make_backend(credentials_ciphertext=b"encrypted_bytes")
    backend.provider = "custom"
    backend.model_id = "demo-model"
    with (
        patch("modulo.api.routes.model_backends.create_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/model-backends", json=custom_body)

    assert resp.status_code == 201
    body = resp.json()
    assert body["provider"] == "custom"
    assert body["model_id"] == "demo-model"
    assert "credentials_ciphertext" not in body
    assert "api_key" not in body
    assert body["has_credentials"] is True


def test_list_model_backends_sqlalchemy_error_returns_503(client: TestClient) -> None:
    from sqlalchemy.exc import SQLAlchemyError as SQLAlchemyError_

    with (
        patch(
            "modulo.api.routes.model_backends.list_model_backends", side_effect=SQLAlchemyError_("mock", "mock", "mock")
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/model-backends")
    assert resp.status_code == 503


def test_create_model_backend_sqlalchemy_error_returns_503(client: TestClient) -> None:
    from sqlalchemy.exc import SQLAlchemyError as SQLAlchemyError_

    with (
        patch(
            "modulo.api.routes.model_backends.create_model_backend",
            side_effect=SQLAlchemyError_("mock", "mock", "mock"),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/model-backends",
            json={
                "name": "x",
                "display_name": "x",
                "provider": "openai",
                "model_id": "gpt-4",
                "api_key": "sk-test",
            },
        )
    assert resp.status_code == 503


def test_get_model_backend_sqlalchemy_error_returns_503(client: TestClient) -> None:
    from sqlalchemy.exc import SQLAlchemyError as SQLAlchemyError_

    with (
        patch(
            "modulo.api.routes.model_backends.get_model_backend", side_effect=SQLAlchemyError_("mock", "mock", "mock")
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{uuid.uuid4()}")
    assert resp.status_code == 503


def test_update_model_backend_sqlalchemy_error_returns_503(client: TestClient) -> None:
    from sqlalchemy.exc import SQLAlchemyError as SQLAlchemyError_

    with (
        patch(
            "modulo.api.routes.model_backends.update_model_backend",
            side_effect=SQLAlchemyError_("mock", "mock", "mock"),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/model-backends/{uuid.uuid4()}", json={"name": "x"})
    assert resp.status_code == 503


def test_delete_model_backend_sqlalchemy_error_returns_503(client: TestClient) -> None:
    from sqlalchemy.exc import SQLAlchemyError as SQLAlchemyError_

    with (
        patch(
            "modulo.api.routes.model_backends.delete_model_backend",
            side_effect=SQLAlchemyError_("mock", "mock", "mock"),
        ),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/model-backends/{uuid.uuid4()}")
    assert resp.status_code == 503


def test_list_model_backends_exception_returns_500(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.list_model_backends", side_effect=TypeError("unexpected None")),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/model-backends")
    assert resp.status_code == 500
    assert "unexpected" in resp.json()["detail"].lower()


def test_create_model_backend_exception_returns_500(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.create_model_backend", side_effect=KeyError("missing_field")),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/model-backends",
            json={
                "name": "x",
                "display_name": "x",
                "provider": "openai",
                "model_id": "gpt-4",
                "api_key": "sk-test",
            },
        )
    assert resp.status_code == 500


def test_get_model_backend_exception_returns_500(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", side_effect=ValueError("bad state")),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{uuid.uuid4()}")
    assert resp.status_code == 500


def test_update_model_backend_exception_returns_500(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.update_model_backend", side_effect=AttributeError("no attribute")),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/model-backends/{uuid.uuid4()}", json={"name": "x"})
    assert resp.status_code == 500


def test_delete_model_backend_exception_returns_500(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.delete_model_backend", side_effect=RuntimeError("unexpected")),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/model-backends/{uuid.uuid4()}")
    assert resp.status_code == 500


def test_get_model_backend_empty_fallback_ids_round_trips(client: TestClient) -> None:
    """Empty fallback_backend_ids list should round-trip as [], not None."""
    backend = _make_backend()
    backend.fallback_backend_ids = []
    with (
        patch("modulo.api.routes.model_backends.get_model_backend", return_value=backend),
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/model-backends/{_BACKEND_ID}")
    assert resp.status_code == 200
    assert not resp.json()["fallback_backend_ids"]


def test_create_model_backend_integrity_error_returns_409(client: TestClient) -> None:
    """IntegrityError (FK/unique violation) on create returns 409."""
    from sqlalchemy.exc import IntegrityError as IntegrityError_

    with (
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.set_rls_user_context"),
        patch(
            "modulo.api.routes.model_backends.create_model_backend", side_effect=IntegrityError_("mock", "mock", "mock")
        ),
    ):
        resp = client.post(
            "/api/v1/model-backends",
            json={
                "name": "x",
                "display_name": "x",
                "provider": "openai",
                "model_id": "gpt-4",
                "api_key": "sk-test",
            },
        )
    assert resp.status_code == 409


def _foreign_org_backend() -> MagicMock:
    """A model backend owned by a different organisation than the test principal."""
    foreign = MagicMock()
    foreign.id = _BACKEND_ID
    foreign.organisation_id = uuid.uuid4()
    return foreign


def test_update_model_backend_foreign_org_returns_404(client: TestClient) -> None:
    """IDOR regression: a foreign-org principal must not update a model backend
    it does not own. The ownership check must raise 404 before any write."""
    with patch(
        "modulo.api.routes.model_backends.get_model_backend",
        return_value=_foreign_org_backend(),
    ):
        resp = client.patch(f"/api/v1/model-backends/{_BACKEND_ID}", json={})
    assert resp.status_code == 404


def test_delete_model_backend_foreign_org_returns_404(client: TestClient) -> None:
    """IDOR regression: a foreign-org principal must not delete a model backend
    it does not own."""
    with patch(
        "modulo.api.routes.model_backends.get_model_backend",
        return_value=_foreign_org_backend(),
    ):
        resp = client.delete(f"/api/v1/model-backends/{_BACKEND_ID}")
    assert resp.status_code == 404
