"""FAR-248: notification-preferences UI contract round-trip.

HTTP-level contract test through the REAL FastAPI app on Testcontainers
Postgres (app role => RLS applies). It pins the exact wire shape the
``/settings/notifications`` page sends:

* ``PUT`` carries a DELTA - a single category key under
  ``notification_opt_outs`` - and the change round-trips through ``GET``.
* A repeated PUT is an idempotent upsert (one row, same state).
* An unknown top-level key is a 422 (the model is ``extra="forbid"``).
* A category-only PUT does not disturb the stored ``dashboard_level``.
"""

import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.auth.jwt import create_access_token

pytestmark = pytest.mark.integration

os.environ.setdefault("REDIS_URL", "")

_VALID_32 = "a" * 32

_PREFERENCES_PATH = "/api/v1/notifications/in-app/preferences"

_DASHBOARD_LEVELS = {"debug", "info", "warning", "error"}


@pytest_asyncio.fixture
async def prefs_tenant(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> dict[str, Any]:
    """The session's committed org + admin user, with a clean prefs slate.

    Reset (not teardown): the append-only audit trail makes deleting an
    account row illegal, so each test clears only this user's preference rows
    AND the stored dashboard level (which lives on the SESSION-scoped account
    row, so a level written by an earlier test would otherwise leak in and
    make "fresh GET returns the loader default" order-dependent).
    """
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("DELETE FROM notification_preferences WHERE organisation_id = :oid AND account_id = :aid"),
            {"oid": str(test_org), "aid": str(test_user)},
        )
        await conn.execute(
            text(
                "UPDATE accounts "
                "SET preferences = coalesce(preferences, '{}'::jsonb) - 'notification_dashboard_level' "
                "WHERE id = :aid"
            ),
            {"aid": str(test_user)},
        )
    return {"org_id": test_org, "user_id": test_user}


