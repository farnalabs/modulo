"""Prove-the-fix tests for the IDOR cross-org deny path on pipeline write
mutations (security/idor-verify phase).

These tests pin the security behaviour this PR adds to the pipeline
archive/restore/unarchive endpoints: a request whose principal belongs to a
different organisation than the pipeline must return 404, never mutate (or
return) the cross-org pipeline.

The org scoping is enforced by passing ``organisation_id`` into the
``get_pipeline`` lookup (pipelines.py:1810/1833/1854). The mock for
``get_pipeline`` reproduces the real lookup contract:

* when called WITH ``organisation_id=principal.organisation_id`` (post-fix),
  a cross-org pipeline is not visible, so the lookup returns ``None`` and the
  endpoint raises 404;
* when called WITHOUT the org filter (pre-fix code path), the cross-org
  pipeline is returned, the guard ``existing is None`` never trips, and the
  mutation proceeds (200).

So every test here fails on the pre-fix code (no ``organisation_id`` passed)
and passes with the guard in place. They also assert the org-scoped lookup was
invoked, locking the contract.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import (
    _get_engine,
    deny_break_glass_mint,
    get_db_session,
    get_plan_context,
)
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_WRONG_ORG_ID = uuid.UUID("00000000-0000-0000-0000-00000000dead")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_PIPELINE_ID = uuid.uuid4()

_GET_PIPELINE_MOCK: AsyncMock | None = None


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    default_result = MagicMock()
    default_result.scalar_one_or_none.return_value = MagicMock()
    session.execute = AsyncMock(return_value=default_result)
    return session


def _wrong_org_pipeline() -> MagicMock:
    p = MagicMock()
    p.organisation_id = _WRONG_ORG_ID
    return p


def _make_get_pipeline() -> AsyncMock:
    """Return a get_pipeline mock that mirrors the real org-scoped lookup.

    When called with ``organisation_id`` set to this org, a cross-org pipeline
    is invisible (returns None). Without the org filter (pre-fix), it is
    returned so the mutation would proceed.
    """

    def _side_effect(
        session: object,
        pipeline_id: object,
        *,
        organisation_id: object = None,
        include_deleted: bool = False,
        **kwargs: object,
    ) -> MagicMock | None:
        if organisation_id == _ORG_ID:
            return None
        return _wrong_org_pipeline()

    return AsyncMock(side_effect=_side_effect)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()
    mock_get_pipeline = _make_get_pipeline()
    mock_restore = AsyncMock(return_value=_wrong_org_pipeline())
    mock_archive = AsyncMock(return_value=_wrong_org_pipeline())
    mock_unarchive = AsyncMock(return_value=_wrong_org_pipeline())

    global _GET_PIPELINE_MOCK
    _GET_PIPELINE_MOCK = mock_get_pipeline

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[deny_break_glass_mint] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", mock_get_pipeline),
        patch("modulo.api.routes.pipelines.restore_pipeline", mock_restore),
        patch("modulo.api.routes.pipelines.archive_pipeline", mock_archive),
        patch("modulo.api.routes.pipelines.unarchive_pipeline", mock_unarchive),
        patch("modulo.api.routes.pipelines.set_rls_org", AsyncMock()),
        patch("modulo.api.routes.pipelines.set_rls_user_context", AsyncMock()),
    ):
        yield TestClient(app)

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Pipeline archive/restore/unarchive — cross-org deny path
# ---------------------------------------------------------------------------


class TestPipelineArchiveRestoreUnarchiveCrossOrgDeny:
    def test_restore_wrong_org_returns_404(self, client: TestClient) -> None:
        resp = client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/restore")
        assert resp.status_code == 404

    def test_archive_wrong_org_returns_404(self, client: TestClient) -> None:
        resp = client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/archive")
        assert resp.status_code == 404

    def test_unarchive_wrong_org_returns_404(self, client: TestClient) -> None:
        resp = client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/unarchive")
        assert resp.status_code == 404

    def test_get_pipeline_invoked_with_organisation_id(self, client: TestClient) -> None:
        client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/restore")
        assert _GET_PIPELINE_MOCK is not None
        assert any(call.kwargs.get("organisation_id") == _ORG_ID for call in _GET_PIPELINE_MOCK.call_args_list)
