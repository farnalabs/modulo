"""Tests for pipeline_diff_rollback feature gating via require_feature('pipeline_diff_rollback').

The team-tier ``pipeline_diff_rollback`` flag covers "diff-based pipeline
version comparison and rollback" — the snapshot-diff and snapshot-rollback
endpoints. Snapshot listing/detail/creation stay community tier; only the
diff and rollback operations are gated.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session
from tests.unit.api.plan_stubs import all_features, community_features

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_SNAPSHOT_A = uuid.UUID("00000000-0000-0000-0000-000000000011")
_SNAPSHOT_B = uuid.UUID("00000000-0000-0000-0000-000000000012")


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
    return session


def _build_client(plan: object) -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_plan_context] = lambda: plan
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def community_client() -> Generator[TestClient, None, None]:
    yield from _build_client(community_features())


@pytest.fixture
def team_client() -> Generator[TestClient, None, None]:
    yield from _build_client(all_features())


def _diff_url(pipeline_id: uuid.UUID = _PIPELINE_ID) -> str:
    return f"/api/v1/pipelines/{pipeline_id}/snapshots/diff"


def _rollback_url(pipeline_id: uuid.UUID = _PIPELINE_ID, snapshot_id: uuid.UUID = _SNAPSHOT_A) -> str:
    return f"/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}/rollback"


def _assert_feature_402(resp: Any) -> None:
    assert resp.status_code == 402, f"Expected 402, got {resp.status_code}: {resp.text[:200]}"
    assert "pipeline_diff_rollback" in resp.text


def _diff_payload() -> dict[str, str]:
    return {"snapshot_a_id": str(_SNAPSHOT_A), "snapshot_b_id": str(_SNAPSHOT_B)}


def _empty_diff_response() -> dict[str, Any]:
    return {
        "snapshot_a": {"graph": {}},
        "snapshot_b": {"graph": {}},
        "nodes_added": [],
        "nodes_removed": [],
        "nodes_modified": [],
        "edges_added": [],
        "edges_removed": [],
        "edges_modified": [],
    }


# ── Diff / rollback return 402 when the team feature is disabled ──


def test_diff_returns_402_when_disabled(community_client: TestClient) -> None:
    _assert_feature_402(community_client.post(_diff_url(), json=_diff_payload()))


def test_rollback_returns_402_when_disabled(community_client: TestClient) -> None:
    _assert_feature_402(community_client.post(_rollback_url()))


# ── Diff / rollback succeed when the team feature is enabled ──


def test_diff_succeeds_when_enabled(team_client: TestClient) -> None:
    with (
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch("modulo.api.routes.pipelines.diff_snapshots", return_value=_empty_diff_response()),
    ):
        resp = team_client.post(_diff_url(), json=_diff_payload())
    assert resp.status_code == 200, resp.text


def test_rollback_succeeds_when_enabled(team_client: TestClient) -> None:
    new_snapshot = MagicMock()
    new_snapshot.id = _SNAPSHOT_B
    new_snapshot.pipeline_id = _PIPELINE_ID
    new_snapshot.snapshot_version = 2
    new_snapshot.tag = None
    new_snapshot.notes = None
    new_snapshot.created_at = None
    new_snapshot.account_id = _USER_ID
    with (
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch(
            "modulo.api.routes.pipelines.rollback_to_snapshot",
            return_value=new_snapshot,
        ),
    ):
        resp = team_client.post(_rollback_url())
    assert resp.status_code == 200, resp.text


# ── Community tier keeps the non-gated snapshot surface ──


def test_snapshot_list_is_community_tier(community_client: TestClient) -> None:
    """Snapshot listing stays community tier — only diff/rollback are gated."""
    snapshot = MagicMock()
    snapshot.id = _SNAPSHOT_A
    snapshot.pipeline_id = _PIPELINE_ID
    snapshot.snapshot_version = 1
    snapshot.tag = None
    snapshot.notes = None
    snapshot.created_at = None
    snapshot.account_id = _USER_ID

    with (
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=MagicMock()),
        patch("modulo.api.routes.pipelines.list_snapshots", return_value=([snapshot], 1)),
    ):
        resp = community_client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots")
    assert resp.status_code == 200, resp.text
