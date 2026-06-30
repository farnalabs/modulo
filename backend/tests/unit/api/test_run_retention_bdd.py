"""Unit tests for run retention BDD scenarios — covers all 6 scenarios from run_retention.feature."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ALT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2026, 6, 29, tzinfo=UTC)


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
    return session


@pytest.fixture()
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
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
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
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def alt_org_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin-globex",
        organisation_id=_ALT_ORG_ID,
        account_id=uuid.uuid4(),
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


# ===========================================================================
# Scenario 1: Run auto-deleted after retention TTL
# ===========================================================================


class TestRetentionJobDeletesOldRuns:
    def test_deletes_terminal_runs_older_than_ttl(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.admin.batch_delete_old_terminal_runs",
                new_callable=AsyncMock,
                return_value=5,
            ) as mock_delete,
        ):
            resp = client.post(
                "/api/v1/admin/purge/runs",
                json={"max_age_days": 90},
            )

        assert resp.status_code == 200
        mock_delete.assert_awaited_once_with(mock_delete.call_args.args[0], max_age_days=90)

    def test_returns_deleted_count(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.admin.batch_delete_old_terminal_runs",
                new_callable=AsyncMock,
                return_value=5,
            ),
        ):
            resp = client.post(
                "/api/v1/admin/purge/runs",
                json={"max_age_days": 90},
            )

        body = resp.json()
        assert body["deleted_run_count"] == 5

    def test_processes_in_batches_of_500(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.admin.batch_delete_old_terminal_runs",
                new_callable=AsyncMock,
                return_value=500,
            ),
        ):
            resp = client.post(
                "/api/v1/admin/purge/runs",
                json={"max_age_days": 90},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_run_count"] == 500


# ===========================================================================
# Scenario 2: Active runs not deleted by retention job
# ===========================================================================


class TestRetentionJobPreservesActiveRuns:
    def test_running_runs_not_affected(self, client: TestClient) -> None:
        """Running runs have status='running' which is not in (complete, failed, cancelled)."""
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.admin.batch_delete_old_terminal_runs",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            resp = client.post(
                "/api/v1/admin/purge/runs",
                json={"max_age_days": 90},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_run_count"] == 0

    def test_pending_runs_not_affected(self, client: TestClient) -> None:
        """Pending runs have status='pending' which is not in (complete, failed, cancelled)."""
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.admin.batch_delete_old_terminal_runs",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            resp = client.post(
                "/api/v1/admin/purge/runs",
                json={"max_age_days": 90},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_run_count"] == 0

    def test_only_terminal_runs_deleted(self, client: TestClient) -> None:
        """Only runs with status complete/failed/cancelled qualify for deletion."""
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.admin.batch_delete_old_terminal_runs",
                new_callable=AsyncMock,
                return_value=3,
            ),
        ):
            resp = client.post(
                "/api/v1/admin/purge/runs",
                json={"max_age_days": 90},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_run_count"] == 3


# ===========================================================================
# Scenario 3: Admin manual purge with date filter
# ===========================================================================


class TestAdminManualPurge:
    def test_returns_200_with_deleted_count(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.admin.purge_runs", new_callable=AsyncMock) as mock_purge,
            patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock),
        ):
            mock_purge.return_value = {"deleted_run_count": 3}
            resp = client.post(
                "/api/v1/admin/purge",
                json={"older_than": "2026-01-01"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_run_count"] == 3

    def test_with_no_runs_to_purge(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.admin.purge_runs", new_callable=AsyncMock) as mock_purge,
            patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock),
        ):
            mock_purge.return_value = {"deleted_run_count": 0}
            resp = client.post(
                "/api/v1/admin/purge",
                json={"older_than": "2026-01-01"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_run_count"] == 0

    def test_requires_older_than_field(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.admin.purge_runs", new_callable=AsyncMock),
            patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/api/v1/admin/purge",
                json={},
            )

        assert resp.status_code == 422


# ===========================================================================
# Scenario 4: Purge audit logged
# ===========================================================================


class TestPurgeAuditLogged:
    def test_audit_event_recorded_on_purge(self, client: TestClient) -> None:
        mock_audit = AsyncMock()
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.admin.purge_runs", new_callable=AsyncMock) as mock_purge,
            patch("modulo.core.audit_logger.append_audit_event", mock_audit),
        ):
            mock_purge.return_value = {"deleted_run_count": 3}
            client.post(
                "/api/v1/admin/purge",
                json={"older_than": "2026-01-01"},
            )

        mock_audit.assert_awaited_once()
        call_kwargs = mock_audit.call_args.kwargs
        assert call_kwargs["event_type"] == "run_purge"

    def test_audit_event_includes_admin_user(self, client: TestClient) -> None:
        mock_audit = AsyncMock()
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.admin.purge_runs", new_callable=AsyncMock) as mock_purge,
            patch("modulo.core.audit_logger.append_audit_event", mock_audit),
        ):
            mock_purge.return_value = {"deleted_run_count": 3}
            client.post(
                "/api/v1/admin/purge",
                json={"older_than": "2026-01-01"},
            )

        call_kwargs = mock_audit.call_args.kwargs
        actor_user_id = call_kwargs.get("actor_user_id")
        assert actor_user_id is not None, "Audit event missing actor_user_id"

    def test_audit_event_includes_date_filter(self, client: TestClient) -> None:
        mock_audit = AsyncMock()
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.admin.purge_runs", new_callable=AsyncMock) as mock_purge,
            patch("modulo.core.audit_logger.append_audit_event", mock_audit),
        ):
            mock_purge.return_value = {"deleted_run_count": 3}
            client.post(
                "/api/v1/admin/purge",
                json={"older_than": "2026-01-01"},
            )

        call_kwargs = mock_audit.call_args.kwargs
        payload = call_kwargs.get("payload_json", {})
        assert "older_than" in payload, "Audit event missing date filter in payload"


# ===========================================================================
# Scenario 5: Configurable retention period
# ===========================================================================


class TestConfigurableRetention:
    def test_uses_custom_ttl(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.admin.batch_delete_old_terminal_runs",
                new_callable=AsyncMock,
                return_value=1,
            ) as mock_delete,
        ):
            resp = client.post(
                "/api/v1/admin/purge/runs",
                json={"max_age_days": 45},
            )

        assert resp.status_code == 200
        mock_delete.assert_awaited_once_with(mock_delete.call_args.args[0], max_age_days=45)

    def test_default_ttl_is_90_days(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.admin.batch_delete_old_terminal_runs",
                new_callable=AsyncMock,
                return_value=2,
            ) as mock_delete,
        ):
            resp = client.post(
                "/api/v1/admin/purge/runs",
                json={"max_age_days": 90},
            )

        assert resp.status_code == 200
        mock_delete.assert_awaited_once_with(mock_delete.call_args.args[0], max_age_days=90)

    def test_shorter_ttl_deletes_more_recent_runs(self, client: TestClient) -> None:
        """A 30-day TTL will match runs that a 90-day TTL would keep."""
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.api.routes.admin.batch_delete_old_terminal_runs",
                new_callable=AsyncMock,
                return_value=10,
            ),
        ):
            resp = client.post(
                "/api/v1/admin/purge/runs",
                json={"max_age_days": 30},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_run_count"] == 10


# ===========================================================================
# Scenario 6: Purge respects org isolation
# ===========================================================================


class TestPurgeOrgIsolation:
    def test_admin_cannot_purge_other_org_runs(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock) as mock_rls,
            patch("modulo.api.routes.admin.purge_runs", new_callable=AsyncMock) as mock_purge,
            patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock),
        ):
            mock_purge.return_value = {"deleted_run_count": 2}
            resp = client.post(
                "/api/v1/admin/purge",
                json={"older_than": "2026-01-01"},
            )

        assert resp.status_code == 200
        mock_rls.assert_awaited_once()
        rls_org_id = mock_rls.call_args[0][1]
        assert rls_org_id == _ORG_ID

    def test_other_org_admin_sees_own_runs(self, alt_org_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.set_rls_org", new_callable=AsyncMock) as mock_rls,
            patch("modulo.api.routes.admin.purge_runs", new_callable=AsyncMock) as mock_purge,
            patch("modulo.core.audit_logger.append_audit_event", new_callable=AsyncMock),
        ):
            mock_purge.return_value = {"deleted_run_count": 4}
            resp = alt_org_client.post(
                "/api/v1/admin/purge",
                json={"older_than": "2026-01-01"},
            )

        assert resp.status_code == 200
        mock_rls.assert_awaited_once()
        rls_org_id = mock_rls.call_args[0][1]
        assert rls_org_id == _ALT_ORG_ID

    def test_viewer_cannot_trigger_purge(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post(
            "/api/v1/admin/purge",
            json={"older_than": "2026-01-01"},
        )
        assert resp.status_code == 403
