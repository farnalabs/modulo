"""Router-level unit tests for /api/v1/runners (status aggregate + apply-template).

Exercises the D5 Runners-page read model in isolation: probe-cache rows ->
strip-state mapping, worst-of aggregation, per-profile health + drift,
the concurrency contract (D3b reader), and the engine-resource preflight â€”
all with a mocked session + CRUD layer. No synchronous engine probe ever
runs on the request path (the endpoint only reads the cache).
"""

import contextlib
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.db.bundled_runner_template import BUNDLED_RUNNER_IMAGE_REF, TEMPLATE_CONFIG_JSON
from modulo.db.crud.base import PageResult
from modulo.db.crud.runner_probe import PROBE_STALENESS_THRESHOLD_SECONDS
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PROFILE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")

_ROUTES = "modulo.api.routes.runners"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
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
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="tenant", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
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


def _probe_row(
    *,
    engine_reachable: bool = True,
    images_present: bool | None = True,
    seconds_ago: int = 30,
    engine_info: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        machine_id="machine-1",
        engine_reachable=engine_reachable,
        images_present=images_present,
        probed_at=datetime.now(UTC) - timedelta(seconds=seconds_ago),
        engine_info_json=engine_info or {},
        image_checks_json={},
        probe_error=None,
    )


def _profile_row(
    *,
    provider_type: str = "runner_docker",
    image_ref: str | None = BUNDLED_RUNNER_IMAGE_REF,
    config_json: dict[str, Any] | None = None,
) -> MagicMock:
    p = MagicMock()
    p.id = _PROFILE_ID
    p.organisation_id = _ORG_ID
    p.name = "Bundled Runner (Docker)"
    p.description = "seeded"
    p.provider_type = provider_type
    p.image_ref = image_ref
    p.capabilities_json = []
    p.config_json = config_json if config_json is not None else dict(TEMPLATE_CONFIG_JSON)
    p.network_policy = "outbound"
    p.initialisation_strategy = "git_clone"
    p.secret_refs_json = []
    p.persistence_policy = "ephemeral"
    p.status = "active"
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


def _patch_status(
    *,
    rows: list[SimpleNamespace],
    profiles: list[MagicMock] | None = None,
    cap: int | None = None,
    is_default: bool = False,
):
    contract = SimpleNamespace(cap=cap, is_default=is_default)
    return (
        patch(f"{_ROUTES}.list_runner_probe_cache", new=AsyncMock(return_value=rows)),
        patch(
            f"{_ROUTES}.list_environment_profiles",
            new=AsyncMock(
                return_value=PageResult(items=profiles or [], total=len(profiles or []), page=1, page_size=100)
            ),
        ),
        patch(f"{_ROUTES}.get_sandbox_concurrency_limit", new=AsyncMock(return_value=contract)),
        patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
        patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
    )


