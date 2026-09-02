"""Route-level integration tests for the ``@handle_db_errors`` decorator.

FAR-159: in most route files the decorator was stacked ABOVE ``@router.*``, so
FastAPI registered the RAW endpoint and the DB-error → 501/503/409 mapping was
bypassed on the real HTTP path (DB failures surfaced as generic 500s). This
suite drives a DB failure through the real ASGI transport and asserts the
mapped status — it FAILS on the old decorator order and PASSES on the fixed
order (``@router.*`` outermost, ``@handle_db_errors`` innermost).

The onboarding ``GET /api/v1/onboarding/status`` handler is the fixture: its
body has no internal try/except, so ``@handle_db_errors`` is the *only* mapping
layer, and its first DB call (``_get_or_create_progress``) is the monkeypatch
target. Patching it to raise each error class exercises the decorator's exact
exception → status mapping through the HTTP path.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.auth.jwt import create_access_token

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


async def _seed_org(db_engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_id), "name": name, "slug": f"{name}-{org_id.hex[:8]}"},
        )
    return org_id


async def _seed_user(db_engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) "
                "VALUES (:id, :email, :name, 'local', true, 'hash')",
            ),
            {"id": str(account_id), "email": email, "name": f"Admin {email}"},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'admin')",
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )
    return account_id


def _token(org_id: uuid.UUID, user_id: uuid.UUID, role: str = "admin") -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role=role,
    )


@pytest_asyncio.fixture(scope="module")
async def org_a(db_engine: AsyncEngine) -> uuid.UUID:
    return await _seed_org(db_engine, "DbErrorMapping-A")


@pytest_asyncio.fixture(scope="module")
async def user_a(db_engine: AsyncEngine, org_a: uuid.UUID) -> uuid.UUID:
    return await _seed_user(db_engine, org_a, "db-error-mapping@test.local")


class TestDbErrorMapping:
    """A DB failure raised inside a decorated handler maps, not generic-500s.

    The onboarding ``/status`` route body has no try/except of its own, so a
    DB exception escapes to ``@handle_db_errors``. With the decorator stacked
    under ``@router.get`` (the fixed order) the wrapper catches it on the HTTP
    path; with the old order (decorator above the router) the raw handler was
    registered and FastAPI returned a generic 500 — this is the regression
    FAR-159 fixed.
    """

    @pytest.fixture
    def patch_progress(self, monkeypatch: pytest.MonkeyPatch):
        import modulo.api.routes.onboarding as onboarding

        def _apply(exc: Exception) -> None:
            async def _raise_progress(session: AsyncSession, org_id: uuid.UUID) -> None:
                raise exc

            monkeypatch.setattr(onboarding, "_get_or_create_progress", _raise_progress)

        return _apply

    async def test_programming_error_maps_to_501_not_500(
        self,
        integration_client: AsyncClient,
        patch_progress,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        patch_progress(ProgrammingError("stmt", {}, Exception("mock: table does not exist")))
        resp = await integration_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {_token(org_a, user_a)}"},
        )
        assert resp.status_code == 501, f"Expected 501, got {resp.status_code}: {resp.text}"
        assert "migration" in resp.json()["detail"].lower()

    async def test_sqlalchemy_error_maps_to_503_not_500(
        self,
        integration_client: AsyncClient,
        patch_progress,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        patch_progress(SQLAlchemyError("mock: connection lost"))
        resp = await integration_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {_token(org_a, user_a)}"},
        )
        assert resp.status_code == 503, f"Expected 503, got {resp.status_code}: {resp.text}"
        assert "unavailable" in resp.json()["detail"].lower()

    async def test_integrity_error_maps_to_409_not_500(
        self,
        integration_client: AsyncClient,
        patch_progress,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        patch_progress(IntegrityError("stmt", {}, Exception("mock: duplicate key")))
        resp = await integration_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {_token(org_a, user_a)}"},
        )
        assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"
        assert "conflict" in resp.json()["detail"].lower()

    async def test_generic_exception_maps_to_500(
        self,
        integration_client: AsyncClient,
        patch_progress,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        patch_progress(RuntimeError("mock: unexpected"))
        resp = await integration_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {_token(org_a, user_a)}"},
        )
        assert resp.status_code == 500, f"Expected 500, got {resp.status_code}: {resp.text}"

    async def test_success_path_returns_200(
        self,
        integration_client: AsyncClient,
        org_a: uuid.UUID,
        user_a: uuid.UUID,
    ) -> None:
        resp = await integration_client.get(
            "/api/v1/onboarding/status",
            headers={"Authorization": f"Bearer {_token(org_a, user_a)}"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert "is_first_run" in resp.json()
