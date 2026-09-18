"""Unit tests for eval definition CRUD endpoints.

Tests: POST /api/v1/evals, GET /api/v1/evals, GET /api/v1/evals/{eval_id},
       PUT /api/v1/evals/{eval_id}, DELETE /api/v1/evals/{eval_id}
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Select

import modulo.api.routes.evals as evals_routes
from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

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
    configure_mock_session(session)
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
    m.node_id = overrides.get("node_id")
    m.name = overrides.get("name", "Test Eval")
    m.eval_type = overrides.get("eval_type", "regex")
    m.config_json = overrides.get("config_json", {"pattern": r"\d+"})
    m.failure_behaviour = overrides.get("failure_behaviour", "warn")
    m.pass_threshold = overrides.get("pass_threshold")
    m.suite_id = overrides.get("suite_id")
    m.created_by = overrides.get("created_by", _USER_ID)
    m.account_id = overrides.get("account_id", _USER_ID)
    m.version = overrides.get("version", 1)
    m.pre_version_raw = overrides.get("pre_version_raw")
    return m


@pytest.fixture
def admin_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db_session] = override_session
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def runner_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="runner",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="runner",
    )
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db_session] = override_session
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


# ── POST /api/v1/evals ─────────────────────────────────────────────────────


class TestCreateEvalDefinition:
    URL = "/api/v1/evals"

    def test_create_returns_201(self, admin_client: TestClient) -> None:
        mock_pipeline = MagicMock()
        mock_pipeline.id = _PIPELINE_ID
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(),  # require_permission authz_enforce (kill-switch) read
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=mock_pipeline),  # pipeline ownership check
        ]
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.post(
            self.URL,
            json={
                "pipeline_id": str(_PIPELINE_ID),
                "name": "Test Eval",
                "eval_type": "regex",
                "config_json": {"pattern": r"\d+"},
                "failure_behaviour": "block",
                "pass_threshold": 0.8,
                "suite_id": "suite-1",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test Eval"
        assert data["eval_type"] == "regex"
        assert data["failure_behaviour"] == "block"
        assert data["pass_threshold"] == pytest.approx(0.8)
        assert data["suite_id"] == "suite-1"
        assert data["config_json"] == {"pattern": r"\d+"}
        assert data["version"] == 1

    def test_create_omit_optionals(self, admin_client: TestClient) -> None:
        mock_pipeline = MagicMock()
        mock_pipeline.id = _PIPELINE_ID
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(),  # require_permission authz_enforce (kill-switch) read
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=mock_pipeline),  # pipeline ownership check
        ]
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.post(
            self.URL,
            json={
                "pipeline_id": str(_PIPELINE_ID),
                "name": "Minimal Eval",
                "eval_type": "regex",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["pass_threshold"] is None
        assert data["suite_id"] is None

    def test_create_admin_required(self, runner_client: TestClient) -> None:
        resp = runner_client.post(
            self.URL,
            json={
                "pipeline_id": str(_PIPELINE_ID),
                "name": "Test Eval",
                "eval_type": "regex",
            },
        )
        assert resp.status_code == 403

    def test_create_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(
            self.URL,
            json={
                "pipeline_id": str(_PIPELINE_ID),
                "name": "Test Eval",
                "eval_type": "regex",
            },
        )
        assert resp.status_code in (401, 403)

    def test_create_invalid_eval_type(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            self.URL,
            json={
                "pipeline_id": str(_PIPELINE_ID),
                "name": "Bad Eval",
                "eval_type": "invalid_type",
            },
        )
        assert resp.status_code == 422

    def test_create_guardrail_forbidden_detection_envelope(self, admin_client: TestClient) -> None:
        # PRD §8.17: guardrail detection must be regex|json_schema. A nested
        # ``detection`` envelope that declares a forbidden type is rejected at
        # the API edge (never reaches the engine to fail closed at run time).
        resp = admin_client.post(
            self.URL,
            json={
                "pipeline_id": str(_PIPELINE_ID),
                "name": "Envelope Eval",
                "eval_type": "guardrail",
                "config_json": {"detection": {"type": "llm_judge"}},
                "failure_behaviour": "block",
            },
        )
        assert resp.status_code == 422

    def test_create_guardrail_detection_envelope_accepted(self, admin_client: TestClient) -> None:
        mock_pipeline = MagicMock()
        mock_pipeline.id = _PIPELINE_ID
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(),  # require_permission authz_enforce (kill-switch) read
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=mock_pipeline),  # pipeline ownership check
        ]
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.post(
            self.URL,
            json={
                "pipeline_id": str(_PIPELINE_ID),
                "name": "Envelope Regex Eval",
                "eval_type": "guardrail",
                "config_json": {"detection": {"type": "regex", "field": "body", "pattern": r"SECRET_[A-Z0-9]{8}"}},
                "failure_behaviour": "block",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["eval_type"] == "guardrail"


# ── GET /api/v1/evals ──────────────────────────────────────────────────────


class TestListEvalDefinitions:
    URL = "/api/v1/evals"

    def test_list_returns_200(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(),  # require_permission authz_enforce (kill-switch) read
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_value=2),
            _make_result(
                all_value=[
                    _make_eval_def(id=uuid.uuid4(), name="Eval 1"),
                    _make_eval_def(id=uuid.uuid4(), name="Eval 2"),
                ]
            ),
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
            _make_result(),  # require_permission authz_enforce (kill-switch) read
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
        assert not data["items"]

    def test_list_queries_are_org_scoped(self, admin_client: TestClient) -> None:
        """The eval definitions list query must filter by organisation_id — an
        org can never list another org's eval definitions."""
        mock_session = _make_mock_session()
        captured_stmts: list[MagicMock] = []

        async def _capturing_execute(stmt: MagicMock, *args: object, **kwargs: object) -> MagicMock:
            captured_stmts.append(stmt)
            idx = len(captured_stmts)
            if idx <= 4:
                return _make_result() if idx == 1 else _make_result(scalar_value=None)
            if idx == 5:
                return _make_result(scalar_value=1)
            return _make_result(all_value=[_make_eval_def(id=uuid.uuid4(), name="Eval 1")])

        mock_session.execute = _capturing_execute

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.get(self.URL)
        assert resp.status_code == 200

        # The two real queries (count + select) both carry the org filter. We
        # restrict the scan to the count/list Selects on EvalDefinition: the RLS
        # setup (`set_config('app.organisation_id', ...)`) is also executed on
        # this session and always contains the literal "organisation_id", so a
        # blanket substring scan could never fail even if the filter regressed.
        eval_selects = [
            stmt for stmt in captured_stmts if isinstance(stmt, Select) and "eval_definitions" in str(stmt.compile())
        ]
        assert len(eval_selects) == 2, f"expected count + list queries, got {len(eval_selects)}"
        for stmt in eval_selects:
            where_sql = str(stmt.whereclause) if stmt.whereclause is not None else ""
            assert "organisation_id" in where_sql, "eval list query is not org-scoped"

    def test_list_filter_by_pipeline(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(),  # require_permission authz_enforce (kill-switch) read
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_value=1),
            _make_result(
                all_value=[
                    _make_eval_def(name="Filtered Eval"),
                ]
            ),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.get(f"{self.URL}?pipeline_id={_PIPELINE_ID}")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_list_filter_by_eval_type(self, admin_client: TestClient) -> None:
        """The guardrail management view lists bound guardrails by filtering the
        eval-definitions list on eval_type='guardrail' (FAR-223 PR D)."""
        mock_session = _make_mock_session()
        captured_stmts: list[MagicMock] = []

        async def _capturing_execute(stmt: MagicMock, *args: object, **kwargs: object) -> MagicMock:
            captured_stmts.append(stmt)
            idx = len(captured_stmts)
            if idx <= 4:
                return _make_result() if idx == 1 else _make_result(scalar_value=None)
            if idx == 5:
                return _make_result(scalar_value=1)
            return _make_result(all_value=[_make_eval_def(name="Guardrail 1")])

        mock_session.execute = _capturing_execute

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.get(f"{self.URL}?eval_type=guardrail")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

        # The count + list queries must both carry the eval_type filter.
        eval_selects = [
            stmt for stmt in captured_stmts if isinstance(stmt, Select) and "eval_definitions" in str(stmt.compile())
        ]
        assert len(eval_selects) == 2, f"expected count + list queries, got {len(eval_selects)}"
        for stmt in eval_selects:
            where_sql = str(stmt.whereclause) if stmt.whereclause is not None else ""
            assert "eval_type" in where_sql, "eval list query is not filtered by eval_type"

    def test_list_rejects_invalid_eval_type(self, admin_client: TestClient) -> None:
        resp = admin_client.get(f"{self.URL}?eval_type=not_a_real_type")
        assert resp.status_code == 422

    def test_list_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code in (401, 403)


