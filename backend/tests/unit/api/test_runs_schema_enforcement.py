"""Route coverage tests for the FAR-902 schema-enforcement endpoints.

Covers the operator-readable run-detail enforcement surface
(``GET /runs/{run_id}/schema-enforcement``) and the org-wide read-only
flip-guard advisory (``GET /runs/schema-enforcement/flip-guard``).

The pure derivation/aggregation functions are covered by
``tests/unit/core/test_schema_enforcement.py``; these tests exercise the
HTTP handlers themselves (auth wiring, DB read, response shaping).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes import runs as runs_module
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.uuid4()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        redis_url="redis://localhost:6379/0",
    )


def _result(rows: list | None = None) -> MagicMock:
    r = MagicMock()
    r.all = MagicMock(return_value=rows if rows is not None else [])
    r.scalar_one_or_none = MagicMock(return_value=None)
    r.scalar = MagicMock(return_value=0)
    r.scalars.return_value.all = MagicMock(return_value=[])
    return r


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=_result())
    return session


class _MockFactory:
    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    def __call__(self) -> _MockFactory:
        return self

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        pass


def _install_overrides(session: AsyncMock) -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[_get_session_factory] = lambda: _MockFactory(session)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: TenantPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan


@pytest.fixture(autouse=True)
def _patch_route_rls() -> Generator[None, None, None]:
    with (
        patch("modulo.api.routes.runs.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.runs.set_rls_user_context", new_callable=AsyncMock),
    ):
        yield


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()
    _install_overrides(session)
    yield TestClient(app), session
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/schema-enforcement
# ---------------------------------------------------------------------------


def test_get_run_schema_enforcement_returns_per_node_records(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    run = MagicMock()
    run.schema_validator_mode = "lenient"
    run.schema_validation_outcome = "lenient_validation_bypassed"
    rec_lenient = {"outcome": "lenient_validation_bypassed", "repair_attempts": 0, "wasted_attempts": 0}
    rec_native = {"outcome": "native_decoded_and_validated", "repair_attempts": 2, "wasted_attempts": 1}
    session.execute = AsyncMock(
        return_value=_result(
            rows=[
                ("node_a", "run:1:node:node_a:agent", rec_lenient),
                ("node_a", "run:1:node:node_a:agent:2", rec_native),
            ]
        )
    )
    with patch.object(runs_module, "_do_get_run", new_callable=AsyncMock, return_value=run):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/schema-enforcement")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == str(_RUN_ID)
    assert body["schema_validator_mode"] == "lenient"
    assert body["schema_validation_outcome"] == "lenient_validation_bypassed"
    assert body["aggregate"] == {
        "native_count": 1,
        "verbatim_count": 1,
        "repair_count": 2,
        "wasted_count": 1,
        "total_attempts": 2,
        "enforcement_record_count": 2,
    }
    assert len(body["nodes"]) == 1
    node = body["nodes"][0]
    assert node["node_id"] == "node_a"
    assert node["aggregate"] == {
        "native_count": 1,
        "verbatim_count": 1,
        "repair_count": 2,
        "wasted_count": 1,
        "total_attempts": 2,
    }
    assert node["records"][0]["node_id"] == "node_a"
    assert node["records"][0]["attempt_key"] == "run:1:node:node_a:agent"


def test_get_run_schema_enforcement_derives_when_run_columns_null(client: tuple[TestClient, AsyncMock]) -> None:
    """NULL run columns fall back to the derived mode/outcome; non-dict rows are skipped."""
    http, session = client
    run = MagicMock()
    run.schema_validator_mode = None
    run.schema_validation_outcome = None
    session.execute = AsyncMock(
        return_value=_result(
            rows=[
                ("node_a", "a1", {"outcome": "native_decoded_and_validated"}),
                ("node_b", "b1", "not-a-dict"),
            ]
        )
    )
    with patch.object(runs_module, "_do_get_run", new_callable=AsyncMock, return_value=run):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/schema-enforcement")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema_validator_mode"] == "strict"
    assert body["schema_validation_outcome"] == "native_decoded_and_validated"
    assert [n["node_id"] for n in body["nodes"]] == ["node_a"]


def test_get_run_schema_enforcement_empty_returns_lenient_no_schema(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    run = MagicMock()
    run.schema_validator_mode = None
    run.schema_validation_outcome = None
    session.execute = AsyncMock(return_value=_result(rows=[]))
    with patch.object(runs_module, "_do_get_run", new_callable=AsyncMock, return_value=run):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/schema-enforcement")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["nodes"] == []
    assert body["schema_validator_mode"] == "lenient"
    assert body["schema_validation_outcome"] == "no_schema"


def test_get_run_schema_enforcement_unknown_run_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with patch.object(
        runs_module,
        "_do_get_run",
        new_callable=AsyncMock,
        side_effect=runs_module.RunNotFoundError(_RUN_ID),
    ):
        resp = http.get(f"/api/v1/runs/{_RUN_ID}/schema-enforcement")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Run not found"


# ---------------------------------------------------------------------------
# GET /runs/schema-enforcement/flip-guard
# ---------------------------------------------------------------------------


def test_flip_guard_safe_when_no_lenient_warnings(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    session.execute = AsyncMock(return_value=_result(rows=[({"outcome": "native_decoded_and_validated"},)]))
    resp = http.get("/api/v1/runs/schema-enforcement/flip-guard")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["safe_to_flip"] is True
    assert body["lenient_warning_count"] == 0
    assert body["total_records"] == 1
    assert body["affected_outcomes"] == []
    assert "Safe to flip" in body["advisory"]


def test_flip_guard_not_safe_with_lenient_warnings(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    session.execute = AsyncMock(
        return_value=_result(
            rows=[
                ({"outcome": "lenient_validation_bypassed"},),
                ({"outcome": "lenient_validation_bypassed"},),
                ("junk",),
            ]
        )
    )
    resp = http.get("/api/v1/runs/schema-enforcement/flip-guard")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["safe_to_flip"] is False
    assert body["lenient_warning_count"] == 2
    assert body["total_records"] == 2
    assert body["affected_outcomes"] == ["lenient_validation_bypassed"]
    assert "NOT safe to flip" in body["advisory"]
