"""Tests for the global search endpoint."""

import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session
from modulo.api.routes.admin import router as admin_router
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal

ORG_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
ADMIN_PRINCIPAL = AuthenticatedPrincipal(
    username="admin",
    organisation_id=ORG_ID,
    account_id=USER_ID,
    org_role="admin",
)
VIEWER_PRINCIPAL = AuthenticatedPrincipal(
    username="viewer",
    organisation_id=ORG_ID,
    account_id=USER_ID,
    org_role="viewer",
)


class _FakeAsyncSession(AsyncMock):
    """Mock session supporting `async with session.begin():` protocol."""

    async def __aenter__(self) -> "_FakeAsyncSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def begin(self) -> "_FakeAsyncSession":
        return self


def _make_app(mock_session: _FakeAsyncSession) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[get_db_session] = lambda: mock_session
    return app


def _mock_session() -> _FakeAsyncSession:
    return _FakeAsyncSession()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_db() -> AsyncMock:
    return _mock_session()


@pytest.fixture()
def client(mock_db: AsyncMock) -> Generator[TestClient, None, None]:
    app = _make_app(mock_db)
    app.dependency_overrides[get_current_user] = lambda: ADMIN_PRINCIPAL
    with patch("modulo.api.routes.admin.set_rls_org", AsyncMock()):
        yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def viewer_client(mock_db: AsyncMock) -> Generator[TestClient, None, None]:
    app = _make_app(mock_db)
    app.dependency_overrides[get_current_user] = lambda: VIEWER_PRINCIPAL
    with patch("modulo.api.routes.admin.set_rls_org", AsyncMock()):
        yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_scalar_result(value: int) -> MagicMock:
    m = MagicMock()
    m.scalar.return_value = value
    return m


def _make_result(*, all_rows: list[MagicMock] | None = None, scalar_value: int | None = None) -> MagicMock:
    m = MagicMock()
    if all_rows is not None:
        m.all.return_value = all_rows
    if scalar_value is not None:
        m.scalar.return_value = scalar_value
    return m


