"""Unit tests for /api/v1/parameter-schemas READ endpoints (IDOR ownership guards).

These regression tests are kept in a dedicated module (separate from the
write-endpoint tenant-isolation tests added by PR #2069 in
test_parameter_schemas_endpoint.py) so the two sibling PRs do not both add
the same new file and collide in the merge queue.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_CROSS_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000099")
_SCHEMA_ID = uuid.uuid4()
_SET_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_PREFIX = "modulo.api.routes.parameter_schemas."


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_schema() -> MagicMock:
    s = MagicMock()
    s.id = _SCHEMA_ID
    s.organisation_id = _ORG_ID
    s.name = "Test Parameter Schema"
    s.description = None
    s.account_id = uuid.uuid4()
    s.created_at = _NOW
    s.updated_at = _NOW
    return s


def _make_set() -> MagicMock:
    s = MagicMock()
    s.id = _SET_ID
    s.parameter_schema_id = _SCHEMA_ID
    s.organisation_id = _ORG_ID
    s.name = "Test Set"
    s.description = None
    s.parameters = []
    s.account_id = uuid.uuid4()
    s.created_at = _NOW
    s.updated_at = _NOW
    return s


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session)
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
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_get_parameter_schema_cross_org_returns_404(client: TestClient) -> None:
    """IDOR guard: a parameter schema belonging to another org must not be readable.

    Fails on the pre-fix code (returns 200 and leaks the row); passes once the
    organisation_id ownership check is enforced.
    """
    cross_org = _make_schema()
    cross_org.organisation_id = _CROSS_ORG_ID
    assert cross_org.organisation_id != _ORG_ID
    with (
        patch(f"{_PREFIX}get_schema", return_value=cross_org),
        patch(f"{_PREFIX}set_rls_org"),
        patch(f"{_PREFIX}set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/parameter-schemas/{_SCHEMA_ID}")
    assert resp.status_code == 404


def test_get_parameter_set_cross_org_returns_404(client: TestClient) -> None:
    """IDOR guard: a parameter set belonging to another org must not be readable.

    Fails on the pre-fix code (returns 200 and leaks the row); passes once the
    organisation_id ownership check is enforced.
    """
    cross_org = _make_set()
    cross_org.organisation_id = _CROSS_ORG_ID
    assert cross_org.organisation_id != _ORG_ID
    with (
        patch(f"{_PREFIX}get_set", return_value=cross_org),
        patch(f"{_PREFIX}set_rls_org"),
        patch(f"{_PREFIX}set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/parameter-schemas/{_SCHEMA_ID}/sets/{_SET_ID}")
    assert resp.status_code == 404


def test_get_parameter_schema_references_cross_org_returns_404(client: TestClient) -> None:
    """IDOR guard: references of a cross-org parameter schema must not be enumerable.

    Fails on the pre-fix code (returns 200 and leaks the referencing agent/set
    UUIDs of another org's schema); passes once the organisation_id ownership
    check is enforced on the schema before returning its references.
    """
    cross_org = _make_schema()
    cross_org.organisation_id = _CROSS_ORG_ID
    assert cross_org.organisation_id != _ORG_ID
    with (
        patch(f"{_PREFIX}get_schema", return_value=cross_org),
        patch(f"{_PREFIX}set_rls_org"),
        patch(f"{_PREFIX}set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/parameter-schemas/{_SCHEMA_ID}/references")
    assert resp.status_code == 404


def test_get_parameter_set_references_cross_org_returns_404(client: TestClient) -> None:
    """IDOR guard: references of a cross-org parameter set must not be enumerable.

    Fails on the pre-fix code (returns 200 and leaks the referencing
    pipeline-node/snapshot UUIDs of another org's set); passes once the
    organisation_id ownership check is enforced on the set before returning
    its references.
    """
    cross_org = _make_set()
    cross_org.organisation_id = _CROSS_ORG_ID
    assert cross_org.organisation_id != _ORG_ID
    with (
        patch(f"{_PREFIX}get_set", return_value=cross_org),
        patch(f"{_PREFIX}set_rls_org"),
        patch(f"{_PREFIX}set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/parameter-sets/{_SET_ID}/references")
    assert resp.status_code == 404


def test_diff_parameter_schema_cross_org_returns_404(client: TestClient) -> None:
    """IDOR guard: a cross-org parameter schema's diff must not be readable."""
    cross_org = _make_schema()
    cross_org.organisation_id = _CROSS_ORG_ID
    assert cross_org.organisation_id != _ORG_ID
    with (
        patch(f"{_PREFIX}get_schema", return_value=cross_org),
        patch(f"{_PREFIX}set_rls_org"),
        patch(f"{_PREFIX}set_rls_user_context"),
    ):
        resp = client.get(
            f"/api/v1/parameter-schemas/{_SCHEMA_ID}/diff",
            params={"from_version": 1, "to_version": 2},
        )
    assert resp.status_code == 404


def test_validate_parameter_values_cross_org_returns_404(client: TestClient) -> None:
    """IDOR guard: validating against a cross-org parameter schema must 404."""
    cross_org = _make_schema()
    cross_org.organisation_id = _CROSS_ORG_ID
    assert cross_org.organisation_id != _ORG_ID
    with (
        patch(f"{_PREFIX}get_schema", return_value=cross_org),
        patch(f"{_PREFIX}set_rls_org"),
        patch(f"{_PREFIX}set_rls_user_context"),
    ):
        resp = client.post(
            f"/api/v1/parameter-schemas/{_SCHEMA_ID}/validate",
            json={"values": {}},
        )
    assert resp.status_code == 404


def test_list_parameter_sets_cross_org_returns_404(client: TestClient) -> None:
    """IDOR guard: listing sets of a cross-org parameter schema must 404."""
    cross_org = _make_schema()
    cross_org.organisation_id = _CROSS_ORG_ID
    assert cross_org.organisation_id != _ORG_ID
    with (
        patch(f"{_PREFIX}get_schema", return_value=cross_org),
        patch(f"{_PREFIX}set_rls_org"),
        patch(f"{_PREFIX}set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/parameter-schemas/{_SCHEMA_ID}/sets")
    assert resp.status_code == 404