class TestRunnersStatus:
    URL = "/api/v1/runners/status"

    def test_unauthenticated_is_rejected(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code in (401, 403)

    def test_healthy_probe_maps_to_green(self, client: TestClient) -> None:
        with _enter_patchers(_patch_status(rows=[_probe_row()])):
            resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["aggregate_state"] == "healthy"
        assert len(data["machines"]) == 1
        assert data["machines"][0]["state"] == "healthy"
        assert data["machines"][0]["age_seconds"] >= 0

    def test_dead_probe_reads_stale_with_threshold_surfaced(self, client: TestClient) -> None:
        """A suspended probe renders 'status unknown', never healthy (D5)."""
        with _enter_patchers(_patch_status(rows=[_probe_row(seconds_ago=PROBE_STALENESS_THRESHOLD_SECONDS + 60)])):
            resp = client.get(self.URL)
        data = resp.json()
        assert data["aggregate_state"] == "stale"
        assert data["machines"][0]["state"] == "stale"
        assert data["staleness_threshold_seconds"] == PROBE_STALENESS_THRESHOLD_SECONDS

    def test_no_probe_rows_at_all_reads_stale(self, client: TestClient) -> None:
        with _enter_patchers(_patch_status(rows=[])):
            resp = client.get(self.URL)
        assert resp.json()["aggregate_state"] == "stale"

    def test_engine_unreachable_worst_of_wins(self, client: TestClient) -> None:
        rows = [
            _probe_row(engine_reachable=True, images_present=True),
            _probe_row(engine_reachable=False, images_present=None, seconds_ago=31),
        ]
        with _enter_patchers(_patch_status(rows=rows)):
            resp = client.get(self.URL)
        assert resp.json()["aggregate_state"] == "engine_unreachable"

    def test_image_not_pulled_maps_to_warning_state(self, client: TestClient) -> None:
        with _enter_patchers(_patch_status(rows=[_probe_row(images_present=False)])):
            resp = client.get(self.URL)
        data = resp.json()
        assert data["aggregate_state"] == "image_not_pulled"
        assert data["machines"][0]["images_present"] is False


class TestRunnersStatusProfiles:
    URL = "/api/v1/runners/status"

    def test_runner_docker_profile_hides_when_unreachable(self, client: TestClient) -> None:
        profile = _profile_row()
        rows = [_probe_row(engine_reachable=False, images_present=None)]
        with _enter_patchers(_patch_status(rows=rows, profiles=[profile])):
            resp = client.get(self.URL)
        data = resp.json()
        runner = data["profiles"][0]
        assert runner["health_state"] == "engine_unreachable"
        assert runner["available"] is False
        assert runner["drift"]["is_seeded"] is True

    def test_e2b_profile_has_no_engine_health_state(self, client: TestClient) -> None:
        profile = _profile_row(provider_type="e2b", image_ref=None)
        with _enter_patchers(_patch_status(rows=[_probe_row()], profiles=[profile])):
            resp = client.get(self.URL)
        runner = resp.json()["profiles"][0]
        assert runner["health_state"] is None
        assert runner["available"] is True

    def test_seeded_profile_with_current_template_reports_no_drift(self, client: TestClient) -> None:
        profile = _profile_row()
        with _enter_patchers(_patch_status(rows=[_probe_row()], profiles=[profile])):
            resp = client.get(self.URL)
        drift = resp.json()["profiles"][0]["drift"]
        assert drift["is_seeded"] is True
        assert drift["drifted"] is False
        assert not drift["drifted_fields"]

    def test_drifted_template_fields_surface(self, client: TestClient) -> None:
        drifted_config = dict(TEMPLATE_CONFIG_JSON)
        drifted_config["memory_mb"] = 4096
        profile = _profile_row(image_ref="modulo-runner:old@sha256:" + "2" * 64, config_json=drifted_config)
        with _enter_patchers(_patch_status(rows=[_probe_row()], profiles=[profile])):
            resp = client.get(self.URL)
        drift = resp.json()["profiles"][0]["drift"]
        assert drift["drifted"] is True
        assert "image_ref" in drift["drifted_fields"]
        assert "config_json.memory_mb" in drift["drifted_fields"]

    def test_placeholder_digest_profile_is_unavailable(self, client: TestClient) -> None:
        profile = _profile_row(image_ref="modulo-runner:opencode@sha256:" + "0" * 64)
        with _enter_patchers(_patch_status(rows=[_probe_row()], profiles=[profile])):
            resp = client.get(self.URL)
        runner = resp.json()["profiles"][0]
        assert runner["placeholder_digest"] is True
        assert runner["available"] is False


class TestRunnersStatusConcurrency:
    URL = "/api/v1/runners/status"

    def test_absent_key_reads_default_cap_of_4(self, client: TestClient) -> None:
        """D3b: the ABSENT key is the Docker-tier default 4 (is_default=True)."""
        with _enter_patchers(_patch_status(rows=[], cap=4, is_default=True)):
            resp = client.get(self.URL)
        conc = resp.json()["concurrency"]
        assert conc["sandbox_concurrency_limit"] == 4
        assert conc["is_default"] is True

    def test_null_cap_reads_uncapped_preflight(self, client: TestClient) -> None:
        with _enter_patchers(_patch_status(rows=[], cap=None)):
            resp = client.get(self.URL)
        pre = resp.json()["concurrency"]["preflight"]
        assert pre["state"] == "uncapped"

    def test_preflight_ok_when_headroom_sufficient(self, client: TestClient) -> None:
        rows = [_probe_row(engine_info={"cpu_count": 8, "mem_total_mb": 16384})]
        with _enter_patchers(_patch_status(rows=rows, cap=4)):
            resp = client.get(self.URL)
        pre = resp.json()["concurrency"]["preflight"]
        assert pre["state"] == "ok"
        assert pre["needed_cpu"] == 4.0
        assert pre["needed_mem_mb"] == 4096

    def test_preflight_warns_when_cap_exceeds_engine(self, client: TestClient) -> None:
        rows = [_probe_row(engine_info={"cpu_count": 2, "mem_total_mb": 2048})]
        with _enter_patchers(_patch_status(rows=rows, cap=4)):
            resp = client.get(self.URL)
        pre = resp.json()["concurrency"]["preflight"]
        assert pre["state"] == "exceeds_cpu_and_mem"

    def test_preflight_unknown_without_engine_info(self, client: TestClient) -> None:
        with _enter_patchers(_patch_status(rows=[_probe_row()], cap=4)):
            resp = client.get(self.URL)
        assert resp.json()["concurrency"]["preflight"]["state"] == "unknown"


def _enter_patchers(patchers: tuple[Any, ...]) -> contextlib.ExitStack:
    stack = contextlib.ExitStack()
    for patcher in patchers:
        stack.enter_context(patcher)
    return stack


class TestApplyTemplate:
    URL = f"/api/v1/runners/profiles/{_PROFILE_ID}/apply-template"

    def test_unauthenticated_is_rejected(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.URL)
        assert resp.status_code in (401, 403)

    def test_apply_returns_refreshed_row(self, client: TestClient) -> None:
        refreshed = _profile_row()
        with (
            patch(f"{_ROUTES}.apply_bundled_runner_template", new=AsyncMock(return_value=refreshed)),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.post(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["image_ref"] == BUNDLED_RUNNER_IMAGE_REF
        assert data["drift"]["is_seeded"] is True

    def test_apply_non_template_row_returns_404(self, client: TestClient) -> None:
        with (
            patch(f"{_ROUTES}.apply_bundled_runner_template", new=AsyncMock(return_value=None)),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
        ):
            resp = client.post(self.URL)
        assert resp.status_code == 404
