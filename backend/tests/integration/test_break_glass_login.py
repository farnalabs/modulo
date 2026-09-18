"""Integration tests for the break-glass login hook (deliverable B).

Full recovery cycle against a real Postgres (testcontainers):

* a live break-glass credential logs in 200 and the CAS consumes it,
* the same credential's second login fails 401 (CAS rowcount 0),
* expired / deactivated credentials fail 401 (early deny),
* refresh is denied once the credential is no longer live (no write).
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.auth.passwords import hash_password
from modulo.settings import Settings, get_settings

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32
_PASSWORD = "recovery-password-123"


# ---------------------------------------------------------------------------
# DB seed helpers (superuser engine — the app role cannot write bg columns)
# ---------------------------------------------------------------------------


async def _create_org(engine: AsyncEngine) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"BGL {org_id.hex[:8]}", "slug": f"bgl-{org_id.hex[:8]}"},
        )
    return org_id


async def _create_bg_account(
    engine: AsyncEngine,
    *,
    email: str,
    password: str,
    expires_at: datetime | None,
    deactivated_at: datetime | None = None,
) -> uuid.UUID:
    acc_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active, "
                "is_break_glass, break_glass_expires_at, break_glass_deactivated_at) "
                "VALUES (:id, :email, :name, :hash, 'local', true, true, :exp, :deactivated)"
            ),
            {
                "id": str(acc_id),
                "email": email,
                "name": f"BG Login {acc_id.hex[:8]}",
                "hash": hash_password(password),
                "exp": expires_at,
                "deactivated": deactivated_at,
            },
        )
    return acc_id


async def _create_membership(engine: AsyncEngine, *, org_id: uuid.UUID, account_id: uuid.UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:id, :aid, :oid, 'admin')"
            ),
            {"id": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )


async def _password_hash(engine: AsyncEngine, account_id: uuid.UUID) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(text("SELECT password_hash FROM accounts WHERE id = :id"), {"id": str(account_id)})
        ).scalar_one()


async def _deactivate(engine: AsyncEngine, account_id: uuid.UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE accounts SET break_glass_deactivated_at = now(), "
                "break_glass_expires_at = NULL, password_hash = gen_random_uuid()::text WHERE id = :id"
            ),
            {"id": str(account_id)},
        )


# ---------------------------------------------------------------------------
# HTTP client fixture — FastAPI app wired to the testcontainer database
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(db_url: str, app_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app

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
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as async_client:
        yield async_client

    app.dependency_overrides.clear()


def _login_json(email: str) -> dict[str, str]:
    return {"email": email, "password": _PASSWORD}


# ---------------------------------------------------------------------------
# Full recovery cycle
# ---------------------------------------------------------------------------


async def test_live_break_glass_login_consumes_then_second_fails(db_engine: AsyncEngine, client: AsyncClient) -> None:
    org_id = await _create_org(db_engine)
    email = f"live-{uuid.uuid4().hex[:12]}@example.com"
    acc_id = await _create_bg_account(
        db_engine, email=email, password=_PASSWORD, expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    await _create_membership(db_engine, org_id=org_id, account_id=acc_id)

    first = await client.post("/api/v1/auth/login", json=_login_json(email))
    assert first.status_code == 200, first.text
    assert "access_token" in first.json()

    second = await client.post("/api/v1/auth/login", json=_login_json(email))
    assert second.status_code == 401
    assert second.json()["detail"] == "Incorrect email or password"

    consumed = await _password_hash(db_engine, acc_id)
    assert consumed is not None
    assert not consumed.startswith("$2"), "CAS must replace the bcrypt hash with a spent (non-bcrypt) value"


async def test_expired_break_glass_login_401(client: AsyncClient, db_engine: AsyncEngine) -> None:
    org_id = await _create_org(db_engine)
    email = f"expired-{uuid.uuid4().hex[:12]}@example.com"
    acc_id = await _create_bg_account(
        db_engine, email=email, password=_PASSWORD, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    await _create_membership(db_engine, org_id=org_id, account_id=acc_id)

    resp = await client.post("/api/v1/auth/login", json=_login_json(email))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"


async def test_deactivated_break_glass_login_401(client: AsyncClient, db_engine: AsyncEngine) -> None:
    org_id = await _create_org(db_engine)
    email = f"deactivated-{uuid.uuid4().hex[:12]}@example.com"
    acc_id = await _create_bg_account(
        db_engine,
        email=email,
        password=_PASSWORD,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        deactivated_at=datetime.now(UTC),
    )
    await _create_membership(db_engine, org_id=org_id, account_id=acc_id)

    resp = await client.post("/api/v1/auth/login", json=_login_json(email))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"


async def test_refresh_denied_after_break_glass_deactivation(client: AsyncClient, db_engine: AsyncEngine) -> None:
    """A break-glass session cannot refresh once the credential is deactivated:
    the ADR 017 live-role re-read folds the membership to None and refresh
    denies 401 before any family write."""
    org_id = await _create_org(db_engine)
    email = f"refresh-{uuid.uuid4().hex[:12]}@example.com"
    acc_id = await _create_bg_account(
        db_engine, email=email, password=_PASSWORD, expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    await _create_membership(db_engine, org_id=org_id, account_id=acc_id)

    login = await client.post("/api/v1/auth/login", json=_login_json(email))
    assert login.status_code == 200, login.text
    refresh_token = login.json()["refresh_token"]

    await _deactivate(db_engine, acc_id)

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 401