@pytest_asyncio.fixture
async def prefs_client(db_url: str, app_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client against the real app; sessions run under the app role (RLS)."""
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

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(app_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_plan_context] = lambda: None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client

    app.dependency_overrides.clear()


def _auth_headers(tenant: dict[str, Any]) -> dict[str, str]:
    token = create_access_token(
        subject=f"user-{tenant['user_id'].hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(tenant["org_id"]),
        account_id=str(tenant["user_id"]),
        org_role="admin",
        client_kind="browser",
    )
    return {"Authorization": f"Bearer {token}"}


async def _get_preferences(client: AsyncClient, tenant: dict[str, Any]) -> dict[str, Any]:
    response = await client.get(_PREFERENCES_PATH, headers=_auth_headers(tenant))
    assert response.status_code == 200
    return response.json()


async def _put_preferences(
    client: AsyncClient,
    tenant: dict[str, Any],
    body: dict[str, Any],
) -> Any:
    return await client.put(_PREFERENCES_PATH, headers=_auth_headers(tenant), json=body)


async def test_delta_put_round_trips_through_get(
    prefs_client: AsyncClient,
    prefs_tenant: dict[str, Any],
) -> None:
    """Two successive single-key PUTs accumulate - never replace the set.

    The second PUT is the regression guard: a replace-instead-of-merge
    implementation answers the first PUT correctly and then drops the first
    key on the second, which is invisible if every other category starts
    ``False`` (as the previous version of this test asserted).
    """
    initial = await _get_preferences(prefs_client, prefs_tenant)
    opt_outs = initial["notification_opt_outs"]
    assert opt_outs, "GET returned no category keys - the page would render no toggles"
    categories = sorted(opt_outs)
    assert len(categories) >= 2, f"need two categories to prove merge semantics, got {categories}"
    category_a = "run.failed" if "run.failed" in opt_outs else categories[0]
    category_b = next(category for category in categories if category != category_a)
    assert opt_outs[category_a] is False
    assert opt_outs[category_b] is False

    first = await _put_preferences(
        prefs_client,
        prefs_tenant,
        # DELTA: exactly one category key, exactly the field the UI sends.
        {"notification_opt_outs": {category_a: True}},
    )
    assert first.status_code == 200
    assert first.json()["notification_opt_outs"][category_a] is True
    assert first.json()["notification_opt_outs"][category_b] is False

    second = await _put_preferences(
        prefs_client,
        prefs_tenant,
        {"notification_opt_outs": {category_b: True}},
    )
    assert second.status_code == 200
    # A replace implementation resets category_a to its default (False) here.
    assert second.json()["notification_opt_outs"][category_a] is True
    assert second.json()["notification_opt_outs"][category_b] is True

    reloaded = await _get_preferences(prefs_client, prefs_tenant)
    assert reloaded["notification_opt_outs"][category_a] is True
    assert reloaded["notification_opt_outs"][category_b] is True
    # A single-key delta must not clear the other categories.
    untouched = {
        key: value for key, value in reloaded["notification_opt_outs"].items() if key not in (category_a, category_b)
    }
    assert all(value is False for value in untouched.values())


async def test_repeated_put_is_an_idempotent_upsert(
    prefs_client: AsyncClient,
    prefs_tenant: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    """PUTting the same delta twice leaves one row and the same state."""
    body = {"notification_opt_outs": {"run.failed": True}}

    first = await _put_preferences(prefs_client, prefs_tenant, body)
    second = await _put_preferences(prefs_client, prefs_tenant, body)
    assert first.status_code == 200
    assert second.status_code == 200

    reloaded = await _get_preferences(prefs_client, prefs_tenant)
    assert reloaded["notification_opt_outs"]["run.failed"] is True

    async with db_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM notification_preferences "
                    "WHERE organisation_id = :oid AND account_id = :aid AND category = :cat"
                ),
                {
                    "oid": str(prefs_tenant["org_id"]),
                    "aid": str(prefs_tenant["user_id"]),
                    "cat": "run.failed",
                },
            )
        ).scalar_one()
    assert rows == 1, f"expected one upserted row, found {rows}"


async def test_unknown_top_level_key_is_422(
    prefs_client: AsyncClient,
    prefs_tenant: dict[str, Any],
) -> None:
    """The update model is ``extra="forbid"``: a typo'd key is rejected, not ignored."""
    response = await _put_preferences(
        prefs_client,
        prefs_tenant,
        {"opt_outs": {"run.failed": True}},
    )
    assert response.status_code == 422


async def test_dashboard_level_survives_a_category_only_put(
    prefs_client: AsyncClient,
    prefs_tenant: dict[str, Any],
) -> None:
    """Omitting ``dashboard_level`` leaves the STORED level untouched.

    The level is written to a non-default value first: starting from the
    loader default makes "preserved" and "reset to default" indistinguishable,
    which is exactly the regression this test exists to catch.
    """
    level_put = await _put_preferences(prefs_client, prefs_tenant, {"dashboard_level": "error"})
    assert level_put.status_code == 200
    assert level_put.json()["dashboard_level"] == "error"

    response = await _put_preferences(
        prefs_client,
        prefs_tenant,
        {"notification_opt_outs": {"run.failed": True}},
    )
    assert response.status_code == 200
    assert response.json()["dashboard_level"] == "error"

    reloaded = await _get_preferences(prefs_client, prefs_tenant)
    assert reloaded["dashboard_level"] == "error"


async def test_dashboard_level_defaults_to_the_loader_default(
    prefs_client: AsyncClient,
    prefs_tenant: dict[str, Any],
) -> None:
    """A user with no stored level reads back the LOADER default.

    ``_load_dashboard_level`` returns ``"info"``; the response MODEL declares
    ``"warning"``. The loader is authoritative, so that is what a fresh GET
    returns - assert the loader's value, not the model's declared default.
    ``prefs_tenant`` clears the stored key so this is not order-dependent.
    """
    initial = await _get_preferences(prefs_client, prefs_tenant)
    assert initial["dashboard_level"] == "info"
    assert initial["dashboard_level"] in _DASHBOARD_LEVELS