# ── GET /api/v1/evals/{eval_id} ────────────────────────────────────────────


class TestGetEvalDefinition:
    URL = "/api/v1/evals"

    def test_get_returns_200(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(),  # require_permission authz_enforce (kill-switch) read
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
            _make_result(),  # require_permission authz_enforce (kill-switch) read
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
            _make_result(),  # require_permission authz_enforce (kill-switch) read
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=eval_def),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.put(
            f"{self.URL}/{_EVAL_DEF_ID}",
            json={
                "name": "Updated Eval",
                "pass_threshold": 0.9,
                "suite_id": "suite-2",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Updated Eval"
        assert data["pass_threshold"] == pytest.approx(0.9)
        assert data["suite_id"] == "suite-2"
        # FAR-382: an update bumps the version and snapshots the pre-edit config
        # so a rubric change is explicitly version-scoped.
        assert data["version"] == 2
        assert data["pre_version_raw"] == {"config_json": {"pattern": r"\d+"}}

    def test_update_not_found(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(),  # require_permission authz_enforce (kill-switch) read
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
            _make_result(),  # require_permission authz_enforce (kill-switch) read
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
            _make_result(),  # require_permission authz_enforce (kill-switch) read
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


class TestDeleteEvalDefinitionTwoStep:
    """FAR-309 PR B two-step soft-delete for GUARDRAIL eval definitions.

    Step 1 — soft-delete: ``DELETE`` stamps ``deleted_at``/``deleted_by`` on a
    guardrail eval definition (the row is retained so snapshot pins referencing
    it take the skipped-with-audit path). Step 2 — purge: ``DELETE ?purge=true``
    hard-removes soft-deleted rows. Non-guardrail evals keep their existing
    hard delete.
    """

    URL = "/api/v1/evals"

    def test_delete_guardrail_eval_soft_deletes(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        eval_def = _make_eval_def(eval_type="guardrail")
        mock_session.execute.side_effect = [
            _make_result(),  # require_permission authz_enforce (kill-switch) read
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=eval_def),
        ]
        mock_session.delete = AsyncMock()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.delete(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code == 204
        # Step 1: soft-delete stamps deleted_at/deleted_by; the row is NOT
        # hard-deleted.
        assert eval_def.deleted_at is not None
        assert eval_def.deleted_by == _USER_ID
        mock_session.delete.assert_not_called()

    def test_delete_guardrail_eval_purge_hard_deletes(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(),  # require_permission authz_enforce (kill-switch) read
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=_make_eval_def(eval_type="guardrail")),
        ]
        mock_session.delete = AsyncMock()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.delete(f"{self.URL}/{_EVAL_DEF_ID}?purge=true")
        assert resp.status_code == 204
        # Step 2: purge hard-removes the soft-deleted row.
        mock_session.delete.assert_called_once()

    def test_delete_guardrail_eval_soft_delete_writes_audit(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two-step soft-delete writes an org-scoped audit event
        (``eval_definition.soft_deleted``) carrying the eval identity + name."""
        audit = AsyncMock()
        monkeypatch.setattr(evals_routes, "append_audit_event", audit)
        mock_session = _make_mock_session()
        eval_def = _make_eval_def(eval_type="guardrail", name="no-secrets")
        mock_session.execute.side_effect = [
            _make_result(),  # require_permission authz_enforce (kill-switch) read
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=eval_def),
        ]
        mock_session.delete = AsyncMock()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.delete(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code == 204
        audit.assert_awaited_once()
        _, kwargs = audit.call_args
        assert kwargs["event_type"] == "eval_definition.soft_deleted"
        assert kwargs["org_id"] == _ORG_ID
        assert kwargs["resource_type"] == "eval_definition"
        assert kwargs["resource_id"] == _EVAL_DEF_ID
        assert kwargs["payload_json"]["eval_id"] == str(_EVAL_DEF_ID)
        assert kwargs["payload_json"]["name"] == "no-secrets"
        assert kwargs["payload_json"]["purge"] is False

    def test_delete_guardrail_eval_purge_writes_audit(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The purge step writes its own audit event
        (``eval_definition.purged``) before the row is removed."""
        audit = AsyncMock()
        monkeypatch.setattr(evals_routes, "append_audit_event", audit)
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(),  # require_permission authz_enforce (kill-switch) read
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=_make_eval_def(eval_type="guardrail")),
        ]
        mock_session.delete = AsyncMock()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.delete(f"{self.URL}/{_EVAL_DEF_ID}?purge=true")
        assert resp.status_code == 204
        audit.assert_awaited_once()
        _, kwargs = audit.call_args
        assert kwargs["event_type"] == "eval_definition.purged"
        assert kwargs["payload_json"]["purge"] is True

    def test_delete_non_guardrail_eval_keeps_hard_delete(self, admin_client: TestClient) -> None:
        """Non-guardrail eval definitions keep their existing HARD delete —
        the row is removed, never soft-stamped."""
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(),  # require_permission authz_enforce (kill-switch) read
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=_make_eval_def(eval_type="regex")),
        ]
        mock_session.delete = AsyncMock()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.delete(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code == 204
        mock_session.delete.assert_called_once()

    def test_delete_non_guardrail_eval_writes_no_audit_event(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two-step soft-delete audit applies to GUARDRAIL rows only — a
        non-guardrail eval keeps its pre-PR-B hard delete WITHOUT an audit
        event (``eval_definition.soft_deleted``/``eval_definition.purged`` are
        guardrail-row events)."""
        audit = AsyncMock()
        monkeypatch.setattr(evals_routes, "append_audit_event", audit)
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(),  # require_permission authz_enforce (kill-switch) read
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=_make_eval_def(eval_type="regex")),
        ]
        mock_session.delete = AsyncMock()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.delete(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code == 204
        mock_session.delete.assert_called_once()
        audit.assert_not_awaited()
