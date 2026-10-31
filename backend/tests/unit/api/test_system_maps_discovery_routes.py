"""Endpoint contract tests for internal System Maps discovery."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context, get_settings
from modulo.api.main import app
from modulo.api.routes.system_maps import router
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.lifecycle_map.validation import LifecycleMapContentError, LifecycleMapPipelineConflictError
from modulo.core.system_map_persistence import SystemMapNameConflictError, SystemMapRepository
from modulo.core.system_maps_discovery import DiscoveryDraft
from modulo.settings import Settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_MAP_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_REVISION_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")


class _Plan:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def feature_enabled(self, name: str) -> bool:
        return name == "system_maps" and self.enabled

    def list_enabled_features(self) -> list[str]:
        return ["system_maps"] if self.enabled else []

    def tier(self) -> str:
        return "enterprise"

    def has_license_key(self) -> bool:
        return True


class _DraftMap:
    id = _MAP_ID
    version = 2


def _make_settings(*, dev_mode: bool = True) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_dev_mode=dev_mode,
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


@pytest.fixture
def route_client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    principal = TenantPrincipal(
        username="internal",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
        key_grants=frozenset({"system_maps_access"}),
    )
    auth_user = AuthenticatedPrincipal(
        username="internal",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
        key_grants=frozenset({"system_maps_access"}),
    )

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = lambda: _Plan(True)
    app.dependency_overrides[get_current_user] = lambda: auth_user
    app.dependency_overrides[get_current_tenant_user] = lambda: principal
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: principal
    yield TestClient(app), mock_session
    app.dependency_overrides.clear()


def test_discovery_route_is_registered() -> None:
    routes = {route.path for route in router.routes}
    assert "/api/v1/system-maps/discover" in routes


def test_discovery_denies_when_feature_is_disabled() -> None:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    principal = TenantPrincipal(
        username="internal",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
        key_grants=frozenset({"system_maps_access"}),
    )
    auth_user = AuthenticatedPrincipal(
        username="internal",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
        key_grants=frozenset({"system_maps_access"}),
    )

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = lambda: _Plan(False)
    app.dependency_overrides[get_current_user] = lambda: auth_user
    app.dependency_overrides[get_current_tenant_user] = lambda: principal
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: principal

    try:
        response = TestClient(app).post("/api/v1/system-maps/discover", json={})
        assert response.status_code == 402
    finally:
        app.dependency_overrides.clear()


def test_discovery_denies_without_system_maps_grant() -> None:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    principal = TenantPrincipal(
        username="operator_without_grant",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
        key_grants=frozenset({"pipeline.list"}),
    )
    auth_user = AuthenticatedPrincipal(
        username="operator_without_grant",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
        key_grants=frozenset({"pipeline.list"}),
    )

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = lambda: _Plan(True)
    app.dependency_overrides[get_current_user] = lambda: auth_user
    app.dependency_overrides[get_current_tenant_user] = lambda: principal
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: principal

    try:
        response = TestClient(app).post("/api/v1/system-maps/discover", json={})
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_discovery_denies_without_dev_mode() -> None:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    principal = TenantPrincipal(
        username="internal",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
        key_grants=frozenset({"system_maps_access"}),
    )
    auth_user = AuthenticatedPrincipal(
        username="internal",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
        key_grants=frozenset({"system_maps_access"}),
    )

    app.dependency_overrides[get_settings] = lambda: _make_settings(dev_mode=False)
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = lambda: _Plan(True)
    app.dependency_overrides[get_current_user] = lambda: auth_user
    app.dependency_overrides[get_current_tenant_user] = lambda: principal
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: principal

    try:
        response = TestClient(app).post("/api/v1/system-maps/discover", json={})
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(("org_role", "expected_status"), [("viewer", 403), ("operator", 201)])
def test_discovery_operator_role_boundary(org_role: str, expected_status: int) -> None:
    """The system_maps_access grant resolves through the org-role hierarchy.

    A viewer holding the grant is denied (the permission floor is operator);
    an operator holding it reaches the handler and persists a draft.
    """
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    principal = TenantPrincipal(
        username=f"{org_role}_user",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
        key_grants=frozenset({"system_maps_access"}),
    )
    auth_user = AuthenticatedPrincipal(
        username=f"{org_role}_user",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
        key_grants=frozenset({"system_maps_access"}),
    )

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = lambda: _Plan(True)
    app.dependency_overrides[get_current_user] = lambda: auth_user
    app.dependency_overrides[get_current_tenant_user] = lambda: principal
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: principal

    if expected_status == 201:
        authz_result = MagicMock(scalar_one_or_none=MagicMock(return_value=True))
        mock_session.execute.side_effect = [
            authz_result,
            MagicMock(scalars=MagicMock(return_value=[])),
            MagicMock(scalars=MagicMock(return_value=[])),
            MagicMock(scalars=MagicMock(return_value=[])),
            MagicMock(scalars=MagicMock(return_value=[])),
        ]

    draft_map = _DraftMap()
    try:
        with (
            patch.object(SystemMapRepository, "get_or_create_draft", new=AsyncMock(return_value=draft_map)),
            patch.object(
                SystemMapRepository,
                "save_discovery_revision",
                new=AsyncMock(return_value=(draft_map, _REVISION_ID)),
            ),
        ):
            response = TestClient(app).post("/api/v1/system-maps/discover", json={"name": "boundary"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == expected_status


@pytest.mark.parametrize(
    ("raised", "expected_status"),
    [
        (LifecycleMapContentError("invalid graph"), 422),
        (LifecycleMapPipelineConflictError("pipeline already mapped"), 409),
        (SystemMapNameConflictError("name taken"), 409),
        (IntegrityError("INSERT", None, Exception("duplicate key")), 409),
        (ProgrammingError("SELECT", {}, Exception("missing relation")), 501),
        (SQLAlchemyError("database down"), 503),
    ],
)
def test_discovery_maps_persistence_errors_to_http_status(
    raised: Exception,
    expected_status: int,
    route_client: tuple[TestClient, AsyncMock],
) -> None:
    client, _session = route_client
    with patch.object(SystemMapRepository, "get_or_create_draft", new=AsyncMock(side_effect=raised)):
        response = client.post("/api/v1/system-maps/discover", json={"name": "errors"})

    assert response.status_code == expected_status


def test_discovery_maps_normalisation_failure_is_422(
    route_client: tuple[TestClient, AsyncMock],
) -> None:
    """A content-bound refusal from ``normalize_content`` must be a 422.

    Normalisation runs over a draft assembled from org inventory (e.g. a
    source name longer than the lifecycle-map bound), so it is part of the
    persistence path, not an unhandled server bug.
    """
    client, _session = route_client
    with patch(
        "modulo.api.routes.system_maps.normalize_content",
        side_effect=LifecycleMapContentError("stage name too long"),
    ):
        response = client.post("/api/v1/system-maps/discover", json={"name": "invalid"})

    assert response.status_code == 422
    assert "stage name too long" in response.json()["detail"]


def test_discovery_persists_deterministic_report_and_suggested_edges(
    route_client: tuple[TestClient, AsyncMock],
) -> None:
    client, session = route_client
    trigger = SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-000000000010"),
        name="webhook",
        pipeline_id=uuid.UUID("00000000-0000-0000-0000-000000000011"),
        trigger_type="webhook",
        organisation_id=_ORG_ID,
        deleted_at=None,
    )
    pipeline = SimpleNamespace(
        id=trigger.pipeline_id,
        name="build",
        folder_id=None,
        run_context_defaults={},
        account_id=_USER_ID,
        organisation_id=_ORG_ID,
        deleted_at=None,
        archived_at=None,
    )
    folder = SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-000000000020"),
        name="Build Folder",
        organisation_id=_ORG_ID,
    )
    connector = SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-000000000030"),
        name="github",
        connector_type_id="github",
        organisation_id=_ORG_ID,
        deleted_at=None,
    )

    authz_result = MagicMock(scalar_one_or_none=MagicMock(return_value=True))
    session.execute.side_effect = [
        authz_result,
        MagicMock(scalars=MagicMock(return_value=[pipeline])),
        MagicMock(scalars=MagicMock(return_value=[trigger])),
        MagicMock(scalars=MagicMock(return_value=[connector])),
        MagicMock(scalars=MagicMock(return_value=[folder])),
    ]

    draft_map = _DraftMap()
    with (
        patch.object(SystemMapRepository, "get_or_create_draft", new=AsyncMock(return_value=draft_map)),
        patch.object(
            SystemMapRepository,
            "save_discovery_revision",
            new=AsyncMock(return_value=(draft_map, _REVISION_ID)),
        ) as save_revision,
    ):
        first = client.post("/api/v1/system-maps/discover", json={"name": "draft"})
        session.execute.side_effect = [
            authz_result,
            MagicMock(scalars=MagicMock(return_value=[pipeline])),
            MagicMock(scalars=MagicMock(return_value=[trigger])),
            MagicMock(scalars=MagicMock(return_value=[connector])),
            MagicMock(scalars=MagicMock(return_value=[folder])),
        ]
        second = client.post("/api/v1/system-maps/discover", json={"name": "draft"})

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert first.json()["edge_count"] == 1
    assert first.json()["inferred_edges"][0]["config_json"]["suggested"] is True
    assert save_revision.await_count == 2
    persisted = save_revision.await_args_list[0].kwargs["content"]
    assert persisted["edges"][0]["config_json"]["suggested"] is True
    stage_types = {stage["name"]: stage["type"] for stage in persisted["stages"]}
    assert stage_types == {
        "build": "modulo",
        "webhook": "manual",
        "github": "placeholder",
        "Build Folder": "placeholder",
    }


def test_discovery_tenant_scoping_queries_filter_and_set_rls(
    route_client: tuple[TestClient, AsyncMock],
) -> None:
    client, session = route_client
    authz_result = MagicMock(scalar_one_or_none=MagicMock(return_value=True))
    session.execute.side_effect = [
        authz_result,
        MagicMock(scalars=MagicMock(return_value=[])),
        MagicMock(scalars=MagicMock(return_value=[])),
        MagicMock(scalars=MagicMock(return_value=[])),
        MagicMock(scalars=MagicMock(return_value=[])),
    ]

    draft_map = _DraftMap()
    with (
        patch.object(SystemMapRepository, "get_or_create_draft", new=AsyncMock(return_value=draft_map)),
        patch.object(
            SystemMapRepository,
            "save_discovery_revision",
            new=AsyncMock(return_value=(draft_map, _REVISION_ID)),
        ),
    ):
        response = client.post("/api/v1/system-maps/discover", json={"name": "scoped"})

    assert response.status_code == 201
    # The RLS prelude must have BOUND the tenant identity, not merely issued
    # queries: deleting the set_rls_org/set_rls_user_context calls previously
    # still passed a count-only assertion. The mock session runs the generic
    # (non-Postgres) branch, which records the binding on ``session.info``.
    assert session.info["org_id"] == _ORG_ID
    assert session.info["user_id"] == _USER_ID
    assert session.info["org_role"] == "operator"
    executed_statements = [call.args[0] for call in session.execute.await_args_list]
    selects = [statement for statement in executed_statements if isinstance(statement, Select)]
    inventory_tables = ("FROM pipelines", "FROM triggers", "FROM connector_instances", "FROM pipeline_folders")
    inventory_selects = [s for s in selects if any(table in str(s) for table in inventory_tables)]
    assert len(inventory_selects) == 4
    assert all("organisation_id" in str(statement) for statement in inventory_selects)


def test_discovery_route_fingerprint_is_stable() -> None:
    from modulo.api.routes.system_maps import _fingerprint

    draft = DiscoveryDraft(nodes=(), relationships=(), warnings=(), skipped=())
    assert _fingerprint(draft) == _fingerprint(draft)
