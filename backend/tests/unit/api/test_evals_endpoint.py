"""Unit tests for eval definition CRUD endpoints.

Tests: POST /api/v1/evals, GET /api/v1/evals, GET /api/v1/evals/{eval_id},
       PUT /api/v1/evals/{eval_id}, DELETE /api/v1/evals/{eval_id}
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_EVAL_DEF_ID = uuid.UUID("00000000-0000-0000-0000-000000000030")


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
    session.execute = AsyncMock()
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = AsyncMock(return_value=bind_mock)
    return session


def _make_result(scalar_one_value=None, scalar_value=None, all_value=None) -> MagicMock:
    m = MagicMock()
    m.scalar_one_or_none = MagicMock(return_value=scalar_one_value)
    if scalar_value is not None:
        m.scalar = MagicMock(return_value=scalar_value)
    if all_value is not None:
        m.all = MagicMock(return_value=all_value)
        m.scalars.return_value = m
    return m


def _make_eval_def(**overrides) -> MagicMock:
    m = MagicMock()
    m.id = overrides.get("id", _EVAL_DEF_ID)
    m.pipeline_id = overrides.get("pipeline_id", _PIPELINE_ID)
    m.node_id = overrides.get("node_id", None)
    m.name = overrides.get("name", "Test Eval")
    m.eval_type = overrides.get("eval_type", "regex")
    m.config_json = overrides.get("config_json", {"pattern": r"\d+"})
    m.failure_behaviour = overrides.get("failure_behaviour", "warn")
    m.pass_threshold = overrides.get("pass_threshold", None)
    m.suite_id = overrides.get("suite_id", None)
    m.created_by = overrides.get("created_by", _USER_ID)
    return m


@pytest.fixture()
def admin_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def runner_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="runner",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="runner",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── POST /api/v1/evals ─────────────────────────────────────────────────────


class TestCreateEvalDefinition:
    URL = "/api/v1/evals"

    def test_create_returns_201(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
        ]
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.post(self.URL, json={
            "pipeline_id": str(_PIPELINE_ID),
            "name": "Test Eval",
            "eval_type": "regex",
            "config_json": {"pattern": r"\d+"},
            "failure_behaviour": "block",
            "pass_threshold": 0.8,
            "suite_id": "suite-1",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test Eval"
        assert data["eval_type"] == "regex"
        assert data["failure_behaviour"] == "block"
        assert data["pass_threshold"] == 0.8
        assert data["suite_id"] == "suite-1"

    def test_create_omit_optionals(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
        ]
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.post(self.URL, json={
            "pipeline_id": str(_PIPELINE_ID),
            "name": "Minimal Eval",
            "eval_type": "regex",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["pass_threshold"] is None
        assert data["suite_id"] is None

    def test_create_admin_required(self, runner_client: TestClient) -> None:
        resp = runner_client.post(self.URL, json={
            "pipeline_id": str(_PIPELINE_ID),
            "name": "Test Eval",
            "eval_type": "regex",
        })
        assert resp.status_code == 403

    def test_create_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.URL, json={
            "pipeline_id": str(_PIPELINE_ID),
            "name": "Test Eval",
            "eval_type": "regex",
        })
        assert resp.status_code in (401, 403)

    def test_create_invalid_eval_type(self, admin_client: TestClient) -> None:
        resp = admin_client.post(self.URL, json={
            "pipeline_id": str(_PIPELINE_ID),
            "name": "Bad Eval",
            "eval_type": "invalid_type",
        })
        assert resp.status_code == 422


# ── GET /api/v1/evals ──────────────────────────────────────────────────────


class TestListEvalDefinitions:
    URL = "/api/v1/evals"

    def test_list_returns_200(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_value=2),
            _make_result(all_value=[
                _make_eval_def(id=uuid.uuid4(), name="Eval 1"),
                _make_eval_def(id=uuid.uuid4(), name="Eval 2"),
            ]),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        assert data["items"][0]["name"] == "Eval 1"
        assert data["page"] == 1
        assert data["page_size"] == 20

    def test_list_empty(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_value=0),
            _make_result(all_value=[]),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_list_filter_by_pipeline(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_value=1),
            _make_result(all_value=[
                _make_eval_def(name="Filtered Eval"),
            ]),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.get(f"{self.URL}?pipeline_id={_PIPELINE_ID}")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_list_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code in (401, 403)


# ── GET /api/v1/evals/{eval_id} ────────────────────────────────────────────


class TestGetEvalDefinition:
    URL = "/api/v1/evals"

    def test_get_returns_200(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=_make_eval_def(name="My Eval")),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.get(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "My Eval"

    def test_get_not_found(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=None),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.get(f"{self.URL}/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_get_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code in (401, 403)


# ── PUT /api/v1/evals/{eval_id} ────────────────────────────────────────────


class TestUpdateEvalDefinition:
    URL = "/api/v1/evals"

    def test_update_returns_200(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        eval_def = _make_eval_def(name="Original", pass_threshold=None, suite_id=None)
        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=eval_def),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.put(f"{self.URL}/{_EVAL_DEF_ID}", json={
            "name": "Updated Eval",
            "pass_threshold": 0.9,
            "suite_id": "suite-2",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Updated Eval"
        assert data["pass_threshold"] == 0.9
        assert data["suite_id"] == "suite-2"

    def test_update_not_found(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=None),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.put(f"{self.URL}/{uuid.uuid4()}", json={"name": "Nope"})
        assert resp.status_code == 404

    def test_update_admin_required(self, runner_client: TestClient) -> None:
        resp = runner_client.put(f"{self.URL}/{_EVAL_DEF_ID}", json={"name": "Should Fail"})
        assert resp.status_code == 403

    def test_update_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.put(f"{self.URL}/{_EVAL_DEF_ID}", json={"name": "Should Fail"})
        assert resp.status_code in (401, 403)


# ── DELETE /api/v1/evals/{eval_id} ─────────────────────────────────────────


class TestDeleteEvalDefinition:
    URL = "/api/v1/evals"

    def test_delete_returns_204(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=_make_eval_def()),
        ]
        mock_session.delete = AsyncMock()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.delete(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code == 204

    def test_delete_not_found(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=None),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.delete(f"{self.URL}/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_delete_admin_required(self, runner_client: TestClient) -> None:
        resp = runner_client.delete(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code == 403

    def test_delete_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.delete(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code in (401, 403)
