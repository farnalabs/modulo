"""Regression test for FAR-140: folder rename/reorder must not 422 after commit.

Before the fix, PATCH /api/v1/pipeline-folders/{id} (rename) and
/api/v1/pipeline-folders/{id}/move returned 422 "Data validation failed." on
every request that actually changed a row: the FolderResponse was validated
AFTER ``session.begin()`` committed, so the server-side ``updated_at``
(onupdate=func.current_timestamp()) was expired at flush and the post-commit
read had no RLS context to refresh it, producing a None ``updated_at`` and a
pydantic ValidationError. A no-op rename (same name) succeeded because no
UPDATE was emitted, so nothing was expired. This test fails on the old code
(422 with the write already committed) and passes with the fix (200).
"""

import os
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.auth.jwt import create_access_token

os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


async def _seed_org(db_engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {
                "id": str(org_id),
                "name": "FolderRenameOrg",
                "slug": f"folder-{org_id.hex[:8]}",
            },
        )
    return org_id


async def _seed_user(db_engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, "
                "auth_provider, active, password_hash) "
                "VALUES (:id, :email, :name, 'local', true, 'hash')",
            ),
            {
                "id": str(account_id),
                "email": f"folder-{account_id.hex[:8]}@test.local",
                "name": "Folder Rename User",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'admin')",
            ),
            {
                "mid": str(uuid.uuid4()),
                "aid": str(account_id),
                "oid": str(org_id),
            },
        )
    return account_id


@pytest_asyncio.fixture
async def integration_client(
    db_url: str,
    app_engine: AsyncEngine,
) -> AsyncGenerator[AsyncClient, None]:
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app
    from modulo.settings import Settings, get_settings

    settings = Settings(
        database_url=db_url,
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_admin_password="",
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        # app_engine sessions run as a non-superuser role, so RLS actually
        # filters rows (the testcontainers superuser bypasses RLS even under
        # FORCE ROW LEVEL SECURITY).
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client

    app.dependency_overrides.clear()


def _token(org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role="admin",
        is_system_admin=False,
    )


class TestPipelineFolderRenameCommit:
    """FAR-140 regression: rename and reorder must return 200 and persist."""

    async def test_rename_returns_200_and_persists(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        org_id = await _seed_org(db_engine)
        user_id = await _seed_user(db_engine, org_id)
        headers = {"Authorization": f"Bearer {_token(org_id, user_id)}"}

        create_resp = await integration_client.post(
            "/api/v1/pipeline-folders",
            json={"name": "Original", "parent_id": None},
            headers=headers,
        )
        assert create_resp.status_code == 201, create_resp.text
        folder_id = create_resp.json()["id"]

        rename_resp = await integration_client.patch(
            f"/api/v1/pipeline-folders/{folder_id}",
            json={"name": "Renamed"},
            headers=headers,
        )
        assert rename_resp.status_code == 200, rename_resp.text
        assert rename_resp.json()["name"] == "Renamed"

        list_resp = await integration_client.get("/api/v1/pipeline-folders", headers=headers)
        assert list_resp.status_code == 200, list_resp.text
        names = {folder["name"] for folder in list_resp.json()}
        assert "Renamed" in names, "new name must be persisted"
        assert "Original" not in names, "old name must be gone"

    async def test_reorder_returns_200_and_persists(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        org_id = await _seed_org(db_engine)
        user_id = await _seed_user(db_engine, org_id)
        headers = {"Authorization": f"Bearer {_token(org_id, user_id)}"}

        create_resp = await integration_client.post(
            "/api/v1/pipeline-folders",
            json={"name": "Sortable", "parent_id": None},
            headers=headers,
        )
        assert create_resp.status_code == 201, create_resp.text
        folder_id = create_resp.json()["id"]

        move_resp = await integration_client.patch(
            f"/api/v1/pipeline-folders/{folder_id}/move",
            json={"sort_order": 5},
            headers=headers,
        )
        assert move_resp.status_code == 200, move_resp.text
        assert move_resp.json()["sort_order"] == 5

        list_resp = await integration_client.get("/api/v1/pipeline-folders", headers=headers)
        assert list_resp.status_code == 200, list_resp.text
        by_id = {folder["id"]: folder for folder in list_resp.json()}
        assert by_id[folder_id]["sort_order"] == 5, "sort_order must be persisted"
