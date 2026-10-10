"""End-to-end API coverage for team soft-delete (FAR-95).

``DELETE /api/v1/teams/{id}`` must soft-delete the team, not hard-delete it.
Exercised against a migrated Postgres with RLS enforced (no mocked sessions):

* the row survives with ``deleted_at`` set — team history is never lost,
* the team disappears from ``GET /teams/{id}``, ``GET /teams``,
  ``GET /admin/teams`` and the caller's ``GET /teams/my``,
* ``PATCH`` on the deleted team 404s,
* the name becomes reusable (``uq_teams_organisation_name`` is a partial
  unique index over non-deleted rows).
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

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
    """Create an account + admin org membership (idempotent under xdist)."""
    async with db_engine.connect() as conn, conn.begin():
        existing = await conn.execute(text("SELECT id FROM accounts WHERE email = :email"), {"email": email})
        row = existing.first()
        if row is not None:
            account_id = uuid.UUID(str(row[0]))
        else:
            account_id = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO accounts (id, email, display_name, "
                    "auth_provider, active, password_hash) "
                    "VALUES (:id, :email, :name, 'local', true, 'hash')",
                ),
                {"id": str(account_id), "email": email, "name": email.split("@", maxsplit=1)[0]},
            )
        membership = await conn.execute(
            text("SELECT id FROM org_memberships WHERE account_id = :aid AND organisation_id = :oid"),
            {"aid": str(account_id), "oid": str(org_id)},
        )
        if membership.first() is None:
            await conn.execute(
                text(
                    "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                    "VALUES (:mid, :aid, :oid, 'admin')",
                ),
                {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
            )
    return account_id


@pytest_asyncio.fixture
async def api_client(
    db_url: str,
    app_engine: AsyncEngine,
) -> AsyncClient:
    """FastAPI app wired to the real DB with RLS enforced (non-superuser role)."""
    from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
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

    class _TeamPlan:
        """Plan stub that grants team_rbac so the gated routes are reachable."""

        def feature_enabled(self, _name: str) -> bool:
            return True

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_plan_context] = lambda: _TeamPlan()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client

    app.dependency_overrides.clear()


def _headers(org_id: uuid.UUID, user_id: uuid.UUID, role: str = "admin") -> dict[str, str]:
    token = create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role=role,
        client_kind="browser",
    )
    return {"Authorization": f"Bearer {token}"}


async def _seed_membership(db_engine: AsyncEngine, org_id: uuid.UUID, team_id: uuid.UUID, user_id: uuid.UUID) -> None:
    from modulo.db.crud.team_membership import add_team_member

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_id)},
        )
        await add_team_member(session, org_id=org_id, team_id=team_id, account_id=user_id, role="operator")


async def _raw_team_row(db_engine: AsyncEngine, org_id: uuid.UUID, team_id: uuid.UUID) -> tuple[str, str | None]:
    """Return ``(name, deleted_at)`` straight from the DB (bypasses ORM scoping)."""
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_id)},
        )
        row = (
            await conn.execute(
                text("SELECT name, deleted_at FROM teams WHERE id = :tid"),
                {"tid": str(team_id)},
            )
        ).first()
    assert row is not None, "the teams row must survive the DELETE (soft delete, not hard delete)"
    return str(row[0]), row[1]


async def test_delete_team_soft_deletes_and_hides_it_everywhere(
    api_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """DELETE /api/v1/teams/{id} sets deleted_at and hides the team from every read surface."""
    org = await _seed_org(db_engine, "SoftDelApiOrg")
    admin = await _seed_user(db_engine, org, f"admin-{uuid.uuid4().hex[:6]}@softdel.local")
    headers = _headers(org, admin)

    name = f"SoftDelete API Team {uuid.uuid4().hex[:6]}"

    # Create through the API so the whole wire flow is exercised end-to-end.
    create_resp = await api_client.post("/api/v1/teams", headers=headers, json={"name": name})
    assert create_resp.status_code == 201, create_resp.text
    team_id = uuid.UUID(create_resp.json()["id"])

    # A membership makes the team visible on GET /teams/my (baseline assertion).
    await _seed_membership(db_engine, org, team_id, admin)

    before = await api_client.get("/api/v1/teams", headers=headers)
    assert before.status_code == 200, before.text
    assert str(team_id) in {t["id"] for t in before.json()["items"]}

    my_before = await api_client.get("/api/v1/teams/my", headers=headers)
    assert my_before.status_code == 200, my_before.text
    assert str(team_id) in {m["team_id"] for m in my_before.json()}

    get_before = await api_client.get(f"/api/v1/teams/{team_id}", headers=headers)
    assert get_before.status_code == 200, get_before.text

    # The delete itself: 204, no body.
    delete_resp = await api_client.delete(f"/api/v1/teams/{team_id}", headers=headers)
    assert delete_resp.status_code == 204, delete_resp.text

    # Row preserved with deleted_at set (raw SQL — the ORM filter hides it).
    row_name, row_deleted_at = await _raw_team_row(db_engine, org, team_id)
    assert row_name == name, "soft delete must not alter the team's stored history"
    assert row_deleted_at is not None, "delete_team must stamp deleted_at, not remove the row"

    # Every read surface now hides the team.
    get_after = await api_client.get(f"/api/v1/teams/{team_id}", headers=headers)
    assert get_after.status_code == 404, get_after.text

    after = await api_client.get("/api/v1/teams", headers=headers)
    assert after.status_code == 200, after.text
    assert str(team_id) not in {t["id"] for t in after.json()["items"]}

    admin_list = await api_client.get("/api/v1/admin/teams", headers=headers)
    assert admin_list.status_code == 200, admin_list.text
    assert str(team_id) not in {t["id"] for t in admin_list.json()["items"]}

    my_after = await api_client.get("/api/v1/teams/my", headers=headers)
    assert my_after.status_code == 200, my_after.text
    assert str(team_id) not in {m["team_id"] for m in my_after.json()}

    # Writes against the deleted team 404 rather than resurrecting it.
    patch_resp = await api_client.patch(
        f"/api/v1/teams/{team_id}",
        headers=headers,
        json={"name": f"{name} Renamed"},
    )
    assert patch_resp.status_code == 404, patch_resp.text
    assert (await _raw_team_row(db_engine, org, team_id))[1] is not None, "a failed PATCH must not clear deleted_at"


async def test_deleted_team_name_is_reusable_through_the_api(
    api_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """A soft-deleted team's name can be re-created (partial unique index)."""
    org = await _seed_org(db_engine, "SoftDelReuseOrg")
    admin = await _seed_user(db_engine, org, f"reuse-{uuid.uuid4().hex[:6]}@softdel.local")
    headers = _headers(org, admin)

    name = f"Reusable API Team {uuid.uuid4().hex[:6]}"

    first = await api_client.post("/api/v1/teams", headers=headers, json={"name": name})
    assert first.status_code == 201, first.text
    first_id = uuid.UUID(first.json()["id"])

    # The name is taken while the team is live.
    while_live = await api_client.post("/api/v1/teams", headers=headers, json={"name": name})
    assert while_live.status_code == 409, while_live.text

    deleted = await api_client.delete(f"/api/v1/teams/{first_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text

    # After soft delete the name frees up, and the old row remains.
    second = await api_client.post("/api/v1/teams", headers=headers, json={"name": name})
    assert second.status_code == 201, second.text
    second_id = uuid.UUID(second.json()["id"])
    assert second_id != first_id

    first_name, first_deleted_at = await _raw_team_row(db_engine, org, first_id)
    assert first_name == name
    assert first_deleted_at is not None

    second_name, second_deleted_at = await _raw_team_row(db_engine, org, second_id)
    assert second_name == name
    assert second_deleted_at is None, "the replacement team must be live"
