"""BDD step definitions for node-category deletion referential integrity.

Supports ``features/admin/node-categories.feature`` — the delete contract:
an unreferenced category is deleted (204), a category still referenced by a
pipeline node is refused (409 with the referencing pipeline named), and a
viewer is forbidden (403).
"""

import contextlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/admin/node-categories.feature")

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
CATEGORY_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")

_CATEGORY_PATH = f"/api/v1/node-categories/{CATEGORY_ID}"


@pytest.fixture
def nc_ctx() -> dict[str, Any]:
    """Shared mutable context for the node-categories step definitions."""
    return {"role": "admin", "referencing": []}


def _make_session(ctx: dict[str, Any]) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    authz_result = MagicMock()
    authz_result.scalar_one_or_none = MagicMock(return_value=True)
    session.execute = AsyncMock(return_value=authz_result)
    return session


def _build_app(ctx: dict[str, Any]) -> FastAPI:
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.routes.node_categories import router as nc_router
    from modulo.auth.dependencies import get_current_tenant_user, get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
    from modulo.settings import Settings, get_settings

    app = FastAPI()
    app.include_router(nc_router)

    role = ctx["role"]

    def _user() -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            username="tester@test",
            organisation_id=ORG_ID,
            account_id=USER_ID,
            org_role=role,
        )

    def _tenant_user() -> TenantPrincipal:
        return TenantPrincipal(
            username="tester@test",
            organisation_id=ORG_ID,
            account_id=USER_ID,
            org_role=role,
        )

    def _settings() -> Any:
        return Settings(
            database_url="sqlite+aiosqlite:///./test.db",
            secret_key="a" * 32,
            fernet_key="b" * 32,
            modulo_admin_password="testpass",
            modulo_csrf_enabled=False,
        )

    async def _db() -> AsyncMock:
        return _make_session(ctx)

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_current_tenant_user] = _tenant_user
    app.dependency_overrides[get_db_session] = _db
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    return app


@given(parsers.parse('I am {article} {role} of org "{org}"'))
def _given_authenticated(article: str, role: str, org: str, nc_ctx: dict[str, Any]) -> None:
    nc_ctx["role"] = "viewer" if role == "viewer" else "admin"


@given("a node category exists")
def _given_category_exists(nc_ctx: dict[str, Any]) -> None:
    nc_ctx["category_exists"] = True


@given("a node category exists that is not referenced by any pipeline node")
def _given_unreferenced_category(nc_ctx: dict[str, Any]) -> None:
    nc_ctx["category_exists"] = True
    nc_ctx["referencing"] = []


@given(parsers.parse('pipeline "{name}" has a node using the node category'))
def _given_pipeline_uses_category(name: str, nc_ctx: dict[str, Any]) -> None:
    nc_ctx["referencing"] = [{"id": str(uuid.uuid4()), "name": name}]


@when("I delete the node category")
def _when_delete_category(nc_ctx: dict[str, Any], request) -> None:
    from modulo.db.crud.node_category import NodeCategoryInUseError

    def _soft_delete(*_args: object, **_kwargs: object) -> bool:
        if nc_ctx.get("referencing"):
            raise NodeCategoryInUseError(category_id=CATEGORY_ID, pipelines=nc_ctx["referencing"])
        nc_ctx["deleted"] = True
        return True

    app = _build_app(nc_ctx)
    client = TestClient(app)
    with (
        patch("modulo.api.routes.node_categories.soft_delete_node_category", side_effect=_soft_delete),
        patch("modulo.api.routes.node_categories.set_rls_org", AsyncMock()),
        patch("modulo.api.routes.node_categories.set_rls_user_context", AsyncMock()),
    ):
        resp = client.delete(_CATEGORY_PATH)
    request.node._resp = resp
    nc_ctx["response"] = resp


@then("the response status is {code:d}")
def _then_status(code: int, request) -> None:
    assert request.node._resp.status_code == code


@then(parsers.parse('the response detail lists the referencing pipeline "{name}"'))
def _then_detail_lists_pipeline(name: str, request) -> None:
    body = request.node._resp.json()
    assert name in body["detail"]


@then("the node category still exists")
def _then_category_still_exists(nc_ctx: dict[str, Any]) -> None:
    assert nc_ctx.get("deleted") is not True
