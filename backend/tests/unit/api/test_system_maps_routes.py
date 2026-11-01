"""Unit tests for System Maps REST API (FAR-2236).

Tests the internal preview surface gated by the `system_maps` feature flag,
dev mode, operator/admin role, and the `system_maps_access` grant.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Self
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.main import app
from modulo.auth.jwt import TenantPrincipal
from modulo.db.repositories.system_map import SystemMapRepository

# ─── Test constants ───

_ORG_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_MAP_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_REV_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_TEAM_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
_OTHER_TEAM_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")

_BASE = "/api/v1/system-maps"


# ─── Test helpers ───


def _map_row(
    *,
    map_id: uuid.UUID = _MAP_ID,
    org_id: uuid.UUID = _ORG_ID,
    name: str = "Test System Map",
    description: str | None = "A test system map",
    owner_team_id: uuid.UUID | None = _TEAM_ID,
    visibility: str = "org",
    default_map: bool = False,
    published_revision_id: uuid.UUID | None = _REV_ID,
) -> MagicMock:
    row = MagicMock()
    row.id = map_id
    row.organisation_id = org_id
    row.name = name
    row.description = description
    row.owner_team_id = owner_team_id
    row.visibility = visibility
    row.default_map = default_map
    row.published_revision_id = published_revision_id
    row.account_id = _USER_ID
    row.updated_by = _USER_ID
    row.deleted_at = None
    row.deleted_by = None
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    return row


def _revision_row(
    *,
    rev_id: uuid.UUID = _REV_ID,
    map_id: uuid.UUID = _MAP_ID,
    org_id: uuid.UUID = _ORG_ID,
    revision_number: int = 1,
    status: str = "draft",
) -> MagicMock:
    row = MagicMock()
    row.id = rev_id
    row.system_map_id = map_id
    row.organisation_id = org_id
    row.revision_number = revision_number
    row.status = status
    row.content_json = {}
    row.published_at = None
    row.deleted_at = None
    row.deleted_by = None
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    return row


def _node_row(
    *,
    node_id: uuid.UUID = uuid.uuid4(),
    rev_id: uuid.UUID = _REV_ID,
    org_id: uuid.UUID = _ORG_ID,
    node_key: str = "node-1",
    node_type: str = "pipeline",
    display_name: str = "Node 1",
) -> MagicMock:
    row = MagicMock()
    row.id = node_id
    row.revision_id = rev_id
    row.organisation_id = org_id
    row.node_key = node_key
    row.node_type = node_type
    row.display_name = display_name
    row.config_json = {}
    row.pipeline_id = None
    row.connector_instance_id = None
    row.owner_team_id = None
    row.eval_id = None
    row.position_x = Decimal(0)
    row.position_y = Decimal(0)
    row.deleted_at = None
    row.deleted_by = None
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    return row


def _relationship_row(
    *,
    rel_id: uuid.UUID = uuid.uuid4(),
    rev_id: uuid.UUID = _REV_ID,
    org_id: uuid.UUID = _ORG_ID,
    source_node_key: str = "node-1",
    target_node_key: str = "node-2",
    relationship_type: str = "flow",
) -> MagicMock:
    row = MagicMock()
    row.id = rel_id
    row.revision_id = rev_id
    row.organisation_id = org_id
    row.source_node_key = source_node_key
    row.target_node_key = target_node_key
    row.relationship_type = relationship_type
    row.config_json = {}
    row.deleted_at = None
    row.deleted_by = None
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    return row


def _loop_config_row(
    *,
    lc_id: uuid.UUID = uuid.uuid4(),
    rev_id: uuid.UUID = _REV_ID,
    org_id: uuid.UUID = _ORG_ID,
    node_key: str = "loop-1",
    target_signal: str = "error_rate",
    observation_window_seconds: int = 300,
    action_pipeline_id: uuid.UUID = uuid.uuid4(),
    budget_limit_usd: Decimal = Decimal("10.00"),
    cooldown_seconds: int = 3600,
    outcome_metric: str = "success_rate",
) -> MagicMock:
    row = MagicMock()
    row.id = lc_id
    row.revision_id = rev_id
    row.organisation_id = org_id
    row.node_key = node_key
    row.target_signal = target_signal
    row.observation_window_seconds = observation_window_seconds
    row.action_pipeline_id = action_pipeline_id
    row.budget_limit_usd = budget_limit_usd
    row.cooldown_seconds = cooldown_seconds
    row.outcome_metric = outcome_metric
    row.deleted_at = None
    row.deleted_by = None
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    return row


def _binding_row(
    *,
    binding_id: uuid.UUID = uuid.uuid4(),
    map_id: uuid.UUID = _MAP_ID,
    org_id: uuid.UUID = _ORG_ID,
    pipeline_id: uuid.UUID = uuid.uuid4(),
    trigger_id: uuid.UUID | None = None,
    node_key: str = "node-1",
    binding_kind: str = "trigger",
) -> MagicMock:
    row = MagicMock()
    row.id = binding_id
    row.organisation_id = org_id
    row.pipeline_id = pipeline_id
    row.trigger_id = trigger_id
    row.system_map_id = map_id
    row.node_key = node_key
    row.binding_kind = binding_kind
    row.deleted_at = None
    row.deleted_by = None
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    return row


class _Harness:
    """Test harness that supplies a repository mock to the route dependency."""

    def __init__(self) -> None:
        self.repository = MagicMock(spec=SystemMapRepository)
        self._stubs: dict[str, AsyncMock] = {}

    def stub(self, method: str, mock: AsyncMock) -> None:
        self._stubs[method] = mock
        setattr(self.repository, method, mock)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self._stubs.clear()


def _install_overrides(
    harness: _Harness,
    *,
    org_role: str = "operator",
    dev_mode: bool = True,
) -> None:
    """Install FastAPI dependency overrides for testing."""
    from modulo.api.dependencies import get_current_tenant_user, get_db_session, get_settings
    from modulo.auth.dependencies import get_current_tenant_user_or_api_key
    from modulo.settings import Settings

    principal = TenantPrincipal(
        username="test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
        is_system_admin=False,
        via_api_key=False,
        client_kind="test",
    )

    session = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    nested_cm = MagicMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one.return_value = 1
    execute_result.scalar_one_or_none.return_value = MagicMock(
        id=uuid.uuid4(), organisation_id=_ORG_ID, deleted_at=None
    )
    session.execute = AsyncMock(return_value=execute_result)
    session.info = {}
    session.add = MagicMock()
    bind = MagicMock()
    bind.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind)

    async def _get_session():
        yield session

    settings = Settings(
        modulo_dev_mode=dev_mode,
        modulo_public_url="http://test",
        fernet_key="test" * 8,
        secret_key="test" * 8,
        redis_url="redis://localhost:6379/0",
        database_url="postgresql+asyncpg://test:test@localhost/test",
    )

    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda request=None: principal
    app.dependency_overrides[get_current_tenant_user] = lambda: principal
    app.dependency_overrides[get_db_session] = _get_session
    app.dependency_overrides[get_settings] = lambda: settings
    from modulo.api.routes.system_maps import get_repo

    app.dependency_overrides[get_repo] = lambda: harness.repository
    # Override the internal system maps access gate
    from modulo.api.routes.system_maps import _require_internal_system_maps_access

    async def _mock_require_internal_system_maps_access(
        principal: TenantPrincipal = Depends(get_current_tenant_user_or_api_key),
        settings: Settings = Depends(get_settings),
    ) -> TenantPrincipal:
        return principal

    app.dependency_overrides[_require_internal_system_maps_access] = _mock_require_internal_system_maps_access
    from modulo.api.routes.system_maps import _require_system_maps_feature

    async def _mock_system_maps_feature() -> None:
        return None

    app.dependency_overrides[_require_system_maps_feature.dependency] = _mock_system_maps_feature

    # Override the tagged permission dependencies generated at import time.
    for route in app.routes:
        if getattr(route, "path", "").startswith(_BASE):
            for dependency in route.dependant.dependencies:
                if getattr(dependency.call, "__name__", "") == "_check":
                    app.dependency_overrides[dependency.call] = lambda: principal


# ─── Pytest fixtures ───


@pytest.fixture
def client() -> tuple[TestClient, _Harness]:
    harness = _Harness()
    _install_overrides(harness)
    with harness:
        yield TestClient(app), harness
    app.dependency_overrides.clear()


# ─── Access control tests ───


def test_ordinary_caller_denied_403() -> None:
    """Ordinary caller (viewer role) is denied 403 by the internal gate."""
    from modulo.api.dependencies import _require_internal_system_maps_access
    from modulo.auth.jwt import TenantPrincipal
    from modulo.settings import Settings

    settings = Settings(
        modulo_dev_mode=True,
        modulo_public_url="http://test",
        fernet_key="test" * 8,
        secret_key="test" * 8,
        redis_url="redis://localhost:6379/0",
        database_url="postgresql+asyncpg://test:test@localhost/test",
    )

    principal = TenantPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="viewer",
        is_system_admin=False,
        via_api_key=False,
        client_kind="test",
    )

    with pytest.raises(HTTPException) as exc_info:
        _require_internal_system_maps_access(principal, settings=settings)

    assert exc_info.value.status_code == 403
    assert "requires 'operator' role" in exc_info.value.detail


def test_internal_caller_without_grant_denied_403() -> None:
    """Grant-bearing operator keys without the dedicated grant are denied 403."""
    from modulo.api.dependencies import _require_internal_system_maps_access
    from modulo.auth.jwt import TenantPrincipal
    from modulo.settings import Settings

    settings = Settings(
        modulo_dev_mode=True,
        modulo_public_url="http://test",
        fernet_key="test" * 8,
        secret_key="test" * 8,
        redis_url="redis://localhost:6379/0",
        database_url="postgresql+asyncpg://test:test@localhost/test",
    )

    principal = TenantPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
        is_system_admin=False,
        via_api_key=True,
        client_kind="test",
        key_grants=frozenset(["pipeline.list"]),
    )

    with pytest.raises(HTTPException) as exc_info:
        _require_internal_system_maps_access(principal, settings=settings)

    assert exc_info.value.status_code == 403
    assert "system_maps_access" in exc_info.value.detail


def test_internal_caller_with_grant_can_access() -> None:
    """Internal caller (operator) with system_maps_access grant can access."""
    from modulo.api.dependencies import _require_internal_system_maps_access
    from modulo.auth.jwt import TenantPrincipal
    from modulo.settings import Settings

    settings = Settings(
        modulo_dev_mode=True,
        modulo_public_url="http:test",
        fernet_key="test" * 8,
        secret_key="test" * 8,
        redis_url="redis://localhost:6379/0",
        database_url="postgresql+asyncpg://test:test@localhost/test",
    )

    principal = TenantPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
        is_system_admin=False,
        via_api_key=True,
        client_kind="test",
        key_grants=frozenset(["system_maps_access"]),
    )

    # Should not raise
    result = _require_internal_system_maps_access(principal, settings=settings)
    assert result is principal


def test_dev_mode_required() -> None:
    """Dev mode is required - without it, even operator gets 403."""
    from modulo.api.dependencies import _require_internal_system_maps_access
    from modulo.auth.jwt import TenantPrincipal
    from modulo.settings import Settings

    settings = Settings(
        modulo_dev_mode=False,  # Dev mode OFF
        modulo_public_url="http://test",
        fernet_key="test" * 8,
        secret_key="test" * 8,
        redis_url="redis://localhost:6379/0",
        database_url="postgresql+asyncpg://test:test@localhost/test",
    )

    principal = TenantPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
        is_system_admin=False,
        via_api_key=False,
        client_kind="test",
    )

    with pytest.raises(HTTPException) as exc_info:
        _require_internal_system_maps_access(principal, settings=settings)

    assert exc_info.value.status_code == 403
    assert "developer mode" in exc_info.value.detail


# ─── SystemMap CRUD tests ───


def test_list_system_maps_returns_paginated(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("list_maps_by_org", AsyncMock(return_value=[_map_row()]))

    resp = http.get(_BASE, params={"page": 1, "page_size": 20})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert body["total"] >= 1
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "Test System Map"


def test_list_system_maps_passes_name_to_repository_before_pagination(
    client: tuple[TestClient, _Harness],
) -> None:
    http, harness = client
    list_maps = AsyncMock(return_value=[_map_row(name="Matching map")])
    harness.stub("list_maps_by_org", list_maps)

    resp = http.get(_BASE, params={"name": "matching"})

    assert resp.status_code == 200, resp.text
    assert list_maps.await_args.kwargs["name"] == "matching"
    assert resp.json()["items"][0]["name"] == "Matching map"


def test_list_system_maps_ignores_include_deleted_and_keeps_live_scope(
    client: tuple[TestClient, _Harness],
) -> None:
    http, harness = client
    list_maps = AsyncMock(return_value=[])
    harness.stub("list_maps_by_org", list_maps)

    resp = http.get(_BASE, params={"include_deleted": "true"})

    assert resp.status_code == 200, resp.text
    assert "include_deleted" not in list_maps.await_args.kwargs


def test_list_system_maps_filters_by_name(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    maps = [_map_row(name="Map A"), _map_row(map_id=uuid.uuid4(), name="Map B")]
    harness.stub("list_maps_by_org", AsyncMock(return_value=maps))

    resp = http.get(_BASE, params={"name": "Map A"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["items"][0]["name"] == "Map A"


def test_list_system_maps_filters_by_owner_team(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("list_maps_by_org", AsyncMock(return_value=[_map_row()]))

    resp = http.get(_BASE, params={"owner_team_id": str(_TEAM_ID)})

    assert resp.status_code == 200, resp.text


def test_create_system_map_returns_201(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("create_map", AsyncMock(return_value=_map_row()))

    resp = http.post(_BASE, json={"name": "New Map", "visibility": "org"})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Test System Map"
    assert body["visibility"] == "org"


def test_create_system_map_runs_owner_team_validation(
    client: tuple[TestClient, _Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, harness = client
    validate_owner_team = AsyncMock(
        side_effect=HTTPException(status_code=404, detail="Team not found in this organisation")
    )
    monkeypatch.setattr("modulo.api.routes.system_maps.validate_owner_team_for_create", validate_owner_team)
    harness.stub("create_map", AsyncMock(return_value=_map_row()))

    response = http.post(_BASE, json={"name": "Map", "owner_team_id": str(_TEAM_ID)})

    assert response.status_code == 404
    validate_owner_team.assert_awaited_once()
    harness.repository.create_map.assert_not_awaited()


def test_create_system_map_rejects_empty_name(client: tuple[TestClient, _Harness]) -> None:
    http, _harness = client

    resp = http.post(_BASE, json={"name": ""})

    assert resp.status_code == 422


def test_create_system_map_rejects_bad_visibility(client: tuple[TestClient, _Harness]) -> None:
    http, _harness = client

    resp = http.post(_BASE, json={"name": "Map", "visibility": "public"})

    assert resp.status_code == 422


def test_create_system_map_value_error_maps_to_409(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("create_map", AsyncMock(side_effect=ValueError("default map exists")))

    resp = http.post(_BASE, json={"name": "Map", "default_map": True})

    assert resp.status_code == 409


def test_create_system_map_integrity_error_maps_to_409(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("create_map", AsyncMock(side_effect=IntegrityError("s", {}, Exception())))

    resp = http.post(_BASE, json={"name": "Map"})

    assert resp.status_code == 409


def test_create_system_map_unexpected_error_maps_to_500(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("create_map", AsyncMock(side_effect=RuntimeError("kaboom")))

    resp = http.post(_BASE, json={"name": "Map"})

    assert resp.status_code == 500


def test_get_system_map_returns_200(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))

    resp = http.get(f"{_BASE}/{_MAP_ID}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(_MAP_ID)
    assert body["name"] == "Test System Map"
    assert body["published_revision_id"] == str(_REV_ID)


def test_get_system_map_missing_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=None))

    resp = http.get(f"{_BASE}/{_MAP_ID}")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "System map not found"


def test_update_system_map_returns_200(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("soft_delete_map", AsyncMock(return_value=True))  # not used but needed for session

    resp = http.put(f"{_BASE}/{_MAP_ID}", json={"name": "Renamed Map"})

    assert resp.status_code == 200, resp.text


def test_update_system_map_missing_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=None))

    resp = http.put(f"{_BASE}/{_MAP_ID}", json={"name": "Renamed"})

    assert resp.status_code == 404


def test_delete_system_map_returns_204(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("soft_delete_map", AsyncMock(return_value=True))

    resp = http.delete(f"{_BASE}/{_MAP_ID}")

    assert resp.status_code == 204, resp.text


def test_delete_system_map_missing_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("soft_delete_map", AsyncMock(return_value=False))

    resp = http.delete(f"{_BASE}/{_MAP_ID}")

    assert resp.status_code == 404


# ─── Revision CRUD tests ───


def test_get_revision_returns_200(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=_revision_row()))

    resp = http.get(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(_REV_ID)
    assert body["revision_number"] == 1
    assert body["status"] == "draft"


def test_system_map_graph_serializes_deleted_by_from_real_orm_objects(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    from modulo.db.models.system_map import (
        SystemMapLoopConfig,
        SystemMapNode,
        SystemMapRelationship,
        SystemMapRevision,
    )

    deleted_by = uuid.uuid4()
    revision = SystemMapRevision(
        id=_REV_ID,
        organisation_id=_ORG_ID,
        system_map_id=_MAP_ID,
        revision_number=1,
        status="draft",
        content_json={},
    )
    revision.deleted_by = deleted_by
    revision.created_at = datetime.now(UTC)
    revision.updated_at = datetime.now(UTC)
    node = SystemMapNode(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        revision_id=_REV_ID,
        node_key="node-1",
        node_type="pipeline",
        display_name="Node 1",
        config_json={},
        position_x=Decimal(0),
        position_y=Decimal(0),
    )
    node.deleted_by = deleted_by
    node.created_at = datetime.now(UTC)
    node.updated_at = datetime.now(UTC)
    relationship = SystemMapRelationship(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        revision_id=_REV_ID,
        source_node_key="node-1",
        target_node_key="node-2",
        relationship_type="flow",
        config_json={},
    )
    relationship.deleted_by = deleted_by
    relationship.created_at = datetime.now(UTC)
    relationship.updated_at = datetime.now(UTC)
    loop_config = SystemMapLoopConfig(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        revision_id=_REV_ID,
        node_key="loop-1",
        target_signal="error_rate",
        observation_window_seconds=300,
        action_pipeline_id=uuid.uuid4(),
        budget_limit_usd=Decimal("10.00"),
        cooldown_seconds=60,
        outcome_metric="success_rate",
    )
    loop_config.deleted_by = deleted_by
    loop_config.created_at = datetime.now(UTC)
    loop_config.updated_at = datetime.now(UTC)
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub(
        "get_revision_by_id",
        AsyncMock(return_value=revision),
    )
    harness.stub(
        "get_full_revision_graph",
        AsyncMock(
            return_value={
                "revision": revision,
                "nodes": [node],
                "relationships": [relationship],
                "loop_configs": [loop_config],
            }
        ),
    )

    response = http.get(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}", params={"include_graph": "true"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"]["deleted_by"] == str(deleted_by)
    assert body["nodes"][0]["deleted_by"] == str(deleted_by)
    assert body["relationships"][0]["deleted_by"] == str(deleted_by)
    assert body["loop_configs"][0]["deleted_by"] == str(deleted_by)


def test_get_revision_with_graph_returns_detail(client: tuple[TestClient, _Harness]) -> None:

    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=_revision_row()))
    harness.stub(
        "get_full_revision_graph",
        AsyncMock(
            return_value={
                "revision": _revision_row(),
                "nodes": [_node_row()],
                "relationships": [_relationship_row()],
                "loop_configs": [_loop_config_row()],
            }
        ),
    )

    resp = http.get(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}", params={"include_graph": "true"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "revision" in body
    assert "nodes" in body
    assert "relationships" in body
    assert "loop_configs" in body
    assert len(body["nodes"]) == 1
    harness.repository.get_full_revision_graph.assert_awaited_once_with(
        harness.repository.get_full_revision_graph.call_args.args[0],
        organisation_id=_ORG_ID,
        revision_id=_REV_ID,
    )


def test_get_revision_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=None))

    resp = http.get(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}")

    assert resp.status_code == 404


def test_get_revision_missing_revision_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=None))

    resp = http.get(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Revision not found"


def test_create_revision_returns_201(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("list_revisions", AsyncMock(return_value=[]))
    harness.stub("create_revision", AsyncMock(return_value=_revision_row()))
    harness.stub("validate_bindings", AsyncMock(return_value=[]))
    harness.stub("create_node", AsyncMock(return_value=_node_row()))
    harness.stub("create_relationship", AsyncMock(return_value=_relationship_row()))
    harness.stub("create_loop_config", AsyncMock(return_value=_loop_config_row()))

    pipeline_id = uuid.uuid4()
    payload = {
        "nodes": [
            {
                "node_key": "node-1",
                "node_type": "pipeline",
                "display_name": "Node 1",
                "config_json": {"created_at": "2026-01-01T00:00:00Z", "amount": "1.25"},
                "pipeline_id": str(pipeline_id),
                "connector_instance_id": None,
                "owner_team_id": None,
                "eval_id": None,
                "position_x": "1.50",
                "position_y": "2.25",
            },
            {
                "node_key": "loop-1",
                "node_type": "improvement_loop",
                "display_name": "Loop 1",
            },
        ],
        "relationships": [],
        "loop_configs": [
            {
                "node_key": "loop-1",
                "target_signal": "error_rate",
                "observation_window_seconds": 300,
                "action_pipeline_id": str(pipeline_id),
                "budget_limit_usd": "10.50",
                "cooldown_seconds": 60,
                "outcome_metric": "success_rate",
            }
        ],
        "notes": "Initial revision",
    }
    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions", json=payload)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == str(_REV_ID)
    assert body["revision_number"] == 1

    create_revision = harness.repository.create_revision
    content = create_revision.await_args.kwargs["content_json"]
    assert content["nodes"][0]["pipeline_id"] == str(pipeline_id)
    assert content["nodes"][0]["position_x"] == "1.50"
    assert content["loop_configs"][0]["budget_limit_usd"] == "10.50"


def test_create_revision_rejects_invalid_bindings(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub(
        "validate_bindings",
        AsyncMock(return_value=["Pipeline does not exist in this organisation"]),
    )

    response = http.post(
        f"{_BASE}/{_MAP_ID}/revisions",
        json={
            "nodes": [
                {
                    "node_key": "node-1",
                    "node_type": "pipeline",
                    "display_name": "Node 1",
                    "pipeline_id": str(uuid.uuid4()),
                }
            ],
            "relationships": [],
            "loop_configs": [],
        },
    )

    assert response.status_code == 422
    assert "Invalid System Map binding" in response.json()["detail"]
    harness.repository.create_revision.assert_not_called()


def test_create_revision_rejects_dangling_relationship_endpoint(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))

    response = http.post(
        f"{_BASE}/{_MAP_ID}/revisions",
        json={
            "nodes": [{"node_key": "node-1", "node_type": "pipeline", "display_name": "Node 1"}],
            "relationships": [
                {
                    "source_node_key": "node-1",
                    "target_node_key": "missing",
                    "relationship_type": "flow",
                }
            ],
            "loop_configs": [],
        },
    )

    assert response.status_code == 422
    assert "missing" in response.json()["detail"]
    harness.repository.create_revision.assert_not_called()


def test_create_revision_rejects_loop_config_for_non_loop_node(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))

    response = http.post(
        f"{_BASE}/{_MAP_ID}/revisions",
        json={
            "nodes": [{"node_key": "node-1", "node_type": "pipeline", "display_name": "Node 1"}],
            "relationships": [],
            "loop_configs": [
                {
                    "node_key": "node-1",
                    "target_signal": "error_rate",
                    "observation_window_seconds": 300,
                    "action_pipeline_id": str(uuid.uuid4()),
                    "budget_limit_usd": "10.00",
                    "cooldown_seconds": 60,
                    "outcome_metric": "success_rate",
                }
            ],
        },
    )

    assert response.status_code == 422
    assert "improvement_loop" in response.json()["detail"]
    harness.repository.create_revision.assert_not_called()


def test_create_attribution_binding_requires_trigger_id(client: tuple[TestClient, _Harness]) -> None:
    http, _harness = client

    response = http.post(
        f"{_BASE}/{_MAP_ID}/attribution-bindings",
        json={"pipeline_id": str(uuid.uuid4()), "node_key": "node-1", "binding_kind": "trigger"},
    )

    assert response.status_code == 422
    assert "trigger_id" in response.json()["detail"]


def test_create_attribution_binding_rejects_trigger_id_for_action_pipeline(
    client: tuple[TestClient, _Harness],
) -> None:
    http, _harness = client

    response = http.post(
        f"{_BASE}/{_MAP_ID}/attribution-bindings",
        json={
            "pipeline_id": str(uuid.uuid4()),
            "trigger_id": str(uuid.uuid4()),
            "node_key": "node-1",
            "binding_kind": "action_pipeline",
        },
    )

    assert response.status_code == 422
    assert "cannot include trigger_id" in response.json()["detail"]


def test_create_revision_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=None))

    payload = {"nodes": [], "relationships": [], "loop_configs": [], "notes": ""}
    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions", json=payload)

    assert resp.status_code == 404


def test_publish_revision_returns_200_on_success(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    published_rev = _revision_row(status="published", revision_number=2)
    published_rev.published_at = datetime.now(UTC)

    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=_revision_row()))
    harness.stub("validate_publish", AsyncMock(return_value=(True, [])))
    harness.stub("publish_revision", AsyncMock(return_value=published_rev))

    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/publish")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision"]["status"] == "published"
    assert not body["validation_errors"]


def test_publish_revision_passes_principal_to_binding_validation(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=_revision_row()))
    validate_publish = AsyncMock(return_value=(False, ["invalid binding"]))
    harness.stub("validate_publish", validate_publish)

    response = http.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/publish")

    assert response.status_code == 200
    assert validate_publish.await_args.kwargs["account_id"] == _USER_ID
    assert validate_publish.await_args.kwargs["org_role"] == "operator"


def test_publish_revision_returns_validation_errors(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=_revision_row()))
    harness.stub("validate_publish", AsyncMock(return_value=(False, ["Cycle detected", "Loop config missing"])))

    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/publish")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["validation_errors"]) == 2
    assert body["validation_errors"][0]["message"] == "Cycle detected"
    assert body["validation_errors"][1]["message"] == "Loop config missing"


def test_publish_revision_already_published_returns_409(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=_revision_row(status="published")))

    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/publish")

    assert resp.status_code == 409


def test_publish_revision_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=None))

    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/publish")

    assert resp.status_code == 404


def test_publish_revision_missing_revision_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=None))

    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/publish")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Revision not found"


def test_publish_revision_rejects_cross_org_pipeline(client: tuple[TestClient, _Harness]) -> None:
    """Publish validation rejects cross-org pipeline bindings."""
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=_revision_row()))
    # validate_publish will check pipeline existence and reject cross-org
    harness.stub(
        "validate_publish",
        AsyncMock(
            return_value=(
                False,
                ["Node 'node-1' references pipeline ... which does not exist or is not in the same organisation"],
            )
        ),
    )

    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/publish")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["validation_errors"]) == 1
    assert "not in the same organisation" in body["validation_errors"][0]["message"]


def test_archive_revision_returns_200(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=_revision_row(status="published")))

    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/archive")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "archived"


def test_archive_revision_already_archived_returns_409(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=_revision_row(status="archived")))

    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/archive")

    assert resp.status_code == 409


def test_delete_revision_returns_204(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=_revision_row(status="draft")))

    resp = http.delete(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}")

    assert resp.status_code == 204, resp.text


def test_delete_published_revision_rejected_409(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=_revision_row(status="published")))

    resp = http.delete(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}")

    assert resp.status_code == 409
    assert "archive it first" in resp.json()["detail"]


# ─── Overview tests ───


def test_get_overview_returns_aggregate(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_published_revision", AsyncMock(return_value=_revision_row(status="published")))
    harness.stub(
        "get_full_revision_graph",
        AsyncMock(
            return_value={
                "revision": _revision_row(status="published"),
                "nodes": [_node_row(node_key="node-1", node_type="pipeline")],
                "relationships": [],
                "loop_configs": [_loop_config_row(node_key="loop-1")],
            }
        ),
    )
    harness.stub("get_overview_pipeline_metrics", AsyncMock(return_value={}))

    resp = http.get(f"{_BASE}/{_MAP_ID}/overview")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["system_map_id"] == str(_MAP_ID)
    assert body["revision_id"] == str(_REV_ID)
    assert body["revision_number"] == 1
    assert "nodes" in body
    assert "loop_configs" in body
    assert "bottlenecks" in body


def test_get_overview_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=None))

    resp = http.get(f"{_BASE}/{_MAP_ID}/overview")

    assert resp.status_code == 404


def test_get_overview_no_published_revision_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_published_revision", AsyncMock(return_value=None))

    resp = http.get(f"{_BASE}/{_MAP_ID}/overview")

    assert resp.status_code == 404
    assert "No published revision found" in resp.json()["detail"]


def test_overview_detects_missing_loop_config_bottleneck(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_published_revision", AsyncMock(return_value=_revision_row(status="published")))
    harness.stub(
        "get_full_revision_graph",
        AsyncMock(
            return_value={
                "revision": _revision_row(status="published"),
                "nodes": [_node_row(node_key="loop-1", node_type="improvement_loop")],
                "relationships": [],
                "loop_configs": [],  # Missing loop config!
            }
        ),
    )
    harness.stub("get_overview_pipeline_metrics", AsyncMock(return_value={}))

    resp = http.get(f"{_BASE}/{_MAP_ID}/overview")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["bottlenecks"]) == 1
    assert body["bottlenecks"][0]["kind"] == "unobserved"
    assert "missing loop configuration" in body["bottlenecks"][0]["detail"]


# ─── Attribution Binding tests ───


def test_create_attribution_binding_returns_201(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    pipeline_id = uuid.uuid4()
    node = _node_row()
    node.pipeline_id = pipeline_id
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_node_by_map_revision_key", AsyncMock(return_value=node))
    harness.stub("create_attribution_binding", AsyncMock(return_value=_binding_row(pipeline_id=pipeline_id)))

    payload = {
        "pipeline_id": str(pipeline_id),
        "trigger_id": str(uuid.uuid4()),
        "node_key": "node-1",
        "binding_kind": "trigger",
    }
    resp = http.post(f"{_BASE}/{_MAP_ID}/attribution-bindings", json=payload)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["node_key"] == "node-1"
    assert body["binding_kind"] == "trigger"


def test_create_attribution_binding_rejects_pipeline_mismatch(
    client: tuple[TestClient, _Harness],
) -> None:
    http, harness = client
    node = _node_row()
    node.pipeline_id = uuid.uuid4()
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_node_by_map_revision_key", AsyncMock(return_value=node))

    response = http.post(
        f"{_BASE}/{_MAP_ID}/attribution-bindings",
        json={
            "pipeline_id": str(uuid.uuid4()),
            "trigger_id": str(uuid.uuid4()),
            "node_key": "node-1",
            "binding_kind": "trigger",
        },
    )

    assert response.status_code == 422
    assert "different pipeline" in response.json()["detail"]
    harness.repository.create_attribution_binding.assert_not_called()


def test_create_action_pipeline_binding_accepts_matching_loop_node(
    client: tuple[TestClient, _Harness],
) -> None:
    http, harness = client
    action_pipeline_id = uuid.uuid4()
    node = _node_row(node_type="improvement_loop")
    loop_config = _loop_config_row(node_key="node-1", action_pipeline_id=action_pipeline_id)
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_node_by_map_revision_key", AsyncMock(return_value=node))
    harness.stub("get_loop_config_by_node_key", AsyncMock(return_value=loop_config))
    harness.stub(
        "create_attribution_binding",
        AsyncMock(return_value=_binding_row(pipeline_id=action_pipeline_id, binding_kind="action_pipeline")),
    )

    response = http.post(
        f"{_BASE}/{_MAP_ID}/attribution-bindings",
        json={"pipeline_id": str(action_pipeline_id), "node_key": "node-1", "binding_kind": "action_pipeline"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["binding_kind"] == "action_pipeline"


def test_create_attribution_binding_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=None))

    payload = {
        "pipeline_id": str(uuid.uuid4()),
        "trigger_id": str(uuid.uuid4()),
        "node_key": "node-1",
        "binding_kind": "trigger",
    }
    resp = http.post(f"{_BASE}/{_MAP_ID}/attribution-bindings", json=payload)

    assert resp.status_code == 404


def test_create_attribution_binding_rejects_unmatched_node_pipeline(
    client: tuple[TestClient, _Harness],
) -> None:
    http, harness = client
    node = _node_row()
    node.pipeline_id = uuid.uuid4()
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_node_by_map_revision_key", AsyncMock(return_value=node))

    payload = {
        "pipeline_id": str(uuid.uuid4()),
        "trigger_id": str(uuid.uuid4()),
        "node_key": "node-1",
        "binding_kind": "trigger",
    }
    resp = http.post(f"{_BASE}/{_MAP_ID}/attribution-bindings", json=payload)

    assert resp.status_code == 422
    assert "different pipeline" in resp.json()["detail"]


def test_list_attribution_bindings_ignores_include_deleted_and_keeps_live_scope(
    client: tuple[TestClient, _Harness],
) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    list_bindings = AsyncMock(return_value=[])
    harness.stub("list_attribution_bindings", list_bindings)

    resp = http.get(f"{_BASE}/{_MAP_ID}/attribution-bindings", params={"include_deleted": "true"})

    assert resp.status_code == 200, resp.text
    assert "include_deleted" not in list_bindings.await_args.kwargs


def test_list_attribution_bindings_returns_list(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("list_attribution_bindings", AsyncMock(return_value=[_binding_row()]))

    resp = http.get(f"{_BASE}/{_MAP_ID}/attribution-bindings")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["node_key"] == "node-1"


def test_delete_attribution_binding_returns_204(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("delete_attribution_binding", AsyncMock(return_value=True))

    binding_id = uuid.uuid4()
    resp = http.delete(f"{_BASE}/{_MAP_ID}/attribution-bindings/{binding_id}")

    assert resp.status_code == 204, resp.text


def test_delete_attribution_binding_missing_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("delete_attribution_binding", AsyncMock(return_value=False))

    binding_id = uuid.uuid4()
    resp = http.delete(f"{_BASE}/{_MAP_ID}/attribution-bindings/{binding_id}")

    assert resp.status_code == 404


# ─── Error mapping matrix ───

_PROG = ProgrammingError("s", {}, Exception())
_SQL = SQLAlchemyError("boom")
_RUNTIME = RuntimeError("kaboom")
_INTEGRITY = IntegrityError("s", {}, Exception())

_ENDPOINT_PAYLOADS = {
    "list": ("GET", _BASE, None, "list_maps_by_org"),
    "create": ("POST", _BASE, {"name": "Map", "visibility": "org"}, "create_map"),
    "get": ("GET", f"{_BASE}/{_MAP_ID}", None, "get_map_by_id"),
    "update": ("PUT", f"{_BASE}/{_MAP_ID}", {"name": "Map"}, "get_map_by_id"),
    "delete": ("DELETE", f"{_BASE}/{_MAP_ID}", None, "soft_delete_map"),
    "get_revision": ("GET", f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}", None, "get_map_by_id"),
    "create_revision": (
        "POST",
        f"{_BASE}/{_MAP_ID}/revisions",
        {"nodes": [], "relationships": [], "loop_configs": [], "notes": ""},
        "get_map_by_id",
    ),
    "publish_revision": ("POST", f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/publish", None, "get_map_by_id"),
    "archive_revision": ("POST", f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/archive", None, "get_map_by_id"),
    "delete_revision": ("DELETE", f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}", None, "get_map_by_id"),
    "overview": ("GET", f"{_BASE}/{_MAP_ID}/overview", None, "get_map_by_id"),
    "create_binding": (
        "POST",
        f"{_BASE}/{_MAP_ID}/attribution-bindings",
        {
            "pipeline_id": str(uuid.uuid4()),
            "trigger_id": str(uuid.uuid4()),
            "node_key": "n1",
            "binding_kind": "trigger",
        },
        "get_map_by_id",
    ),
    "list_bindings": ("GET", f"{_BASE}/{_MAP_ID}/attribution-bindings", None, "get_map_by_id"),
    "delete_binding": (
        "DELETE",
        f"{_BASE}/{_MAP_ID}/attribution-bindings/{uuid.uuid4()}",
        None,
        "get_map_by_id",
    ),
}


@pytest.mark.parametrize("endpoint", sorted(_ENDPOINT_PAYLOADS))
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_PROG, 501),
        (_SQL, 503),
        (_RUNTIME, 500),
    ],
)
def test_service_error_mapping_matrix(
    client: tuple[TestClient, _Harness],
    endpoint: str,
    exc: Exception,
    expected: int,
) -> None:
    """Every endpoint honours the route error convention:
    ProgrammingError→501, SQLAlchemyError→503, Exception→500."""
    http, harness = client
    method, url, payload, service = _ENDPOINT_PAYLOADS[endpoint]
    harness.stub(service, AsyncMock(side_effect=exc))

    resp = http.request(method, url, json=payload)

    assert resp.status_code == expected, f"{endpoint} + {type(exc).__name__}: {resp.text}"


@pytest.mark.parametrize(
    "endpoint",
    ["create", "update", "create_revision", "create_binding"],
)
def test_integrity_error_maps_to_409(
    client: tuple[TestClient, _Harness],
    endpoint: str,
) -> None:
    """IntegrityError on write operations maps to 409."""
    http, harness = client
    method, url, payload, service = _ENDPOINT_PAYLOADS[endpoint]
    harness.stub(service, AsyncMock(side_effect=_INTEGRITY))

    resp = http.request(method, url, json=payload)

    assert resp.status_code == 409, f"{endpoint}: {resp.text}"


# ─── Schema validation tests ───


def test_system_map_create_schema_validates_node_types() -> None:
    from modulo.api.schemas.system_maps import VALID_NODE_TYPES, SystemMapNodeCreate

    for node_type in VALID_NODE_TYPES:
        node = SystemMapNodeCreate(
            node_key="test",
            node_type=node_type,  # type: ignore[arg-type]
            display_name="Test",
        )
        assert node.node_type == node_type


def test_system_map_create_schema_rejects_invalid_node_type() -> None:
    from pydantic import ValidationError

    from modulo.api.schemas.system_maps import SystemMapNodeCreate

    with pytest.raises(ValidationError):
        SystemMapNodeCreate(
            node_key="test",
            node_type="invalid_type",  # type: ignore[arg-type]
            display_name="Test",
        )


def test_system_map_schemas_reject_vocabulary_substrings() -> None:
    from pydantic import ValidationError

    from modulo.api.schemas.system_maps import (
        AttributionBindingCreate,
        SystemMapNodeCreate,
        SystemMapRelationshipCreate,
    )

    with pytest.raises(ValidationError):
        SystemMapNodeCreate(
            node_key="test",
            node_type="not-an-agent-actually",  # type: ignore[arg-type]
            display_name="Test",
        )

    with pytest.raises(ValidationError):
        SystemMapRelationshipCreate(
            source_node_key="a",
            target_node_key="b",
            relationship_type="testflowend",  # type: ignore[arg-type]
        )

    with pytest.raises(ValidationError):
        AttributionBindingCreate(
            pipeline_id=uuid.uuid4(),
            node_key="node-1",
            binding_kind="pretriggersuffix",  # type: ignore[arg-type]
        )


def test_system_map_revision_create_schema_validates_loop_config() -> None:
    from modulo.api.schemas.system_maps import SystemMapLoopConfigCreate, SystemMapNodeCreate, SystemMapRevisionCreate

    rev = SystemMapRevisionCreate(
        nodes=[SystemMapNodeCreate(node_key="loop-1", node_type="improvement_loop", display_name="Loop 1")],
        relationships=[],
        loop_configs=[
            SystemMapLoopConfigCreate(
                node_key="loop-1",
                target_signal="error_rate",
                observation_window_seconds=300,
                action_pipeline_id=uuid.uuid4(),
                budget_limit_usd=Decimal("10.00"),
                cooldown_seconds=3600,
                outcome_metric="success_rate",
            )
        ],
    )
    assert len(rev.loop_configs) == 1


def test_system_map_overview_response_includes_bottlenecks() -> None:
    from modulo.api.schemas.system_maps import (
        SystemMapOverviewBottleneck,
        SystemMapOverviewLoopConfig,
        SystemMapOverviewNodeHealth,
        SystemMapOverviewResponse,
    )

    overview = SystemMapOverviewResponse(
        system_map_id=_MAP_ID,
        revision_id=_REV_ID,
        revision_number=1,
        generated_at=datetime.now(UTC),
        window_days=7,
        window_start=datetime.now(UTC),
        window_end=datetime.now(UTC),
        nodes=[
            SystemMapOverviewNodeHealth(
                node_key="node-1",
                node_type="pipeline",
                display_name="Pipeline 1",
                health="healthy",
            )
        ],
        loop_configs=[
            SystemMapOverviewLoopConfig(
                node_key="loop-1",
                target_signal="error_rate",
                observation_window_seconds=300,
                action_pipeline_id=uuid.uuid4(),
                budget_limit_usd=Decimal("10.00"),
                cooldown_seconds=3600,
                outcome_metric="success_rate",
            )
        ],
        bottlenecks=[
            SystemMapOverviewBottleneck(
                node_key="loop-1",
                node_type="improvement_loop",
                display_name="Loop 1",
                kind="unobserved",
                detail="Missing loop config",
                severity="high",
            )
        ],
    )
    assert len(overview.bottlenecks) == 1
    assert overview.bottlenecks[0].kind == "unobserved"


# ─── Changed-lines coverage: helper, detail and error arms ───


def _install_session(*, scalar_one: object = 1, one_or_none: object = None) -> MagicMock:
    """Override the request session with a scripted execute() result."""
    from modulo.api.dependencies import get_db_session

    session = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=cm)
    session.begin_nested = MagicMock(return_value=cm)
    session.flush = AsyncMock()
    session.rollback = AsyncMock()
    session.add = MagicMock()
    session.info = {}
    bind = MagicMock()
    bind.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind)
    result = MagicMock()
    result.scalar_one.return_value = scalar_one
    result.scalar_one_or_none.return_value = one_or_none
    session.execute = AsyncMock(return_value=result)

    async def _get_session():
        yield session

    app.dependency_overrides[get_db_session] = _get_session
    return session


async def test_get_repo_builds_repository_with_shared_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modulo.api.routes import system_maps as routes

    session = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    monkeypatch.setattr(routes, "get_settings", lambda: MagicMock())
    monkeypatch.setattr(routes, "get_or_create_engine", lambda settings: MagicMock())
    monkeypatch.setattr(routes, "get_or_create_session_factory", lambda engine: factory)

    repository = await routes._get_repo()

    assert isinstance(repository, SystemMapRepository)
    generator = repository._session_factory()
    assert await anext(generator) is session


async def test_get_repo_dependency_delegates_to_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    from modulo.api.routes import system_maps as routes

    sentinel = MagicMock()
    monkeypatch.setattr(routes, "_get_repo", AsyncMock(return_value=sentinel))

    assert await routes.get_repo() is sentinel


async def test_record_audit_swallows_write_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from modulo.api.routes import system_maps as routes

    session = MagicMock()
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested)
    append_audit = AsyncMock(side_effect=RuntimeError("audit down"))
    monkeypatch.setattr(routes, "append_audit_event", append_audit)

    await routes._record_audit(
        session,
        org_id=_ORG_ID,
        event_type="system_map.updated",
        account_id=_USER_ID,
        resource_id=_MAP_ID,
    )

    append_audit.assert_awaited_once()


def test_list_system_maps_filters_count_by_visibility(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("list_maps_by_org", AsyncMock(return_value=[]))

    resp = http.get(_BASE, params={"visibility": "org"})

    assert resp.status_code == 200, resp.text


def test_list_system_maps_uses_scalar_total(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("list_maps_by_org", AsyncMock(return_value=[]))
    _install_session(scalar_one=7)

    resp = http.get(_BASE)

    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 7


def _direct_principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
        is_system_admin=False,
        via_api_key=False,
        client_kind="test",
    )


def _direct_session() -> MagicMock:
    session = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=cm)
    session.begin_nested = MagicMock(return_value=cm)
    session.flush = AsyncMock()
    session.info = {}
    bind = MagicMock()
    bind.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind)
    session.execute = AsyncMock(return_value=MagicMock())
    return session


async def _assert_http_exception_reraised(endpoint, **kwargs: object) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await endpoint(**kwargs)
    assert exc_info.value.status_code == 418


async def test_list_endpoint_reraises_http_exception() -> None:
    from modulo.api.routes.system_maps import list_system_maps_endpoint

    repository = MagicMock(spec=SystemMapRepository)
    repository.list_maps_by_org = AsyncMock(side_effect=HTTPException(status_code=418, detail="teapot"))
    await _assert_http_exception_reraised(
        list_system_maps_endpoint,
        page=1,
        page_size=20,
        name=None,
        owner_team_id=None,
        visibility=None,
        session=_direct_session(),
        principal=_direct_principal(),
        repository=repository,
    )


async def test_get_endpoint_reraises_http_exception() -> None:
    from modulo.api.routes.system_maps import get_system_map_endpoint

    repository = MagicMock(spec=SystemMapRepository)
    repository.get_map_by_id = AsyncMock(side_effect=HTTPException(status_code=418, detail="teapot"))
    await _assert_http_exception_reraised(
        get_system_map_endpoint,
        map_id=_MAP_ID,
        session=_direct_session(),
        principal=_direct_principal(),
        repository=repository,
    )


async def test_update_endpoint_reraises_http_exception() -> None:
    from modulo.api.routes.system_maps import update_system_map_endpoint
    from modulo.api.schemas.system_maps import SystemMapUpdate

    repository = MagicMock(spec=SystemMapRepository)
    repository.get_map_by_id = AsyncMock(side_effect=HTTPException(status_code=418, detail="teapot"))
    await _assert_http_exception_reraised(
        update_system_map_endpoint,
        map_id=_MAP_ID,
        req=SystemMapUpdate(name="New"),
        session=_direct_session(),
        principal=_direct_principal(),
        repository=repository,
    )


async def test_delete_endpoint_reraises_http_exception() -> None:
    from modulo.api.routes.system_maps import delete_system_map_endpoint

    repository = MagicMock(spec=SystemMapRepository)
    repository.soft_delete_map = AsyncMock(side_effect=HTTPException(status_code=418, detail="teapot"))
    await _assert_http_exception_reraised(
        delete_system_map_endpoint,
        map_id=_MAP_ID,
        session=_direct_session(),
        principal=_direct_principal(),
        repository=repository,
    )


async def test_list_bindings_endpoint_reraises_http_exception() -> None:
    from modulo.api.routes.system_maps import list_attribution_bindings_endpoint

    repository = MagicMock(spec=SystemMapRepository)
    repository.get_map_by_id = AsyncMock(side_effect=HTTPException(status_code=418, detail="teapot"))
    await _assert_http_exception_reraised(
        list_attribution_bindings_endpoint,
        map_id=_MAP_ID,
        session=_direct_session(),
        principal=_direct_principal(),
        repository=repository,
    )


async def test_delete_binding_endpoint_reraises_http_exception() -> None:
    from modulo.api.routes.system_maps import delete_attribution_binding_endpoint

    repository = MagicMock(spec=SystemMapRepository)
    repository.get_map_by_id = AsyncMock(side_effect=HTTPException(status_code=418, detail="teapot"))
    await _assert_http_exception_reraised(
        delete_attribution_binding_endpoint,
        map_id=_MAP_ID,
        binding_id=uuid.uuid4(),
        session=_direct_session(),
        principal=_direct_principal(),
        repository=repository,
    )


def test_update_system_map_rejects_empty_payload(client: tuple[TestClient, _Harness]) -> None:
    http, _ = client

    resp = http.put(f"{_BASE}/{_MAP_ID}", json={})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "No fields to update"


def test_update_system_map_validates_team_transition(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(side_effect=[_map_row(), _map_row(visibility="team")]))

    resp = http.put(f"{_BASE}/{_MAP_ID}", json={"visibility": "team"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["visibility"] == "team"


def test_update_system_map_team_transition_missing_map(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=None))

    resp = http.put(f"{_BASE}/{_MAP_ID}", json={"visibility": "team"})

    assert resp.status_code == 404


def test_get_revision_with_graph_missing_graph_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=_revision_row()))
    harness.stub("get_full_revision_graph", AsyncMock(return_value=None))

    resp = http.get(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}", params={"include_graph": "true"})

    assert resp.status_code == 404


def test_create_revision_persists_relationships(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("list_revisions", AsyncMock(return_value=[]))
    harness.stub("create_revision", AsyncMock(return_value=_revision_row()))
    harness.stub("validate_bindings", AsyncMock(return_value=[]))
    harness.stub("create_node", AsyncMock(return_value=_node_row()))
    harness.stub("create_relationship", AsyncMock(return_value=_relationship_row()))

    payload = {
        "nodes": [{"node_key": "node-1", "node_type": "pipeline", "display_name": "Node 1"}],
        "relationships": [{"source_node_key": "node-1", "target_node_key": "node-1", "relationship_type": "flow"}],
        "loop_configs": [],
    }
    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions", json=payload)

    assert resp.status_code == 201, resp.text
    harness.repository.create_relationship.assert_awaited_once()


def test_publish_revision_missing_publish_result_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=_revision_row()))
    harness.stub("validate_publish", AsyncMock(return_value=(True, [])))
    harness.stub("publish_revision", AsyncMock(return_value=None))

    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/publish")

    assert resp.status_code == 404


def test_publish_revision_integrity_error_returns_409(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=_revision_row()))
    harness.stub("validate_publish", AsyncMock(return_value=(True, [])))
    harness.stub("publish_revision", AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("dup"))))

    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/publish")

    assert resp.status_code == 409, resp.text


def test_archive_revision_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=None))

    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/archive")

    assert resp.status_code == 404


def test_archive_revision_missing_revision_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=None))

    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/archive")

    assert resp.status_code == 404


def test_archive_revision_keeps_unrelated_published_pointer(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub(
        "get_map_by_id",
        AsyncMock(return_value=_map_row(published_revision_id=uuid.uuid4())),
    )
    harness.stub("get_revision_by_id", AsyncMock(return_value=_revision_row(status="published")))

    resp = http.post(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}/archive")

    assert resp.status_code == 200, resp.text


def test_delete_revision_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=None))

    resp = http.delete(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}")

    assert resp.status_code == 404


def test_delete_revision_missing_revision_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_revision_by_id", AsyncMock(return_value=None))

    resp = http.delete(f"{_BASE}/{_MAP_ID}/revisions/{_REV_ID}")

    assert resp.status_code == 404


def test_overview_with_matching_loop_config_has_no_bottleneck(
    client: tuple[TestClient, _Harness],
) -> None:
    http, harness = client
    loop_node = _node_row(node_key="loop-1", node_type="improvement_loop")
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_published_revision", AsyncMock(return_value=_revision_row(status="published")))
    harness.stub(
        "get_full_revision_graph",
        AsyncMock(
            return_value={
                "revision": _revision_row(status="published"),
                "nodes": [loop_node],
                "relationships": [],
                "loop_configs": [_loop_config_row(node_key="loop-1")],
            }
        ),
    )

    resp = http.get(f"{_BASE}/{_MAP_ID}/overview")

    assert resp.status_code == 200, resp.text
    assert not resp.json()["bottlenecks"]


def test_overview_missing_graph_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_published_revision", AsyncMock(return_value=_revision_row(status="published")))
    harness.stub("get_full_revision_graph", AsyncMock(return_value=None))

    resp = http.get(f"{_BASE}/{_MAP_ID}/overview")

    assert resp.status_code == 404


def _binding_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "pipeline_id": str(uuid.uuid4()),
        "trigger_id": str(uuid.uuid4()),
        "node_key": "node-1",
        "binding_kind": "trigger",
    }
    payload.update(overrides)
    return payload


def test_create_binding_without_published_revision_returns_409(
    client: tuple[TestClient, _Harness],
) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_published_revision", AsyncMock(return_value=None))

    resp = http.post(f"{_BASE}/{_MAP_ID}/attribution-bindings", json=_binding_payload())

    assert resp.status_code == 409


def test_create_binding_missing_node_returns_422(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_published_revision", AsyncMock(return_value=_revision_row(status="published")))
    harness.stub("get_node_by_map_revision_key", AsyncMock(return_value=None))

    resp = http.post(f"{_BASE}/{_MAP_ID}/attribution-bindings", json=_binding_payload())

    assert resp.status_code == 422
    assert "does not exist in the published map revision" in resp.json()["detail"]


def test_create_binding_wrong_node_type_returns_422(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_published_revision", AsyncMock(return_value=_revision_row(status="published")))
    harness.stub(
        "get_node_by_map_revision_key",
        AsyncMock(return_value=_node_row(node_type="improvement_loop")),
    )

    resp = http.post(f"{_BASE}/{_MAP_ID}/attribution-bindings", json=_binding_payload())

    assert resp.status_code == 422
    assert "require a pipeline node" in resp.json()["detail"]


def test_create_action_binding_missing_loop_config_returns_422(
    client: tuple[TestClient, _Harness],
) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_published_revision", AsyncMock(return_value=_revision_row(status="published")))
    harness.stub(
        "get_node_by_map_revision_key",
        AsyncMock(return_value=_node_row(node_type="improvement_loop")),
    )
    harness.stub("get_loop_config_by_node_key", AsyncMock(return_value=None))

    resp = http.post(
        f"{_BASE}/{_MAP_ID}/attribution-bindings",
        json=_binding_payload(binding_kind="action_pipeline", trigger_id=None),
    )

    assert resp.status_code == 422
    assert "no improvement-loop configuration" in resp.json()["detail"]


def test_create_action_binding_mismatched_loop_pipeline_returns_422(
    client: tuple[TestClient, _Harness],
) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_published_revision", AsyncMock(return_value=_revision_row(status="published")))
    harness.stub(
        "get_node_by_map_revision_key",
        AsyncMock(return_value=_node_row(node_type="improvement_loop")),
    )
    harness.stub(
        "get_loop_config_by_node_key",
        AsyncMock(return_value=_loop_config_row(action_pipeline_id=uuid.uuid4())),
    )

    resp = http.post(
        f"{_BASE}/{_MAP_ID}/attribution-bindings",
        json=_binding_payload(binding_kind="action_pipeline", trigger_id=None),
    )

    assert resp.status_code == 422
    assert "different action pipeline" in resp.json()["detail"]


def test_create_binding_missing_pipeline_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    pipeline_id = uuid.uuid4()
    node = _node_row()
    node.pipeline_id = pipeline_id
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_published_revision", AsyncMock(return_value=_revision_row(status="published")))
    harness.stub("get_node_by_map_revision_key", AsyncMock(return_value=node))
    _install_session(one_or_none=None)

    resp = http.post(
        f"{_BASE}/{_MAP_ID}/attribution-bindings",
        json=_binding_payload(pipeline_id=str(pipeline_id)),
    )

    assert resp.status_code == 404
    assert "not found in this organisation" in resp.json()["detail"]


def test_create_binding_missing_trigger_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    pipeline_id = uuid.uuid4()
    node = _node_row()
    node.pipeline_id = pipeline_id
    harness.stub("get_map_by_id", AsyncMock(return_value=_map_row()))
    harness.stub("get_published_revision", AsyncMock(return_value=_revision_row(status="published")))
    harness.stub("get_node_by_map_revision_key", AsyncMock(return_value=node))
    # RLS set_config calls also use execute(), so resolve the pipeline and the
    # trigger from scalar_one_or_none in call order instead of side_effecting
    # execute() itself.
    session = _install_session()

    def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        result = MagicMock()
        if "FROM pipelines" in str(stmt):
            result.scalar_one_or_none.return_value = MagicMock(id=pipeline_id)
        else:
            result.scalar_one_or_none.return_value = None
        return result

    session.execute = AsyncMock(side_effect=_execute)

    resp = http.post(
        f"{_BASE}/{_MAP_ID}/attribution-bindings",
        json=_binding_payload(pipeline_id=str(pipeline_id)),
    )

    assert resp.status_code == 404
    assert "Trigger" in resp.json()["detail"]


def test_list_bindings_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=None))

    resp = http.get(f"{_BASE}/{_MAP_ID}/attribution-bindings")

    assert resp.status_code == 404


def test_delete_binding_missing_map_returns_404(client: tuple[TestClient, _Harness]) -> None:
    http, harness = client
    harness.stub("get_map_by_id", AsyncMock(return_value=None))

    resp = http.delete(f"{_BASE}/{_MAP_ID}/attribution-bindings/{uuid.uuid4()}")

    assert resp.status_code == 404


def test_system_map_revision_schema_rejects_duplicate_and_dangling_entries() -> None:
    from pydantic import ValidationError

    from modulo.api.schemas.system_maps import (
        SystemMapLoopConfigCreate,
        SystemMapNodeCreate,
        SystemMapRelationshipCreate,
        SystemMapRevisionCreate,
    )

    node = SystemMapNodeCreate(node_key="a", node_type="pipeline", display_name="A")
    loop = SystemMapLoopConfigCreate(
        node_key="a",
        target_signal="error_rate",
        observation_window_seconds=60,
        action_pipeline_id=uuid.uuid4(),
        budget_limit_usd=Decimal(1),
        cooldown_seconds=1,
        outcome_metric="error_rate",
    )

    with pytest.raises(ValidationError):
        SystemMapRevisionCreate(nodes=[node, node])

    with pytest.raises(ValidationError):
        SystemMapRevisionCreate(
            nodes=[node],
            relationships=[
                SystemMapRelationshipCreate(source_node_key="a", target_node_key="a", relationship_type="flow"),
                SystemMapRelationshipCreate(source_node_key="a", target_node_key="a", relationship_type="flow"),
            ],
        )

    with pytest.raises(ValidationError):
        SystemMapRevisionCreate(
            nodes=[node],
            relationships=[
                SystemMapRelationshipCreate(source_node_key="ghost", target_node_key="a", relationship_type="flow")
            ],
        )

    with pytest.raises(ValidationError):
        SystemMapRevisionCreate(nodes=[node], loop_configs=[loop, loop])

    with pytest.raises(ValidationError):
        SystemMapRevisionCreate(nodes=[node], loop_configs=[loop.model_copy(update={"node_key": "ghost"})])

    with pytest.raises(ValidationError):
        SystemMapRevisionCreate(nodes=[node], loop_configs=[loop])
