"""Changed-lines coverage for the eval dataset/suite CRUD routes (FAR-947).

FAR-947 adds the ``/eval-datasets`` and ``/eval-suites`` CRUD handlers plus
their team-scope gates.  ``test_eval_team_scope.py`` exercises the 403/404
team-gate matrix; this module covers the happy paths, the admin-only 403s, the
404s, and the route error convention (IntegrityError->409, SQLAlchemyError->503)
for every new handler so the new-code coverage gate sees the whole surface.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import (
    get_current_tenant_user,
    get_current_tenant_user_or_api_key,
    get_current_user,
)
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_DATASET_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_SUITE_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_SQL = SQLAlchemyError("boom")
_INTEGRITY = IntegrityError("s", {}, Exception())


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


class _BeginRaiser:
    """Async CM whose ``__aenter__`` raises the given exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> None:
        raise self._exc

    async def __aexit__(self, *args: object) -> None:
        return None


def _result(
    *,
    first: Any = None,
    scalar: Any = None,
    one: Any = None,
    rows: list[Any] | None = None,
) -> MagicMock:
    r = MagicMock()
    r.first.return_value = first
    r.scalar.return_value = scalar
    r.scalar_one_or_none.return_value = one
    scalars = MagicMock()
    scalars.all.return_value = [] if rows is None else rows
    r.scalars.return_value = scalars
    r.all.return_value = [] if rows is None else rows
    return r


def _make_dataset_obj(**overrides: Any) -> MagicMock:
    d = MagicMock()
    d.id = overrides.get("id", _DATASET_ID)
    d.organisation_id = _ORG_ID
    d.name = overrides.get("name", "Test Dataset")
    d.visibility = overrides.get("visibility", "team")
    d.owner_team_id = overrides.get("owner_team_id", _TEAM_ID)
    d.version = 1
    d.deleted_at = None
    d.deleted_by = None
    d.created_at = _NOW
    d.updated_at = _NOW
    return d


def _make_suite_obj(**overrides: Any) -> MagicMock:
    s = MagicMock()
    s.id = overrides.get("id", _SUITE_ID)
    s.organisation_id = _ORG_ID
    s.name = overrides.get("name", "Test Suite")
    s.description = overrides.get("description")
    s.visibility = overrides.get("visibility", "team")
    s.owner_team_id = overrides.get("owner_team_id", _TEAM_ID)
    s.version = 1
    s.eval_definition_ids = []
    s.deleted_at = None
    s.created_at = _NOW
    s.updated_at = _NOW
    return s


def _make_session(
    *,
    table: str | None = None,
    scope: tuple[uuid.UUID | None, str | None] = (None, "org"),
    obj: MagicMock | None = None,
    count: int = 1,
    rows: list[Any] | None = None,
    begin_exc: Exception | None = None,
    add_side_effect: Callable[[Any], None] | None = None,
) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    if begin_exc is not None:
        session.begin = MagicMock(return_value=_BeginRaiser(begin_exc))
        return session

    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock(side_effect=add_side_effect)
    session.flush = AsyncMock(return_value=None)
    session.delete = AsyncMock(return_value=None)
    session.refresh = AsyncMock(return_value=None)

    async def _execute(stmt: Any, *_a: Any, **_k: Any) -> MagicMock:
        stmt_str = str(stmt).lower()
        if "team_memberships" in stmt_str:
            return _result(first=MagicMock())
        if table is not None and table in stmt_str:
            return _result(first=scope, scalar=count, one=obj, rows=rows)
        return _result()

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _principal(*, org_role: str) -> TenantPrincipal:
    return TenantPrincipal(
        username=org_role,
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )


def _setup_overrides(session: AsyncMock, principal: TenantPrincipal) -> None:
    async def _override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    async def _override_tenant() -> TenantPrincipal:
        return principal

    app.dependency_overrides[get_db_session] = _override_session
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=principal.username,
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=principal.org_role,
    )
    app.dependency_overrides[get_current_tenant_user] = _override_tenant
    app.dependency_overrides[get_current_tenant_user_or_api_key] = _override_tenant

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan


def _client(session: AsyncMock, principal: TenantPrincipal) -> TestClient:
    _setup_overrides(session, principal)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Eval dataset CRUD
