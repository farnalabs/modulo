"""Real FastAPI dispatch coverage for the remaining API route branches."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.auth.scim_auth import ScimPrincipal, get_scim_plan_context, get_scim_principal
from modulo.core.audit_coverage import audit_session
from modulo.core.feature_flags import DbPlanContext, FeatureFlagRegistry
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_GROUP_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_VALID_32 = "a" * 32
_SCIM_TOKEN = "coverage-scim-token"
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_scim_token=_SCIM_TOKEN,
        modulo_public_url="http://localhost:8000",
    )


def _session(*, execute: object = None, allow_empty_execute: bool = False) -> AsyncMock:
    session = configure_mock_session(AsyncMock(), allow_empty_execute=allow_empty_execute)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    if execute is not None:
        session.execute = AsyncMock(side_effect=execute)
    return session


def _install_common_overrides(session: AsyncMock, *, plan_enabled: bool = True) -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    principal = AuthenticatedPrincipal(
        username="coverage-user",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="admin",
    )
    tenant = TenantPrincipal(
        username="coverage-user",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_user] = lambda: principal
    app.dependency_overrides[get_current_tenant_user] = lambda: tenant
    plan = MagicMock()
    plan.feature_enabled.return_value = plan_enabled
    app.dependency_overrides[get_plan_context] = lambda: plan


def _install_scim_overrides(session: AsyncMock) -> None:
    _install_common_overrides(session)
    app.dependency_overrides[get_scim_plan_context] = lambda: DbPlanContext(FeatureFlagRegistry(current_tier="team"))
    app.dependency_overrides[get_scim_principal] = lambda: ScimPrincipal(organisation_id=_ORG_ID)

    async def audit_override() -> AsyncGenerator[AsyncMock, None]:
        yield _session(allow_empty_execute=True)

    async def audit_principal() -> TenantPrincipal:
        return TenantPrincipal(
            username="scim",
            organisation_id=_ORG_ID,
            account_id=_ACCOUNT_ID,
            org_role="admin",
        )

    app.dependency_overrides[audit_session] = audit_override
    from modulo.api.routes.scim import get_scim_audit_principal

    app.dependency_overrides[get_scim_audit_principal] = audit_principal


@pytest.fixture(autouse=True)
def clear_overrides() -> Generator[None, None, None]:
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


class TestDashboardRouteDispatch:
    @pytest.mark.parametrize(
        ("error", "expected_status"),
        [
            pytest.param(ProgrammingError("stmt", {}, Exception("missing")), 501, id="programming"),
            pytest.param(SQLAlchemyError("database down"), 503, id="sqlalchemy"),
            pytest.param(RuntimeError("unexpected"), 500, id="unexpected"),
        ],
    )
    def test_summary_maps_handler_errors(self, error: Exception, expected_status: int) -> None:
        session = _session(execute=error)
        _install_common_overrides(session)
        response = TestClient(app).get("/api/v1/dashboard/summary")
        assert response.status_code == expected_status

    def test_summary_preserves_invalid_days_response(self) -> None:
        session = _session()
        _install_common_overrides(session)
        response = TestClient(app).get("/api/v1/dashboard/summary?days=0")
        assert response.status_code == 422

    @pytest.mark.parametrize(
        ("path", "error", "expected_status"),
        [
            pytest.param("/api/v1/dashboard/trends?days=7", RuntimeError("unexpected"), 500, id="trends-unexpected"),
            pytest.param(
                "/api/v1/dashboard/daily-run-counts?days=7",
                RuntimeError("unexpected"),
                500,
                id="daily-unexpected",
            ),
        ],
    )
    def test_dashboard_error_paths_return_structured_http_errors(
        self, path: str, error: Exception, expected_status: int
    ) -> None:
        session = _session(execute=error)
        _install_common_overrides(session)
        response = TestClient(app).get(path)
        assert response.status_code == expected_status

    def test_daily_run_counts_dispatches_rows_into_nested_mapping(self) -> None:
        row = MagicMock(day=_NOW.date(), status="complete", cnt=2)
        result = MagicMock()
        result.__iter__ = MagicMock(return_value=iter([row]))
        session = _session(execute=lambda *_args, **_kwargs: result)
        _install_common_overrides(session)
        response = TestClient(app).get("/api/v1/dashboard/daily-run-counts?days=7")
        assert response.status_code == 200
        assert response.json()["daily_counts"]["2025-01-01"]["complete"] == 2

    @pytest.mark.parametrize(
        ("path", "error", "expected_status"),
        [
            pytest.param(
                "/api/v1/dashboard/trends?days=7",
                ProgrammingError("stmt", {}, Exception("missing")),
                501,
                id="trends-programming",
            ),
            pytest.param(
                "/api/v1/dashboard/daily-run-counts?days=7",
                SQLAlchemyError("database down"),
                503,
                id="daily-sqlalchemy",
            ),
        ],
    )
    def test_other_dashboard_routes_map_database_errors(
        self, path: str, error: Exception, expected_status: int
    ) -> None:
        session = _session(execute=error)
        _install_common_overrides(session)
        response = TestClient(app).get(path)
        assert response.status_code == expected_status


class TestOnboardingRouteDispatch:
    def test_status_runs_real_auto_completion_queries(self) -> None:
        progress_result = MagicMock()
        progress_result.scalar_one_or_none.return_value = None
        primitive_result = MagicMock()
        primitive_result.scalar_one_or_none.return_value = MagicMock()
        session = _session()
        session.execute = AsyncMock(
            side_effect=[
                progress_result,
                primitive_result,
                primitive_result,
                primitive_result,
                primitive_result,
                primitive_result,
            ]
        )
        _install_common_overrides(session)
        response = TestClient(app).get("/api/v1/onboarding/status")
        assert response.status_code == 200
        body = response.json()
        assert body["completed_actions"]
        assert body["progress_pct"] == 100.0

    def test_status_creates_progress_when_missing(self) -> None:
        progress_result = MagicMock()
        progress_result.scalar_one_or_none.return_value = None
        empty_result = MagicMock()
        empty_result.scalar_one_or_none.return_value = None
        session = _session()
        session.execute = AsyncMock(
            side_effect=[progress_result, empty_result, empty_result, empty_result, empty_result, empty_result]
        )
        _install_common_overrides(session)
        response = TestClient(app).get("/api/v1/onboarding/status")
        assert response.status_code == 200
        session.add.assert_called_once()

    def test_skip_existing_action_does_not_duplicate_it(self) -> None:
        progress = MagicMock(completed_actions=[], skipped_actions=["login"], dismissed=False)
        progress_result = MagicMock()
        progress_result.scalar_one_or_none.return_value = progress
        session = _session()
        session.execute = AsyncMock(return_value=progress_result)
        _install_common_overrides(session)
        response = TestClient(app).post("/api/v1/onboarding/actions/login/skip")
        assert response.status_code == 200
        assert progress.skipped_actions == ["login"]

    def test_complete_existing_action_removes_skip_marker(self) -> None:
        progress = MagicMock(completed_actions=[], skipped_actions=["login"], dismissed=False)
        progress_result = MagicMock()
        progress_result.scalar_one_or_none.return_value = progress
        session = _session()
        session.execute = AsyncMock(return_value=progress_result)
        _install_common_overrides(session)
        response = TestClient(app).post("/api/v1/onboarding/actions/login/complete")
        assert response.status_code == 200
        assert progress.completed_actions == ["login"]
        assert not progress.skipped_actions

    def test_dismiss_dispatches_and_marks_progress(self) -> None:
        progress = MagicMock(completed_actions=[], skipped_actions=[], dismissed=False)
        progress_result = MagicMock()
        progress_result.scalar_one_or_none.return_value = progress
        session = _session()
        session.execute = AsyncMock(return_value=progress_result)
        _install_common_overrides(session)
        response = TestClient(app).post("/api/v1/onboarding/dismiss")
        assert response.status_code == 200
        assert progress.dismissed is True


class TestScimRouteDispatch:
    def test_list_users_pagination_and_filter_are_forwarded(self) -> None:
        account = MagicMock(
            id=_USER_ID,
            email="user@example.com",
            display_name="User",
            active=True,
            created_at=_NOW,
            updated_at=_NOW,
            kind="human",
        )
        session = _session()
        _install_scim_overrides(session)
        with patch(
            "modulo.api.routes.scim.scim_list_users", new_callable=AsyncMock, return_value=([account], 3)
        ) as listing:
            response = TestClient(app).get(
                "/scim/v2/Users?filter=userName%20eq%20%5C%22user@example.com%5C%22&startIndex=2&count=1",
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert response.status_code == 200
        assert response.json()["startIndex"] == 2
        assert response.json()["totalResults"] == 3
        assert listing.await_args.kwargs["start_index"] == 2
        assert listing.await_args.kwargs["count"] == 1

    @pytest.mark.parametrize(
        ("path", "patch_target"),
        [
            pytest.param("/scim/v2/Users", "modulo.api.routes.scim.scim_list_users", id="users"),
            pytest.param("/scim/v2/Groups", "modulo.api.routes.scim.scim_list_groups", id="groups"),
        ],
    )
    def test_list_routes_map_database_errors(self, path: str, patch_target: str) -> None:
        session = _session()
        _install_scim_overrides(session)
        with patch(patch_target, new_callable=AsyncMock, side_effect=SQLAlchemyError("down")):
            response = TestClient(app).get(path, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        assert response.status_code == 503

    def test_get_user_rejects_service_principal_as_not_found(self) -> None:
        service = MagicMock(id=_USER_ID, kind="service")
        session = _session()
        _install_scim_overrides(session)
        with patch("modulo.api.routes.scim.scim_get_user", new_callable=AsyncMock, return_value=service):
            response = TestClient(app).get(
                f"/scim/v2/Users/{_USER_ID}", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"}
            )
        assert response.status_code == 404

    def test_get_group_not_found_returns_scim_error(self) -> None:
        session = _session()
        _install_scim_overrides(session)
        with patch("modulo.api.routes.scim.scim_get_group", new_callable=AsyncMock, return_value=None):
            response = TestClient(app).get(
                f"/scim/v2/Groups/{_GROUP_ID}", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"}
            )
        assert response.status_code == 404

    def test_get_group_maps_database_error(self) -> None:
        session = _session()
        _install_scim_overrides(session)
        with patch(
            "modulo.api.routes.scim.scim_get_group", new_callable=AsyncMock, side_effect=SQLAlchemyError("down")
        ):
            response = TestClient(app).get(
                f"/scim/v2/Groups/{_GROUP_ID}", headers={"Authorization": f"Bearer {_SCIM_TOKEN}"}
            )
        assert response.status_code == 503

    def test_patch_group_invalid_operation_is_scim_error(self) -> None:
        group = MagicMock(id=_GROUP_ID, name="Engineering", created_at=_NOW, updated_at=_NOW)
        session = _session()
        _install_scim_overrides(session)
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace-me", "value": {}}],
        }
        with patch("modulo.api.routes.scim.scim_get_group", new_callable=AsyncMock, return_value=group):
            response = TestClient(app).patch(
                f"/scim/v2/Groups/{_GROUP_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert response.status_code == 400

    def test_patch_group_remove_path_dispatches_member_removal(self) -> None:
        group = MagicMock(id=_GROUP_ID, name="Engineering", created_at=_NOW, updated_at=_NOW)
        session = _session(allow_empty_execute=True)
        _install_scim_overrides(session)
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {"op": "remove", "path": f'members[value eq "{_USER_ID}"]'},
            ],
        }
        with (
            patch("modulo.api.routes.scim.scim_get_group", new_callable=AsyncMock, return_value=group),
            patch("modulo.api.routes.scim.scim_remove_group_member", new_callable=AsyncMock) as remove_member,
            patch("modulo.api.routes.scim.scim_list_group_members", new_callable=AsyncMock, return_value=[]),
        ):
            response = TestClient(app).patch(
                f"/scim/v2/Groups/{_GROUP_ID}",
                json=body,
                headers={"Authorization": f"Bearer {_SCIM_TOKEN}"},
            )
        assert response.status_code == 200
        remove_member.assert_awaited_once_with(session, _GROUP_ID, _USER_ID)

    @pytest.mark.parametrize(
        ("path", "patch_target"),
        [
            pytest.param(
                f"/scim/v2/Users/{_USER_ID}",
                "modulo.api.routes.scim.scim_get_user",
                id="replace-user",
            ),
            pytest.param(
                f"/scim/v2/Groups/{_GROUP_ID}",
                "modulo.api.routes.scim.scim_get_group",
                id="replace-group",
            ),
        ],
    )
    def test_replace_routes_map_programming_errors(self, path: str, patch_target: str) -> None:
        session = _session()
        _install_scim_overrides(session)
        body = {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": "user@example.com"}
        if "Groups" in path:
            body = {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"], "displayName": "Engineering"}
        with patch(
            patch_target, new_callable=AsyncMock, side_effect=ProgrammingError("stmt", {}, Exception("missing"))
        ):
            response = TestClient(app).put(path, json=body, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        assert response.status_code == 501


class TestMeRouteDispatch:
    def test_settings_hides_service_principal(self) -> None:
        session = _session()
        _install_common_overrides(session)
        account = MagicMock(kind="service")
        with patch("modulo.api.routes.me.get_account_by_id", new_callable=AsyncMock, return_value=account):
            response = TestClient(app).get("/api/v1/me/settings")
        assert response.status_code == 404

    def test_update_settings_with_all_optional_fields_uses_both_values(self) -> None:
        session = _session()
        _install_common_overrides(session)
        with patch(
            "modulo.api.routes.me.update_account_preferences",
            new_callable=AsyncMock,
            return_value={"theme": "dark", "locale": "en-GB"},
        ) as update:
            response = TestClient(app).put("/api/v1/me/settings", json={"theme": "dark", "locale": "en-GB"})
        assert response.status_code == 200
        assert update.await_args.args[2] == {"theme": "dark", "locale": "en-GB"}

    def test_password_change_aborts_when_blacklisting_fails(self) -> None:
        session = _session()
        _install_common_overrides(session)
        account = MagicMock(password_hash="hash", must_change_password=True, kind="human")
        family = MagicMock(family_id="family-1")
        with (
            patch("modulo.api.routes.me.get_account_by_id", new_callable=AsyncMock, return_value=account),
            patch("modulo.api.routes.me.verify_password", return_value=True),
            patch("modulo.api.routes.me.validate_password_strength"),
            patch("modulo.api.routes.me.hash_password", return_value="new-hash"),
            patch("modulo.api.routes.me.list_families_for_account", new_callable=AsyncMock, return_value=[family]),
            patch("modulo.api.routes.me.blacklist_family", new_callable=AsyncMock, side_effect=SQLAlchemyError("down")),
        ):
            response = TestClient(app).put(
                "/api/v1/me/password",
                json={"current_password": "old-pass", "new_password": "new-pass-123"},
            )
        assert response.status_code == 503

    def test_password_change_audit_failure_does_not_change_success_response(self) -> None:
        session = _session()
        _install_common_overrides(session)
        account = MagicMock(password_hash="hash", must_change_password=True, kind="human")
        with (
            patch("modulo.api.routes.me.get_account_by_id", new_callable=AsyncMock, return_value=account),
            patch("modulo.api.routes.me.verify_password", return_value=True),
            patch("modulo.api.routes.me.validate_password_strength"),
            patch("modulo.api.routes.me.hash_password", return_value="new-hash"),
            patch("modulo.api.routes.me.list_families_for_account", new_callable=AsyncMock, return_value=[]),
            patch(
                "modulo.core.audit_logger.append_audit_event",
                new_callable=AsyncMock,
                side_effect=RuntimeError("audit"),
            ),
        ):
            response = TestClient(app).put(
                "/api/v1/me/password",
                json={"current_password": "old-pass", "new_password": "new-pass-123"},
            )
        assert response.status_code == 200
        assert account.must_change_password is False

    def test_update_skill_applies_each_optional_field(self) -> None:
        session = _session()
        _install_common_overrides(session)
        skill = MagicMock(
            id=uuid.uuid4(),
            name="old",
            description=None,
            triggers=[],
            body="old",
            active=True,
            created_at=None,
            updated_at=None,
        )
        with patch("modulo.api.routes.me.get_user_skill_or_404", new_callable=AsyncMock, return_value=skill):
            response = TestClient(app).put(
                f"/api/v1/me/assistant/skills/{skill.id}",
                json={"name": "new", "description": "desc", "triggers": ["ask"], "body": "new body", "active": False},
            )
        assert response.status_code == 200
        assert skill.name == "new"
        assert skill.description == "desc"
        assert skill.triggers == ["ask"]
        assert skill.body == "new body"
        assert skill.active is False
