"""Route-level coverage tests for the schema endpoints (FAR-618).

Complements ``test_schemas_endpoint.py`` (CRUD + migrate happy paths),
``test_schema_generate_endpoint.py`` / ``test_schema_infer_endpoint.py`` (LLM
surfaces) by covering the per-route DB error-convention matrices
(IntegrityError->409, ProgrammingError->501, SQLAlchemyError->503, generic
Exception->500), the /counts aggregation, folder-move and deletion-guard
branches, the inference context guards (404 connector / 400 unsupported type /
400 no backends), the model-backend resolver failure mappings (502/503), the
migration-plan failure paths, the validate/import helper branches, and the
JSON-path location lookup helpers.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.schema_registry import SchemaGenerationError
from modulo.db.crud.schema import SchemaDeletionProtectedError
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_SCHEMA_ID = uuid.uuid4()
_FOLDER_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_PROG = ProgrammingError("s", {}, Exception())
_SQL = SQLAlchemyError("boom")
_INTEGRITY = IntegrityError("s", {}, Exception())
_RUNTIME = RuntimeError("kaboom")

_PREFIX = "modulo.api.routes.schemas."


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_schema() -> MagicMock:
    s = MagicMock()
    s.id = _SCHEMA_ID
    s.organisation_id = _ORG_ID
    s.name = "Test Schema"
    s.description = None
    s.abstract_name = None
    s.folder_id = None
    s.account_id = uuid.uuid4()
    s.created_by = s.account_id
    s.created_at = _NOW
    s.updated_at = _NOW
    s.deprecated = False
    s.deprecated_at = None
    return s


def _make_schema_version(schema_id: uuid.UUID) -> MagicMock:
    sv = MagicMock()
    sv.id = uuid.uuid4()
    sv.organisation_id = _ORG_ID
    sv.schema_id = schema_id
    sv.version = "1.0"
    sv.version_number = 1
    sv.definition_json = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    sv.published = False
    sv.account_id = uuid.uuid4()
    sv.created_by = sv.account_id
    sv.created_at = _NOW
    sv.updated_at = _NOW
    return sv


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.all.return_value = []
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin@test", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app), session
    app.dependency_overrides.clear()


def _assert_error_matrix(
    http: TestClient,
    *,
    method: str,
    url: str,
    json_body: dict | None,
    patch_target: str,
    expected: dict,
    extra_patches: tuple = (),
    query: dict | None = None,
) -> None:
    for exc, status_code in expected:
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}{patch_target}", new=AsyncMock(side_effect=exc)))
            for target, kwargs in extra_patches:
                stack.enter_context(patch(f"{_PREFIX}{target}", **kwargs))
            stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
            stack.enter_context(patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock))
            kwargs = {"json": json_body} if json_body is not None else {}
            if query:
                kwargs["params"] = query
            resp = getattr(http, method.lower())(url, **kwargs)
        assert resp.status_code == status_code, f"{patch_target} {exc!r}: {resp.text}"


# ---------------------------------------------------------------------------
# GET /schemas + /counts — error mapping + aggregation
# ---------------------------------------------------------------------------


def test_list_schemas_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="get",
        url="/api/v1/schemas",
        json_body=None,
        patch_target="list_schemas",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_schema_counts_happy_path_aggregates_by_folder(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    rows = [(_SCHEMA_ID, 3), (None, 2)]
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    session.execute = AsyncMock(return_value=result)
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        resp = http.get("/api/v1/schemas/counts")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 5
    assert body["by_folder"] == {str(_SCHEMA_ID): 3}


def test_schema_counts_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    for exc, expected in [(_PROG, 501), (_SQL, 503)]:
        session.execute = AsyncMock(side_effect=exc)
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
            resp = http.get("/api/v1/schemas/counts")
        session.execute = _make_session().execute
        assert resp.status_code == expected, resp.text


# ---------------------------------------------------------------------------
# POST / GET / PATCH / deprecate / folder / delete — error mapping
# ---------------------------------------------------------------------------


def test_create_schema_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="post",
        url="/api/v1/schemas",
        json_body={"name": "New Schema"},
        patch_target="create_schema",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_get_schema_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="get",
        url=f"/api/v1/schemas/{_SCHEMA_ID}",
        json_body=None,
        patch_target="get_schema",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_update_schema_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="patch",
        url=f"/api/v1/schemas/{_SCHEMA_ID}",
        json_body={"name": "Renamed"},
        patch_target="update_schema",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
        extra_patches=(("_assert_owns_schema", {"new": AsyncMock(return_value=_make_schema())}),),
    )


def test_update_schema_unknown_after_write_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}update_schema", new=AsyncMock(return_value=None)))
        stack.enter_context(patch(f"{_PREFIX}_assert_owns_schema", new=AsyncMock(return_value=_make_schema())))
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        resp = http.patch(f"/api/v1/schemas/{_SCHEMA_ID}", json={"name": "Renamed"})

    assert resp.status_code == 404, resp.text


def test_deprecate_schema_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="patch",
        url=f"/api/v1/schemas/{_SCHEMA_ID}/deprecate",
        json_body=None,
        patch_target="deprecate_schema",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
        extra_patches=(("_assert_owns_schema", {"new": AsyncMock(return_value=_make_schema())}),),
    )


def test_deprecate_schema_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}deprecate_schema", new=AsyncMock(return_value=None)))
        stack.enter_context(patch(f"{_PREFIX}_assert_owns_schema", new=AsyncMock(return_value=_make_schema())))
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        resp = http.patch(f"/api/v1/schemas/{_SCHEMA_ID}/deprecate")

    assert resp.status_code == 404, resp.text


def test_move_schema_to_folder_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    moved = _make_schema()
    moved.folder_id = _FOLDER_ID
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}move_schema_to_folder", new=AsyncMock(return_value=moved)))
        stack.enter_context(patch(f"{_PREFIX}_assert_owns_schema", new=AsyncMock(return_value=_make_schema())))
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        resp = http.patch(f"/api/v1/schemas/{_SCHEMA_ID}/folder", json={"folder_id": str(_FOLDER_ID)})

    assert resp.status_code == 200, resp.text
    assert resp.json()["folder_id"] == str(_FOLDER_ID)


def test_move_schema_to_folder_invalid_returns_422(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(
            patch(f"{_PREFIX}move_schema_to_folder", new=AsyncMock(side_effect=ValueError("folder mismatch")))
        )
        stack.enter_context(patch(f"{_PREFIX}_assert_owns_schema", new=AsyncMock(return_value=_make_schema())))
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        resp = http.patch(f"/api/v1/schemas/{_SCHEMA_ID}/folder", json={"folder_id": str(_FOLDER_ID)})

    assert resp.status_code == 422, resp.text


def test_move_schema_to_folder_unknown_schema_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}move_schema_to_folder", new=AsyncMock(return_value=None)))
        stack.enter_context(patch(f"{_PREFIX}_assert_owns_schema", new=AsyncMock(return_value=_make_schema())))
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        resp = http.patch(f"/api/v1/schemas/{_SCHEMA_ID}/folder", json={"folder_id": str(_FOLDER_ID)})

    assert resp.status_code == 404, resp.text


def test_move_schema_to_folder_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="patch",
        url=f"/api/v1/schemas/{_SCHEMA_ID}/folder",
        json_body={"folder_id": str(_FOLDER_ID)},
        patch_target="move_schema_to_folder",
        expected={(_PROG, 501)},
        extra_patches=(("_assert_owns_schema", {"new": AsyncMock(return_value=_make_schema())}),),
    )


def test_delete_schema_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="delete",
        url=f"/api/v1/schemas/{_SCHEMA_ID}",
        json_body=None,
        patch_target="delete_schema",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
        extra_patches=(("_assert_owns_schema", {"new": AsyncMock(return_value=_make_schema())}),),
    )


def test_delete_schema_deletion_protected_returns_409(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                f"{_PREFIX}delete_schema",
                new=AsyncMock(side_effect=SchemaDeletionProtectedError("in use by pipelines")),
            )
        )
        stack.enter_context(patch(f"{_PREFIX}_assert_owns_schema", new=AsyncMock(return_value=_make_schema())))
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        resp = http.delete(f"/api/v1/schemas/{_SCHEMA_ID}")

    assert resp.status_code == 409, resp.text


def test_delete_schema_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}delete_schema", new=AsyncMock(return_value=False)))
        stack.enter_context(patch(f"{_PREFIX}_assert_owns_schema", new=AsyncMock(return_value=_make_schema())))
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        resp = http.delete(f"/api/v1/schemas/{_SCHEMA_ID}")

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Schema versions — error mapping
# ---------------------------------------------------------------------------


def test_list_schema_versions_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="get",
        url=f"/api/v1/schemas/{_SCHEMA_ID}/versions",
        json_body=None,
        patch_target="list_schema_versions",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
        extra_patches=(("get_schema", {"new": AsyncMock(return_value=_make_schema())}),),
    )


def test_create_schema_version_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="post",
        url=f"/api/v1/schemas/{_SCHEMA_ID}/versions",
        json_body={"version": "2.0", "version_number": 2, "definition_json": {"type": "object"}},
        patch_target="create_schema_version",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
        extra_patches=(("_assert_owns_schema", {"new": AsyncMock(return_value=_make_schema())}),),
    )


def test_get_schema_version_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="get",
        url=f"/api/v1/schemas/{_SCHEMA_ID}/versions/1.0",
        json_body=None,
        patch_target="get_schema_version",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_get_schema_version_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}get_schema_version", new=AsyncMock(return_value=None)))
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        resp = http.get(f"/api/v1/schemas/{_SCHEMA_ID}/versions/99")

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# GET /{schema_id}/fields — error mapping + unknown schema
# ---------------------------------------------------------------------------


def test_list_schema_fields_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        version = _make_schema_version(_SCHEMA_ID)
        stack.enter_context(patch(f"{_PREFIX}_load_latest_definition", new=AsyncMock(return_value=version)))
        resp = http.get(f"/api/v1/schemas/{_SCHEMA_ID}/fields")

    assert resp.status_code == 200, resp.text
    fields = resp.json()["fields"]
    assert len(fields) == 1
    assert fields[0]["name"] == "name"
    assert fields[0]["required"] is True


def test_list_schema_fields_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="get",
        url=f"/api/v1/schemas/{_SCHEMA_ID}/fields",
        json_body=None,
        patch_target="_load_latest_definition",
        expected={(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


# ---------------------------------------------------------------------------
# Inference helpers — connector sampling + model backend resolution
# ---------------------------------------------------------------------------


def _infer_ci(connector_type: str = "github") -> MagicMock:
    ci = MagicMock()
    ci.organisation_id = _ORG_ID
    ci.connector_type_id = connector_type
    ci.name = "Repo connector"
    return ci


def _mbs_ok() -> MagicMock:
    mbs = MagicMock()
    mbs.items = [MagicMock()]
    return mbs


def test_sample_connector_records_init_failure_returns_502() -> None:

    from modulo.api.routes.schemas import SchemaInferRequest, SchemaSampleQuery, _sample_connector_records

    hub = MagicMock()
    hub.__aenter__ = AsyncMock(return_value=hub)
    hub.__aexit__ = AsyncMock(return_value=False)
    hub.initialise = AsyncMock(side_effect=RuntimeError("connector down"))
    settings = _make_settings()
    req = SchemaInferRequest(
        connector_instance_id=uuid.uuid4(),
        sample_query=SchemaSampleQuery(resource="issues"),
    )

    async def _run() -> None:
        session = _make_session()
        with (
            patch(f"{_PREFIX}ConnectorHub", return_value=hub),
            patch(f"{_PREFIX}create_secrets_backend", return_value=MagicMock()),
            patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        ):
            await _sample_connector_records(settings, _infer_ci(), req, session)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_run())
    assert excinfo.value.status_code == 502
    assert "Failed to initialise connector" in str(excinfo.value.detail)


def test_sample_connector_records_timeout_returns_504() -> None:

    from modulo.api.routes.schemas import SchemaInferRequest, SchemaSampleQuery, _sample_connector_records

    hub = MagicMock()
    hub.__aenter__ = AsyncMock(return_value=hub)
    hub.__aexit__ = AsyncMock(return_value=False)
    hub.initialise = AsyncMock()
    hub.sample = AsyncMock(side_effect=TimeoutError())
    settings = _make_settings()
    req = SchemaInferRequest(
        connector_instance_id=uuid.uuid4(),
        sample_query=SchemaSampleQuery(resource="issues"),
    )

    async def _run() -> None:
        session = _make_session()
        with (
            patch(f"{_PREFIX}ConnectorHub", return_value=hub),
            patch(f"{_PREFIX}create_secrets_backend", return_value=MagicMock()),
            patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        ):
            await _sample_connector_records(settings, _infer_ci(), req, session)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_run())
    assert excinfo.value.status_code == 504


def test_sample_connector_records_sampling_failure_returns_502() -> None:

    from modulo.api.routes.schemas import SchemaInferRequest, SchemaSampleQuery, _sample_connector_records

    hub = MagicMock()
    hub.__aenter__ = AsyncMock(return_value=hub)
    hub.__aexit__ = AsyncMock(return_value=False)
    hub.initialise = AsyncMock()
    hub.sample = AsyncMock(side_effect=ValueError("bad sample"))
    settings = _make_settings()
    req = SchemaInferRequest(
        connector_instance_id=uuid.uuid4(),
        sample_query=SchemaSampleQuery(resource="issues"),
    )

    async def _run() -> None:
        session = _make_session()
        with (
            patch(f"{_PREFIX}ConnectorHub", return_value=hub),
            patch(f"{_PREFIX}create_secrets_backend", return_value=MagicMock()),
            patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        ):
            await _sample_connector_records(settings, _infer_ci(), req, session)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_run())
    assert excinfo.value.status_code == 502
    assert "Failed to sample connector data" in str(excinfo.value.detail)


def test_resolve_model_backend_failure_branches() -> None:

    from modulo.api.routes.schemas import _resolve_model_backend

    async def _run(hub: MagicMock) -> None:
        session = _make_session()
        with (
            patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        ):
            await _resolve_model_backend(
                hub,
                _mbs_ok(),
                MagicMock(),
                session=session,
                organisation_id=_ORG_ID,
                init_log="init",
                init_detail="init failed",
                empty_detail="no backends",
                get_log="get",
                get_detail="get failed",
            )

    # initialise raises -> 502
    hub = MagicMock()
    hub.__aenter__ = AsyncMock(return_value=hub)
    hub.__aexit__ = AsyncMock(return_value=False)
    hub.initialise = AsyncMock(side_effect=RuntimeError("down"))
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_run(hub))
    assert excinfo.value.status_code == 502

    # no backend ids -> 503
    hub = MagicMock()
    hub.__aenter__ = AsyncMock(return_value=hub)
    hub.__aexit__ = AsyncMock(return_value=False)
    hub.initialise = AsyncMock()
    hub.backend_ids = []
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_run(hub))
    assert excinfo.value.status_code == 503

    # get() raises -> 502
    hub = MagicMock()
    hub.__aenter__ = AsyncMock(return_value=hub)
    hub.__aexit__ = AsyncMock(return_value=False)
    hub.initialise = AsyncMock()
    hub.backend_ids = [uuid.uuid4()]
    hub.get = AsyncMock(side_effect=RuntimeError("gone"))
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_run(hub))
    assert excinfo.value.status_code == 502


def test_resolve_model_backend_happy_path_returns_backend() -> None:

    from modulo.api.routes.schemas import _resolve_model_backend

    backend = MagicMock()
    hub = MagicMock()
    hub.__aenter__ = AsyncMock(return_value=hub)
    hub.__aexit__ = AsyncMock(return_value=False)
    hub.initialise = AsyncMock()
    backend_id = uuid.uuid4()
    hub.backend_ids = [backend_id]
    hub.get = AsyncMock(return_value=backend)

    async def _run() -> tuple:
        session = _make_session()
        with patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock):
            return await _resolve_model_backend(
                hub,
                _mbs_ok(),
                MagicMock(),
                session=session,
                organisation_id=_ORG_ID,
                init_log="init",
                init_detail="init failed",
                empty_detail="no backends",
                get_log="get",
                get_detail="get failed",
            )

    resolved_backend, resolved_id = asyncio.run(_run())
    assert resolved_backend is backend
    assert resolved_id == backend_id


# ---------------------------------------------------------------------------
# POST /infer — context guards + error mapping + happy path
# ---------------------------------------------------------------------------

_INFER_URL = "/api/v1/schemas/infer"
_INFER_BODY = {
    "connector_instance_id": str(uuid.uuid4()),
    "sample_query": {"resource": "issues", "filters": {}, "limit": 10},
}


def test_infer_unknown_connector_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}get_connector_instance", new=AsyncMock(return_value=None)))
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        stack.enter_context(patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock))
        resp = http.post(_INFER_URL, json=_INFER_BODY)

    assert resp.status_code == 404, resp.text


def test_infer_unsupported_connector_type_returns_400(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        ci = _infer_ci("filesystem")
        stack.enter_context(patch(f"{_PREFIX}get_connector_instance", new=AsyncMock(return_value=ci)))
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        stack.enter_context(patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock))
        resp = http.post(_INFER_URL, json=_INFER_BODY)

    assert resp.status_code == 400, resp.text
    assert "does not support schema inference" in resp.json()["detail"]


def test_infer_no_model_backends_returns_400(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    mbs = MagicMock()
    mbs.items = []
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}get_connector_instance", new=AsyncMock(return_value=_infer_ci())))
        stack.enter_context(patch(f"{_PREFIX}list_model_backends", new=AsyncMock(return_value=mbs)))
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        stack.enter_context(patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock))
        resp = http.post(_INFER_URL, json=_INFER_BODY)

    assert resp.status_code == 400, resp.text


def test_infer_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    for exc, expected in [(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)]:
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}get_connector_instance", new=AsyncMock(side_effect=exc)))
            stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
            stack.enter_context(patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock))
            resp = http.post(_INFER_URL, json=_INFER_BODY)
        assert resp.status_code == expected, f"{exc!r}: {resp.text}"


def test_infer_happy_path_returns_suggestion(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    records = [{"name": "a", "stars": 5}, {"name": "b", "stars": 7}]
    definition = {"type": "object", "properties": {"name": {"type": "string"}}}
    ci = _infer_ci()
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}_resolve_infer_context", new=AsyncMock(return_value=(ci, _mbs_ok()))))
        stack.enter_context(patch(f"{_PREFIX}_sample_connector_records", new=AsyncMock(return_value=records)))
        inferred = (definition, uuid.uuid4())
        stack.enter_context(patch(f"{_PREFIX}_infer_definition", new=AsyncMock(return_value=inferred)))
        stack.enter_context(patch(f"{_PREFIX}flag_rare_fields", return_value=["stars"]))
        stack.enter_context(patch(f"{_PREFIX}append_audit_event_isolated", new=AsyncMock()))
        resp = http.post(_INFER_URL, json=_INFER_BODY)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["definition_json"] == definition
    assert body["sample_count"] == 2
    assert body["rare_fields"] == ["stars"]
    assert "Repo connector" in body["suggestion_name"]


# ---------------------------------------------------------------------------
# POST /generate — no-backend 400, error matrix, service failure
# ---------------------------------------------------------------------------

_GENERATE_URL = "/api/v1/schemas/generate"
_GENERATE_BODY = {"description": "A person record", "examples": [{"name": "x"}]}


def test_generate_no_model_backends_returns_400(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    mbs = MagicMock()
    mbs.items = []
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}list_model_backends", new=AsyncMock(return_value=mbs)))
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        stack.enter_context(patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock))
        resp = http.post(_GENERATE_URL, json=_GENERATE_BODY)

    assert resp.status_code == 400, resp.text


def test_generate_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    for exc, expected in [(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)]:
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}list_model_backends", new=AsyncMock(side_effect=exc)))
            stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
            stack.enter_context(patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock))
            resp = http.post(_GENERATE_URL, json=_GENERATE_BODY)
        assert resp.status_code == expected, f"{exc!r}: {resp.text}"


def test_generate_service_failure_returns_502(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    mbs = _mbs_ok()
    hub = MagicMock()
    hub.__aenter__ = AsyncMock(return_value=hub)
    hub.__aexit__ = AsyncMock(return_value=False)
    hub.initialise = AsyncMock()
    backend_id = uuid.uuid4()
    hub.backend_ids = [backend_id]
    hub.get = AsyncMock(return_value=MagicMock())
    service = MagicMock()
    service.generate = AsyncMock(side_effect=SchemaGenerationError("llm refused"))
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}list_model_backends", new=AsyncMock(return_value=mbs)))
        stack.enter_context(patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock))
        stack.enter_context(patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock))
        stack.enter_context(patch(f"{_PREFIX}ModelBackendHub", return_value=hub))
        stack.enter_context(patch(f"{_PREFIX}create_secrets_backend", return_value=MagicMock()))
        stack.enter_context(patch(f"{_PREFIX}SchemaGenerationService", return_value=service))
        stack.enter_context(patch(f"{_PREFIX}append_audit_event_isolated", new=AsyncMock()))
        resp = http.post(_GENERATE_URL, json=_GENERATE_BODY)

    assert resp.status_code == 502, resp.text
    assert "Schema generation failed" in resp.json()["detail"]


def test_generate_happy_path_returns_definition(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    definition = {"type": "object", "properties": {"name": {"type": "string"}}}
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}list_model_backends", new=AsyncMock(return_value=_mbs_ok())))
        stack.enter_context(patch(f"{_PREFIX}_generate_schema", new=AsyncMock(return_value=(definition, uuid.uuid4()))))
        stack.enter_context(patch(f"{_PREFIX}append_audit_event_isolated", new=AsyncMock()))
        resp = http.post(_GENERATE_URL, json=_GENERATE_BODY)

    assert resp.status_code == 200, resp.text
    assert resp.json()["definition_json"] == definition


# ---------------------------------------------------------------------------
# POST /migrate + /migrate/plan — failure paths + dry-run
# ---------------------------------------------------------------------------

_MIGRATE_BODY = {
    "from_schema_id": str(_SCHEMA_ID),
    "to_schema_id": str(uuid.uuid4()),
    "data": {"name": "x"},
}


def test_migrate_no_versions_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with (
        patch(f"{_PREFIX}_assert_owns_schema", new=AsyncMock(return_value=_make_schema())),
        patch(f"{_PREFIX}_get_latest_version", new=AsyncMock(return_value=None)),
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
    ):
        resp = http.post("/api/v1/schemas/migrate", json=_MIGRATE_BODY)

    assert resp.status_code == 404, resp.text
    assert "no versions" in resp.json()["detail"]


def test_migrate_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    for exc, expected in [(_INTEGRITY, 409), (_PROG, 501), (_SQL, 503), (_RUNTIME, 500)]:
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}_load_migration_versions", new=AsyncMock(side_effect=exc)))
            resp = http.post("/api/v1/schemas/migrate", json=_MIGRATE_BODY)
        assert resp.status_code == expected, f"{exc!r}: {resp.text}"


def test_migrate_plan_failure_returns_500(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    from_sv = _make_schema_version(_SCHEMA_ID)
    to_sv = _make_schema_version(uuid.uuid4())
    with (
        patch(f"{_PREFIX}_load_migration_versions", new=AsyncMock(return_value=(from_sv, to_sv))),
        patch(f"{_PREFIX}create_migration", side_effect=ValueError("bad definitions")),
    ):
        resp = http.post("/api/v1/schemas/migrate", json=_MIGRATE_BODY)

    assert resp.status_code == 500, resp.text


def test_migrate_apply_failure_returns_500(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    from_sv = _make_schema_version(_SCHEMA_ID)
    to_sv = _make_schema_version(uuid.uuid4())
    plan = MagicMock()
    plan.field_additions = []
    plan.field_removals = []
    plan.type_changes = {}
    plan.renames = []
    with (
        patch(f"{_PREFIX}_load_migration_versions", new=AsyncMock(return_value=(from_sv, to_sv))),
        patch(f"{_PREFIX}create_migration", return_value=plan),
        patch(f"{_PREFIX}apply_migration", side_effect=ValueError("bad data")),
        patch(f"{_PREFIX}append_audit_event_isolated", new=AsyncMock()),
    ):
        resp = http.post("/api/v1/schemas/migrate", json=_MIGRATE_BODY)

    assert resp.status_code == 500, resp.text


def test_migrate_dry_run_returns_plan_without_applying(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    from_sv = _make_schema_version(_SCHEMA_ID)
    to_sv = _make_schema_version(uuid.uuid4())
    plan = MagicMock()
    plan.field_additions = ["new_field"]
    plan.field_removals = []
    plan.type_changes = {}
    plan.renames = []
    with (
        patch(f"{_PREFIX}_load_migration_versions", new=AsyncMock(return_value=(from_sv, to_sv))),
        patch(f"{_PREFIX}create_migration", return_value=plan),
        patch(f"{_PREFIX}apply_migration", new=AsyncMock(side_effect=AssertionError("must not apply on dry-run"))),
        patch(f"{_PREFIX}append_audit_event_isolated", new=AsyncMock()),
    ):
        resp = http.post("/api/v1/schemas/migrate", params={"dry_run": "true"}, json=_MIGRATE_BODY)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan"]["dry_run"] is True
    assert body["plan"]["field_additions"] == ["new_field"]
    assert body["migrated_data"] == {"name": "x"}


def test_migration_plan_compute_failure_returns_500(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with patch(f"{_PREFIX}create_migration", side_effect=ValueError("bad")):
        resp = http.post(
            "/api/v1/schemas/migrate/plan",
            json={"from_definition": {"type": "object"}, "to_definition": {"type": "object"}},
        )

    assert resp.status_code == 500, resp.text


# ---------------------------------------------------------------------------
# POST /validate + /import — helpers and error shapes
# ---------------------------------------------------------------------------


def test_validate_schema_valid_and_invalid(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    good = http.post("/api/v1/schemas/validate", json={"definition": {"type": "object"}})
    assert good.status_code == 200
    assert good.json()["valid"] is True

    bad = http.post("/api/v1/schemas/validate", json={"definition": {"type": "not-a-type"}})
    assert bad.status_code == 200
    body = bad.json()
    assert body["valid"] is False
    assert body["errors"]


def test_step_json_path_and_location_helpers() -> None:
    from modulo.api.routes.schemas import _find_json_location, _json_path_exists, _step_json_path

    assert _step_json_path({"a": {"b": 1}}, "a") == {"b": 1}
    assert _step_json_path(["x"], "0") == "x"
    assert _step_json_path(["x"], "5") is None
    assert _step_json_path("scalar", "a") is None
    assert _json_path_exists({"a": 1}, ["a"])
    assert _json_path_exists({"a": {"b": 1}}, ["a", "b"])
    assert not _json_path_exists(["x"], ["5"])

    raw = '{\n  "properties": {\n    "name": {"type": "string"}\n  }\n}'
    line, _col = _find_json_location(raw, "/properties/name")
    assert line == 3
    line_none, col_none = _find_json_location(raw, "/missing/path")
    assert line_none is None
    assert col_none is None
    line_bad, col_bad = _find_json_location("not json", "/x")
    assert line_bad is None
    assert col_bad is None


def test_import_invalid_json_returns_400(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client

    resp = http.post("/api/v1/schemas/import", json={"content": "{not json"})

    assert resp.status_code == 400, resp.text
    assert "Invalid JSON" in resp.json()["detail"]


def test_import_non_object_returns_400(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client

    resp = http.post("/api/v1/schemas/import", json={"content": "[1, 2, 3]"})

    assert resp.status_code == 400, resp.text
    assert "JSON object" in resp.json()["detail"]


def test_import_invalid_json_schema_returns_422(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client

    resp = http.post("/api/v1/schemas/import", json={"content": '{"type": "bogus-type"}'})

    assert resp.status_code == 422, resp.text
    assert "Invalid JSON Schema" in resp.json()["detail"]


def test_import_happy_path_extracts_fields(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    content = (
        '{"title": "Person", "description": "A person", "properties": '
        '{"name": {"type": "string", "description": "Full name"}, "age": {"type": "integer"}}, '
        '"required": ["name"]}'
    )

    resp = http.post("/api/v1/schemas/import", json={"content": content})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Person"
    assert body["description"] == "A person"
    assert len(body["fields"]) == 2
    name_field = next(f for f in body["fields"] if f["name"] == "name")
    assert name_field["required"] is True
    assert name_field["description"] == "Full name"