# ---------------------------------------------------------------------------
class TestEvalDatasetCrud:
    def test_create_dataset_happy_path_201(self) -> None:
        def _add(obj: Any) -> None:
            obj.id = _DATASET_ID

        session = _make_session(table="eval_datasets", add_side_effect=_add)
        try:
            resp = _client(session, _principal(org_role="admin")).post(
                "/api/v1/eval-datasets", json={"name": "New Dataset"}
            )
            assert resp.status_code == 201, resp.text
            assert resp.json()["name"] == "New Dataset"
        finally:
            app.dependency_overrides.clear()

    def test_create_dataset_non_admin_403(self) -> None:
        session = _make_session(table="eval_datasets")
        try:
            resp = _client(session, _principal(org_role="operator")).post(
                "/api/v1/eval-datasets", json={"name": "New Dataset"}
            )
            assert resp.status_code == 403, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_create_dataset_foreign_team_404(self) -> None:
        session = _make_session(table="eval_datasets", scope=(None, None))
        try:
            resp = _client(session, _principal(org_role="admin")).post(
                "/api/v1/eval-datasets",
                json={"name": "New Dataset", "owner_team_id": str(_TEAM_ID)},
            )
            assert resp.status_code == 404, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_create_dataset_integrity_error_409(self) -> None:
        session = _make_session(begin_exc=_INTEGRITY)
        try:
            resp = _client(session, _principal(org_role="admin")).post("/api/v1/eval-datasets", json={"name": "Dup"})
            assert resp.status_code == 409, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_create_dataset_sqlalchemy_error_503(self) -> None:
        session = _make_session(begin_exc=_SQL)
        try:
            resp = _client(session, _principal(org_role="admin")).post("/api/v1/eval-datasets", json={"name": "Boom"})
            assert resp.status_code == 503, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_list_datasets_happy_path_200(self) -> None:
        session = _make_session(
            table="eval_datasets",
            count=2,
            rows=[_make_dataset_obj()],
        )
        try:
            resp = _client(session, _principal(org_role="admin")).get("/api/v1/eval-datasets?page=1&page_size=20")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["total"] == 2
            assert len(body["items"]) == 1
        finally:
            app.dependency_overrides.clear()

    def test_list_datasets_sqlalchemy_error_503(self) -> None:
        session = _make_session(begin_exc=_SQL)
        try:
            resp = _client(session, _principal(org_role="admin")).get("/api/v1/eval-datasets")
            assert resp.status_code == 503, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_get_dataset_not_found_404(self) -> None:
        session = _make_session(table="eval_datasets", obj=None)
        try:
            resp = _client(session, _principal(org_role="admin")).get(f"/api/v1/eval-datasets/{_DATASET_ID}")
            assert resp.status_code == 404, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_get_dataset_sqlalchemy_error_503(self) -> None:
        session = _make_session(begin_exc=_SQL)
        try:
            resp = _client(session, _principal(org_role="admin")).get(f"/api/v1/eval-datasets/{_DATASET_ID}")
            assert resp.status_code == 503, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_update_dataset_happy_path_200(self) -> None:
        session = _make_session(table="eval_datasets", obj=_make_dataset_obj(), scope=(None, "org"))
        try:
            resp = _client(session, _principal(org_role="admin")).patch(
                f"/api/v1/eval-datasets/{_DATASET_ID}", json={"name": "Renamed"}
            )
            assert resp.status_code == 200, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_update_dataset_non_admin_403(self) -> None:
        session = _make_session(table="eval_datasets", obj=_make_dataset_obj(), scope=(None, "org"))
        try:
            resp = _client(session, _principal(org_role="operator")).patch(
                f"/api/v1/eval-datasets/{_DATASET_ID}", json={"name": "Renamed"}
            )
            assert resp.status_code == 403, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_update_dataset_not_found_404(self) -> None:
        session = _make_session(table="eval_datasets", obj=None, scope=(None, "org"))
        try:
            resp = _client(session, _principal(org_role="admin")).patch(
                f"/api/v1/eval-datasets/{_DATASET_ID}", json={"name": "Renamed"}
            )
            assert resp.status_code == 404, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_update_dataset_integrity_error_409(self) -> None:
        session = _make_session(begin_exc=_INTEGRITY)
        try:
            resp = _client(session, _principal(org_role="admin")).patch(
                f"/api/v1/eval-datasets/{_DATASET_ID}", json={"name": "Renamed"}
            )
            assert resp.status_code == 409, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_update_dataset_sqlalchemy_error_503(self) -> None:
        session = _make_session(begin_exc=_SQL)
        try:
            resp = _client(session, _principal(org_role="admin")).patch(
                f"/api/v1/eval-datasets/{_DATASET_ID}", json={"name": "Renamed"}
            )
            assert resp.status_code == 503, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_delete_dataset_happy_path_204(self) -> None:
        session = _make_session(table="eval_datasets", obj=_make_dataset_obj(), scope=(None, "org"))
        try:
            resp = _client(session, _principal(org_role="admin")).delete(f"/api/v1/eval-datasets/{_DATASET_ID}")
            assert resp.status_code == 204, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_delete_dataset_non_admin_403(self) -> None:
        session = _make_session(table="eval_datasets", obj=_make_dataset_obj(), scope=(None, "org"))
        try:
            resp = _client(session, _principal(org_role="operator")).delete(f"/api/v1/eval-datasets/{_DATASET_ID}")
            assert resp.status_code == 403, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_delete_dataset_not_found_404(self) -> None:
        session = _make_session(table="eval_datasets", obj=None, scope=(None, "org"))
        try:
            resp = _client(session, _principal(org_role="admin")).delete(f"/api/v1/eval-datasets/{_DATASET_ID}")
            assert resp.status_code == 404, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_delete_dataset_sqlalchemy_error_503(self) -> None:
        session = _make_session(begin_exc=_SQL)
        try:
            resp = _client(session, _principal(org_role="admin")).delete(f"/api/v1/eval-datasets/{_DATASET_ID}")
            assert resp.status_code == 503, resp.text
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Eval suite CRUD
# ---------------------------------------------------------------------------
class TestEvalSuiteCrud:
    def test_create_suite_happy_path_201(self) -> None:
        def _add(obj: Any) -> None:
            obj.id = _SUITE_ID

        session = _make_session(table="eval_suites", add_side_effect=_add)
        try:
            resp = _client(session, _principal(org_role="admin")).post(
                "/api/v1/eval-suites", json={"name": "New Suite", "description": "d"}
            )
            assert resp.status_code == 201, resp.text
            assert resp.json()["name"] == "New Suite"
        finally:
            app.dependency_overrides.clear()

    def test_create_suite_non_admin_403(self) -> None:
        session = _make_session(table="eval_suites")
        try:
            resp = _client(session, _principal(org_role="operator")).post(
                "/api/v1/eval-suites", json={"name": "New Suite"}
            )
            assert resp.status_code == 403, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_create_suite_foreign_team_404(self) -> None:
        session = _make_session(table="eval_suites", scope=(None, None))
        try:
            resp = _client(session, _principal(org_role="admin")).post(
                "/api/v1/eval-suites",
                json={"name": "New Suite", "owner_team_id": str(_TEAM_ID)},
            )
            assert resp.status_code == 404, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_create_suite_integrity_error_409(self) -> None:
        session = _make_session(begin_exc=_INTEGRITY)
        try:
            resp = _client(session, _principal(org_role="admin")).post("/api/v1/eval-suites", json={"name": "Dup"})
            assert resp.status_code == 409, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_create_suite_sqlalchemy_error_503(self) -> None:
        session = _make_session(begin_exc=_SQL)
        try:
            resp = _client(session, _principal(org_role="admin")).post("/api/v1/eval-suites", json={"name": "Boom"})
            assert resp.status_code == 503, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_list_suites_happy_path_200(self) -> None:
        session = _make_session(table="eval_suites", count=1, rows=[_make_suite_obj()])
        try:
            resp = _client(session, _principal(org_role="admin")).get("/api/v1/eval-suites")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["total"] == 1
            assert len(body["items"]) == 1
        finally:
            app.dependency_overrides.clear()

    def test_list_suites_sqlalchemy_error_503(self) -> None:
        session = _make_session(begin_exc=_SQL)
        try:
            resp = _client(session, _principal(org_role="admin")).get("/api/v1/eval-suites")
            assert resp.status_code == 503, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_get_suite_not_found_404(self) -> None:
        session = _make_session(table="eval_suites", obj=None)
        try:
            resp = _client(session, _principal(org_role="admin")).get(f"/api/v1/eval-suites/{_SUITE_ID}")
            assert resp.status_code == 404, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_get_suite_sqlalchemy_error_503(self) -> None:
        session = _make_session(begin_exc=_SQL)
        try:
            resp = _client(session, _principal(org_role="admin")).get(f"/api/v1/eval-suites/{_SUITE_ID}")
            assert resp.status_code == 503, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_update_suite_happy_path_200(self) -> None:
        session = _make_session(table="eval_suites", obj=_make_suite_obj(), scope=(None, "org"))
        try:
            resp = _client(session, _principal(org_role="admin")).patch(
                f"/api/v1/eval-suites/{_SUITE_ID}", json={"name": "Renamed"}
            )
            assert resp.status_code == 200, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_update_suite_non_admin_403(self) -> None:
        session = _make_session(table="eval_suites", obj=_make_suite_obj(), scope=(None, "org"))
        try:
            resp = _client(session, _principal(org_role="operator")).patch(
                f"/api/v1/eval-suites/{_SUITE_ID}", json={"name": "Renamed"}
            )
            assert resp.status_code == 403, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_update_suite_not_found_404(self) -> None:
        session = _make_session(table="eval_suites", obj=None, scope=(None, "org"))
        try:
            resp = _client(session, _principal(org_role="admin")).patch(
                f"/api/v1/eval-suites/{_SUITE_ID}", json={"name": "Renamed"}
            )
            assert resp.status_code == 404, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_update_suite_integrity_error_409(self) -> None:
        session = _make_session(begin_exc=_INTEGRITY)
        try:
            resp = _client(session, _principal(org_role="admin")).patch(
                f"/api/v1/eval-suites/{_SUITE_ID}", json={"name": "Renamed"}
            )
            assert resp.status_code == 409, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_update_suite_sqlalchemy_error_503(self) -> None:
        session = _make_session(begin_exc=_SQL)
        try:
            resp = _client(session, _principal(org_role="admin")).patch(
                f"/api/v1/eval-suites/{_SUITE_ID}", json={"name": "Renamed"}
            )
            assert resp.status_code == 503, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_delete_suite_happy_path_204(self) -> None:
        session = _make_session(table="eval_suites", obj=_make_suite_obj(), scope=(None, "org"))
        try:
            resp = _client(session, _principal(org_role="admin")).delete(f"/api/v1/eval-suites/{_SUITE_ID}")
            assert resp.status_code == 204, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_delete_suite_non_admin_403(self) -> None:
        session = _make_session(table="eval_suites", obj=_make_suite_obj(), scope=(None, "org"))
        try:
            resp = _client(session, _principal(org_role="operator")).delete(f"/api/v1/eval-suites/{_SUITE_ID}")
            assert resp.status_code == 403, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_delete_suite_not_found_404(self) -> None:
        session = _make_session(table="eval_suites", obj=None, scope=(None, "org"))
        try:
            resp = _client(session, _principal(org_role="admin")).delete(f"/api/v1/eval-suites/{_SUITE_ID}")
            assert resp.status_code == 404, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_delete_suite_integrity_error_409(self) -> None:
        session = _make_session(begin_exc=_INTEGRITY)
        try:
            resp = _client(session, _principal(org_role="admin")).delete(f"/api/v1/eval-suites/{_SUITE_ID}")
            assert resp.status_code == 409, resp.text
        finally:
            app.dependency_overrides.clear()

    def test_delete_suite_sqlalchemy_error_503(self) -> None:
        session = _make_session(begin_exc=_SQL)
        try:
            resp = _client(session, _principal(org_role="admin")).delete(f"/api/v1/eval-suites/{_SUITE_ID}")
            assert resp.status_code == 503, resp.text
        finally:
            app.dependency_overrides.clear()