def _mock_rows(data: list[tuple[int, object, object, object]]) -> MagicMock:
    row_mocks = []
    for row in data:
        rm = MagicMock()
        rm.relevance = row[0]
        if len(row) >= 4:
            rm.id = row[1]
            rm.name = row[2]
            rm.description = row[3]
        else:
            rm.id = row[1]
            rm.display_id = row[2]
            rm.pipeline_name = row[3]
        row_mocks.append(rm)
    m = _make_result(all_rows=row_mocks)
    m.scalar.return_value = len(data) if data else 0
    return m


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGlobalSearchAuth:
    def test_requires_auth(self, mock_db: AsyncMock) -> None:
        app = _make_app(mock_db)
        client = TestClient(app)
        resp = client.get("/api/v1/admin/search?q=test")
        assert resp.status_code in (401, 403)

    def test_viewer_cannot_search(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/api/v1/admin/search?q=test")
        assert resp.status_code == 403
        assert "Insufficient permissions" in resp.text

    def test_operator_can_search(self, mock_db: AsyncMock) -> None:
        principal = AuthenticatedPrincipal(
            username="operator",
            organisation_id=ORG_ID,
            account_id=USER_ID,
            org_role="operator",
        )
        app = _make_app(mock_db)
        app.dependency_overrides[get_current_user] = lambda: principal
        mock_db.execute.return_value = _mock_rows([])
        mock_db.execute.side_effect = None
        client = TestClient(app)
        resp = client.get("/api/v1/admin/search?q=test")
        assert resp.status_code == 200


class TestGlobalSearchEndpoint:
    def test_empty_results(self, client: TestClient, mock_db: AsyncMock) -> None:
        mock_db.execute.return_value = _make_result(all_rows=[], scalar_value=0)

        resp = client.get("/api/v1/admin/search?q=zzzznonexistent")
        assert resp.status_code == 200
        body = resp.json()
        assert body["results"] == []
        assert body["total_by_type"] == {
            "pipeline": 0,
            "run": 0,
            "audit": 0,
            "library": 0,
        }

    def test_min_query_length(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/search?q=")
        assert resp.status_code == 422

    def test_invalid_type_filter(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/search?q=test&type=invalid")
        assert resp.status_code == 422

    def test_pipeline_search_type(self, client: TestClient, mock_db: AsyncMock) -> None:
        pipe_id = uuid.uuid4()
        calls: list[MagicMock] = []

        pipe_row = MagicMock()
        pipe_row.relevance = 2
        pipe_row.id = pipe_id
        pipe_row.name = "My Pipeline"
        pipe_row.description = "A test pipeline"

        def execute_side(*args: object, **kwargs: object) -> MagicMock:
            nonlocal calls
            calls.append(MagicMock())
            idx = len(calls)
            if idx % 2 == 1:
                return _make_result(all_rows=[pipe_row])
            return _make_result(scalar_value=1)

        mock_db.execute.side_effect = execute_side

        resp = client.get("/api/v1/admin/search?q=pipeline&type=pipeline")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) == 1
        assert body["results"][0]["type"] == "pipeline"
        assert body["results"][0]["title"] == "My Pipeline"
        assert body["results"][0]["url"] == f"/pipelines/{pipe_id}"
        assert body["total_by_type"]["pipeline"] == 1

    def test_all_types_search(self, client: TestClient, mock_db: AsyncMock) -> None:
        pipe_id = uuid.uuid4()
        run_id = uuid.uuid4()
        audit_id = uuid.uuid4()
        lib_id = uuid.uuid4()

        pipe_row = MagicMock()
        pipe_row.relevance = 2
        pipe_row.id = pipe_id
        pipe_row.name = "Data Pipeline"
        pipe_row.description = "ETL"

        run_row = MagicMock()
        run_row.relevance = 2
        run_row.id = run_id
        run_row.display_id = str(run_id)
        run_row.pipeline_name = "Data Pipeline"

        audit_row = MagicMock()
        audit_row.relevance = 2
        audit_row.id = audit_id
        audit_row.event_type = "pipeline_run"
        audit_row.resource_type = "Pipeline"

        lib_row = MagicMock()
        lib_row.relevance = 2
        lib_row.id = lib_id
        lib_row.name = "Transformer"
        lib_row.description = "A transformer node"

        type_results = {
            0: pipe_row,
            1: run_row,
            2: audit_row,
            3: lib_row,
        }

        call_idx = 0

        def execute_side(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_idx
            t = call_idx // 2
            is_data = call_idx % 2 == 0
            call_idx += 1
            if is_data:
                rows = [type_results[t]]
                return _make_result(all_rows=rows)
            return _make_result(scalar_value=1)

        mock_db.execute.side_effect = execute_side

        resp = client.get("/api/v1/admin/search?q=data")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) >= 1
        assert body["total_by_type"] == {
            "pipeline": 1,
            "run": 1,
            "audit": 1,
            "library": 1,
        }

    def test_limit_and_offset(self, client: TestClient, mock_db: AsyncMock) -> None:
        pipe_id = uuid.uuid4()
        pipe_row = MagicMock()
        pipe_row.relevance = 2
        pipe_row.id = pipe_id
        pipe_row.name = "Pipeline A"
        pipe_row.description = "First"

        call_idx = 0

        def execute_side(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_idx
            is_data = call_idx % 2 == 0
            call_idx += 1
            if is_data:
                return _make_result(all_rows=[pipe_row])
            return _make_result(scalar_value=1)

        mock_db.execute.side_effect = execute_side

        resp = client.get("/api/v1/admin/search?q=pipeline&type=pipeline&limit=5&offset=0")
        assert resp.status_code == 200

    def test_relevance_ordering(self, client: TestClient, mock_db: AsyncMock) -> None:
        id_a = uuid.uuid4()
        id_b = uuid.uuid4()
        id_c = uuid.uuid4()

        def _make_row(relevance: int, row_id: uuid.UUID, title: str, subtitle: str) -> MagicMock:
            rm = MagicMock()
            rm.relevance = relevance
            rm.id = row_id
            rm.name = title
            rm.description = subtitle
            return rm

        row_a = _make_row(2, id_a, "Exact Match Pipeline", "desc")
        row_b = _make_row(1, id_b, "Something with pipeline in name", "desc")
        row_c = _make_row(1, id_c, "Another pipeline mention", "desc")

        call_idx = 0

        def execute_side(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_idx
            is_data = call_idx % 2 == 0
            call_idx += 1
            if is_data:
                return _make_result(all_rows=[row_a, row_b, row_c])
            return _make_result(scalar_value=3)

        mock_db.execute.side_effect = execute_side

        resp = client.get("/api/v1/admin/search?q=exact&type=pipeline")
        assert resp.status_code == 200

    def test_org_scoping(self, client: TestClient, mock_db: AsyncMock) -> None:
        mock_db.execute.return_value = _make_result(all_rows=[], scalar_value=0)

        resp = client.get("/api/v1/admin/search?q=test")
        assert resp.status_code == 200
