"""Tests for snapshot route error handling — ProgrammingError→501, SQLAlchemyError→503."""

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

os.environ.setdefault("MODULO_CSRF_ENABLED", "false")

from modulo.api.dependencies import get_db_session  # noqa: E402
from modulo.api.main import app  # noqa: E402
from modulo.auth.dependencies import get_current_user  # noqa: E402
from modulo.auth.jwt import AuthenticatedPrincipal  # noqa: E402
from modulo.settings import Settings, get_settings  # noqa: E402

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="user",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestSnapshotListProgrammingError:
    @patch(
        "modulo.api.routes.pipelines.list_snapshots",
        new=AsyncMock(side_effect=ProgrammingError("mock", {}, "")),
    )
    def test_list_snapshots_returns_501(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    @patch(
        "modulo.api.routes.pipelines.list_snapshots",
        new=AsyncMock(side_effect=SQLAlchemyError("connection failed")),
    )
    def test_list_snapshots_returns_503(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots")
        assert resp.status_code == 503


class TestSnapshotGetDetailProgrammingError:
    @patch(
        "modulo.api.routes.pipelines.get_snapshot_detail",
        new=AsyncMock(side_effect=ProgrammingError("mock", {}, "")),
    )
    def test_get_snapshot_detail_returns_501(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAPSHOT_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    @patch(
        "modulo.api.routes.pipelines.get_snapshot_detail",
        new=AsyncMock(side_effect=SQLAlchemyError("connection failed")),
    )
    def test_get_snapshot_detail_returns_503(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAPSHOT_ID}")
        assert resp.status_code == 503


class TestSnapshotTagProgrammingError:
    @patch(
        "modulo.api.routes.pipelines.tag_snapshot",
        new=AsyncMock(side_effect=ProgrammingError("mock", {}, "")),
    )
    def test_tag_snapshot_returns_501(self, client: TestClient) -> None:
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAPSHOT_ID}",
            json={"tag": "test"},
        )
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    @patch(
        "modulo.api.routes.pipelines.tag_snapshot",
        new=AsyncMock(side_effect=SQLAlchemyError("connection failed")),
    )
    def test_tag_snapshot_returns_503(self, client: TestClient) -> None:
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAPSHOT_ID}",
            json={"tag": "test"},
        )
        assert resp.status_code == 503


class TestSnapshotRollbackProgrammingError:
    @patch(
        "modulo.api.routes.pipelines.rollback_to_snapshot",
        new=AsyncMock(side_effect=ProgrammingError("mock", {}, "")),
    )
    def test_rollback_snapshot_returns_501(self, client: TestClient) -> None:
        resp = client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAPSHOT_ID}/rollback")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    @patch(
        "modulo.api.routes.pipelines.rollback_to_snapshot",
        new=AsyncMock(side_effect=SQLAlchemyError("connection failed")),
    )
    def test_rollback_snapshot_returns_503(self, client: TestClient) -> None:
        resp = client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAPSHOT_ID}/rollback")
        assert resp.status_code == 503


class TestSnapshotDeleteProgrammingError:
    @patch(
        "modulo.api.routes.pipelines.delete_snapshot",
        new=AsyncMock(side_effect=ProgrammingError("mock", {}, "")),
    )
    def test_delete_snapshot_returns_501(self, client: TestClient) -> None:
        resp = client.delete(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAPSHOT_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    @patch(
        "modulo.api.routes.pipelines.delete_snapshot",
        new=AsyncMock(side_effect=SQLAlchemyError("connection failed")),
    )
    def test_delete_snapshot_returns_503(self, client: TestClient) -> None:
        resp = client.delete(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAPSHOT_ID}")
        assert resp.status_code == 503


class TestSnapshotDiffProgrammingError:
    @patch(
        "modulo.api.routes.pipelines.diff_snapshots",
        new=AsyncMock(side_effect=ProgrammingError("mock", {}, "")),
    )
    def test_diff_snapshots_returns_501(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/diff",
            json={"snapshot_a_id": str(_SNAPSHOT_ID), "snapshot_b_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    @patch(
        "modulo.api.routes.pipelines.diff_snapshots",
        new=AsyncMock(side_effect=SQLAlchemyError("connection failed")),
    )
    def test_diff_snapshots_returns_503(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/diff",
            json={"snapshot_a_id": str(_SNAPSHOT_ID), "snapshot_b_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 503
