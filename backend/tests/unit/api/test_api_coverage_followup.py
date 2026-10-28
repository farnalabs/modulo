"""Additional route-dispatch coverage for the API coverage batch."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes import dashboard as dashboard_route
from modulo.api.routes import onboarding as onboarding_route
from modulo.api.routes import scim as scim_route
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.auth.scim_auth import get_scim_plan_context, get_scim_principal
from modulo.core.audit_coverage import audit_session
from modulo.core.feature_flags import DbPlanContext, FeatureFlagRegistry
from modulo.db.models.onboarding_progress import OnboardingProgress
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_SCIM_TOKEN = "coverage-scim-token"


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="team-license",
        modulo_scim_token=_SCIM_TOKEN,
        modulo_public_url="http://localhost:8000/",
    )


def _session() -> AsyncMock:
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def route_client() -> Generator[TestClient, None, None]:
    session = _session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    principal = TenantPrincipal(username="coverage", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin")
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_tenant_user] = lambda: principal
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="coverage", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    plan = MagicMock()
    plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: plan
    app.dependency_overrides[get_scim_plan_context] = lambda: DbPlanContext(FeatureFlagRegistry(current_tier="team"))
    app.dependency_overrides[get_scim_principal] = lambda: scim_route.ScimPrincipal(organisation_id=_ORG_ID)
    app.dependency_overrides[scim_route.get_scim_audit_principal] = lambda: principal

    async def audit_override() -> AsyncGenerator[AsyncMock, None]:
        yield _session()

    app.dependency_overrides[audit_session] = audit_override
    client = TestClient(app)
    client.session = session  # type: ignore[attr-defined]
    yield client
    app.dependency_overrides.clear()


class TestDashboardCoverage:
    @pytest.mark.parametrize("value", [object(), None, "bad", b"bad"])
    def test_safe_float_default(self, value: object) -> None:
        assert dashboard_route._safe_float(value, 4.5) == 4.5

    @pytest.mark.parametrize(
        ("path", "exc", "expected"),
        [
            ("/api/v1/dashboard/summary", ProgrammingError("s", {}, Exception()), 501),
            ("/api/v1/dashboard/summary", SQLAlchemyError(), 503),
            ("/api/v1/dashboard/summary", RuntimeError(), 500),
            ("/api/v1/dashboard/trends", ProgrammingError("s", {}, Exception()), 501),
            ("/api/v1/dashboard/trends", SQLAlchemyError(), 503),
            ("/api/v1/dashboard/trends", RuntimeError(), 500),
            ("/api/v1/dashboard/daily-run-counts", ProgrammingError("s", {}, Exception()), 501),
            ("/api/v1/dashboard/daily-run-counts", SQLAlchemyError(), 503),
            ("/api/v1/dashboard/daily-run-counts", RuntimeError(), 500),
        ],
        ids=[
            "summary-501",
            "summary-503",
            "summary-500",
            "trends-501",
            "trends-503",
            "trends-500",
            "daily-501",
            "daily-503",
            "daily-500",
        ],
    )
    def test_route_errors(self, route_client: TestClient, path: str, exc: Exception, expected: int) -> None:
        route_client.session.execute = AsyncMock(side_effect=exc)  # type: ignore[attr-defined]
        assert route_client.get(path).status_code == expected

    async def test_eval_stats_nonempty_rows(self) -> None:
        session = AsyncMock()
        rows = [MagicMock(owner_team_id=_ORG_ID, pipeline_id=_USER_ID, total=2, passed=1)]
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(one=MagicMock(return_value=MagicMock(total=2, passed=1))),
                MagicMock(all=MagicMock(return_value=rows)),
            ]
        )
        result, per_team = await dashboard_route._load_eval_stats(session, _ORG_ID)
        assert result is not None
        assert per_team[str(_ORG_ID)]["total_evals"] == 2


class TestOnboardingCoverage:
    def test_zero_actions_progress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(onboarding_route, "_ONBOARDING_ACTIONS", [])
        assert onboarding_route._compute_progress_pct([], []) == 100.0

    async def test_create_progress_and_auto_complete(self) -> None:
        session = _session()
        results = [MagicMock(scalar_one_or_none=MagicMock(return_value=None))]
        results.extend(MagicMock(scalar_one_or_none=MagicMock(return_value=object())) for _ in range(5))
        session.execute = AsyncMock(side_effect=results)
        progress = await onboarding_route._get_or_create_progress(session, _ORG_ID)
        completed = await onboarding_route._check_auto_completion(session, _ORG_ID)
        assert progress.organisation_id == _ORG_ID
        assert len(completed) == 6

    def test_duplicate_action_paths(self, route_client: TestClient) -> None:
        progress = OnboardingProgress(
            organisation_id=_ORG_ID,
            completed_actions=["login"],
            skipped_actions=["add_ai_model"],
            dismissed=False,
        )
        with patch.object(onboarding_route, "_get_or_create_progress", new=AsyncMock(return_value=progress)):
            assert route_client.post("/api/v1/onboarding/actions/login/complete").status_code == 200
            assert route_client.post("/api/v1/onboarding/actions/add_ai_model/skip").status_code == 200


class TestScimCoverage:
    def test_human_guard_hides_service_account(self) -> None:
        with patch.object(scim_route, "is_service_account", return_value=True), pytest.raises(HTTPException) as info:
            scim_route._require_human_scim_user(MagicMock(), _USER_ID)
        assert info.value.status_code == 404

    def test_member_uuid_shapes(self) -> None:
        assert scim_route._iter_member_uuids({"value": str(_USER_ID)}) == [_USER_ID]
        assert scim_route._iter_member_uuids([{"value": str(_USER_ID)}, {"value": "bad"}]) == [_USER_ID]
        assert scim_route._parse_member_uuid(None) is None

    @pytest.mark.parametrize(
        ("path", "method", "target"),
        [
            ("/scim/v2/Users", "get", "scim_list_users"),
            ("/scim/v2/Groups", "get", "scim_list_groups"),
            (f"/scim/v2/Users/{_USER_ID}", "get", "scim_get_user"),
            (f"/scim/v2/Groups/{_USER_ID}", "get", "scim_get_group"),
            (f"/scim/v2/Groups/{_USER_ID}", "delete", "scim_delete_group_by_id"),
        ],
    )
    def test_sqlalchemy_errors(self, route_client: TestClient, path: str, method: str, target: str) -> None:
        with patch.object(scim_route, target, side_effect=SQLAlchemyError("db")):
            response = getattr(route_client, method)(path, headers={"Authorization": f"Bearer {_SCIM_TOKEN}"})
        assert response.status_code == 503

    async def test_group_member_helper_variants(self) -> None:
        session = AsyncMock()
        group = MagicMock(id=_USER_ID)
        with patch.multiple(
            scim_route,
            scim_list_group_members=AsyncMock(return_value=[]),
            scim_update_group=AsyncMock(),
            scim_get_user=AsyncMock(return_value=MagicMock()),
            scim_add_group_member=AsyncMock(),
            scim_remove_group_member=AsyncMock(),
        ):
            await scim_route._apply_replace_group_op(
                session,
                group,
                scim_route.ScimPatchOperation(
                    op="replace", value={"displayName": "Renamed", "members": {"value": str(_USER_ID)}}
                ),
                _ORG_ID,
            )
            await scim_route._apply_add_group_op(
                session, group, scim_route.ScimPatchOperation(op="add", value={"value": str(_USER_ID)}), _ORG_ID
            )
            await scim_route._apply_remove_group_op(
                session, group, scim_route.ScimPatchOperation(op="remove", path=f'members[value eq "{_USER_ID}"]')
            )
        assert group.id == _USER_ID


class TestMeCoverage:
    def test_service_account_hidden(self) -> None:
        from modulo.api.routes.me import _require_human_account

        with patch("modulo.api.routes.me.is_service_account", return_value=True), pytest.raises(HTTPException) as info:
            _require_human_account(MagicMock())
        assert info.value.status_code == 404

    def test_invalid_context_mode_is_422(self, route_client: TestClient) -> None:
        response = route_client.put(
            "/api/v1/me/assistant/context-sources/page_context",
            json={"source_mode": "invalid"},
        )
        assert response.status_code == 422

    def test_locale_only_settings(self, route_client: TestClient) -> None:
        with patch(
            "modulo.api.routes.me.update_account_preferences",
            new_callable=AsyncMock,
            return_value={"locale": "en-GB"},
        ) as update:
            response = route_client.put("/api/v1/me/settings", json={"locale": "en-GB"})
        assert response.status_code == 200
        assert update.call_args.args[2] == {"locale": "en-GB"}
