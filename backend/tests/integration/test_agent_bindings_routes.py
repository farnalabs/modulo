"""Agent runner-binding ROUTE tests (FAR-592 / D6, qa fixes).

HTTP-level integration coverage through the real app on Testcontainers
Postgres (app role => RLS applies):

* PUT reserved target_env_var -> 422 with the validator message (F7 — the
  validator is ValueError-shaped and must never surface as a 500).
* DELETE of a binding that belongs to a DIFFERENT agent under the same org
  -> 404 and NO audit row (F8 — scoped delete, no phantom audit); a real
  delete DOES append the audit event (positive control).
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

_INSERT_ORG_SQL = text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :n, :s, '{}'::json)")

_INSERT_ACCOUNT_SQL = text(
    "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
    "VALUES (:id, :e, :n, 'h', 'local', true)"
)

_INSERT_MEMBERSHIP_SQL = text(
    "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:mid, :aid, :oid, 'admin')"
)

_INSERT_AGENT_SQL = text(
    "INSERT INTO agents (id, organisation_id, name, prompt_template, account_id, "
    "is_executable, prompt_always_visible, prompt_version_history, connector_type_refs, "
    "required_environment_capabilities, retry_policy) "
    "VALUES (:id, :oid, :n, 't', :uid, true, false, '[]'::json, '[]'::json, '[]'::json, '{}'::json)"
)

# Plain INSERT (ciphertext placeholder): the PUT path validates the var shape
# BEFORE any hub/secret involvement — the 422 must fire with no decryption.
_INSERT_MODEL_BACKEND_SQL = text(
    "INSERT INTO model_backends (id, organisation_id, name, display_name, provider, model_id, "
    "credentials_ciphertext, account_id, default_params, cost_tracking, visibility, tier) "
    "VALUES (:id, :oid, :n, 'd', 'anthropic', 'm', decode('636970686572','hex'), :uid, "
    "'{}'::json, 'enabled', 'org', 'native')"
)

_INSERT_BINDING_SQL = text(
    "INSERT INTO agent_runner_bindings (id, organisation_id, agent_id, model_backend_id, "
    "target_env_var, source_field, account_id) "
    "VALUES (:id, :oid, :aid, :bid, :target, :source, :uid)"
)


@pytest_asyncio.fixture(scope="module")
async def bindings_org(db_engine: AsyncEngine) -> dict[str, Any]:
    """One committed org + user + two agents + one backend.

    Agent B exists in the SAME org so the cross-agent delete test can prove
    the delete is agent-scoped (an org-only RLS check would still allow it —
    both bindings share the organisation).
    """
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    backend_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            _INSERT_ORG_SQL,
            {"id": str(org_id), "n": "Bindings Route Org", "s": f"bnd-{org_id.hex[:8]}"},
        )
        await conn.execute(
            _INSERT_ACCOUNT_SQL,
            {"id": str(user_id), "e": f"bnd-{org_id.hex[:8]}@example.com", "n": "Bindings Admin"},
        )
        await conn.execute(
            _INSERT_MEMBERSHIP_SQL,
            {"mid": str(uuid.uuid4()), "aid": str(user_id), "oid": str(org_id)},
        )
        await conn.execute(
            _INSERT_AGENT_SQL,
            {"id": str(agent_a), "oid": str(org_id), "n": "BindingsAgentA", "uid": str(user_id)},
        )
        await conn.execute(
            _INSERT_AGENT_SQL,
            {"id": str(agent_b), "oid": str(org_id), "n": "BindingsAgentB", "uid": str(user_id)},
        )
        await conn.execute(
            _INSERT_MODEL_BACKEND_SQL,
            {
                "id": str(backend_id),
                "oid": str(org_id),
                "n": f"BindRoute-MB-{uuid.uuid4().hex[:6]}",
                "uid": str(user_id),
            },
        )
    return {
        "org_id": org_id,
        "user_id": user_id,
        "agent_a": agent_a,
        "agent_b": agent_b,
        "backend_id": backend_id,
    }


@pytest_asyncio.fixture
async def bindings_client(db_url: str, app_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
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
        # app_engine sessions run as a non-superuser role, so RLS policies
        # actually filter (the testcontainers superuser bypasses even FORCE RLS).
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


def _token(org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role="admin",
        client_kind="browser",
    )


def _auth_headers(payload: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(payload['org_id'], payload['user_id'])}"}


async def test_put_bindings_reserved_target_returns_422(
    bindings_client: AsyncClient,
    bindings_org: dict[str, Any],
) -> None:
    payload = bindings_org
    resp = await bindings_client.put(
        f"/api/v1/agents/{payload['agent_a']}/bindings",
        headers=_auth_headers(payload),
        json={
            "bindings": [
                {
                    "model_backend_id": str(payload["backend_id"]),
                    "target_env_var": "MODULO_API_KEY",
                    "source_field": "api_key",
                }
            ]
        },
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json().get("detail", "")
    assert "reserved" in detail
    assert "MODULO_API_KEY" in detail


async def test_delete_cross_agent_binding_returns_404_and_no_audit_row(
    bindings_client: AsyncClient,
    bindings_org: dict[str, Any],
    db_engine: AsyncEngine,
) -> None:
    payload = bindings_org
    own_binding_id = uuid.uuid4()
    foreign_binding_id = uuid.uuid4()
    foreign_agent_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            _INSERT_AGENT_SQL,
            {
                "id": str(foreign_agent_id),
                "oid": str(payload["org_id"]),
                "n": "BindingsAgentC",
                "uid": str(payload["user_id"]),
            },
        )
        await conn.execute(
            _INSERT_BINDING_SQL,
            {
                "id": str(own_binding_id),
                "oid": str(payload["org_id"]),
                "aid": str(payload["agent_a"]),
                "bid": str(payload["backend_id"]),
                "target": "OWN_VAR_CA9adf",
                "source": "api_key",
                "uid": str(payload["user_id"]),
            },
        )
        await conn.execute(
            _INSERT_BINDING_SQL,
            {
                "id": str(foreign_binding_id),
                "oid": str(payload["org_id"]),
                "aid": str(foreign_agent_id),
                "bid": str(payload["backend_id"]),
                "target": "FOREIGN_VAR_3f8c1b",
                "source": "api_key",
                "uid": str(payload["user_id"]),
            },
        )

    # F8: the DELETE route resolves the binding scoped to the path's agent —
    # agent C's binding under agent A's path is a plain 404.
    resp = await bindings_client.delete(
        f"/api/v1/agents/{payload['agent_a']}/bindings/{foreign_binding_id}",
        headers=_auth_headers(payload),
    )
    assert resp.status_code == 404, resp.text
    assert resp.json().get("detail") == "Binding not found"

    async with db_engine.connect() as conn, conn.begin():
        audited = (
            await conn.execute(
                text("SELECT count(*) FROM audit_events WHERE payload_json->>'binding_id' = :bid"),
                {"bid": str(foreign_binding_id)},
            )
        ).scalar_one()
    # No phantom audit row: the delete never happened.
    assert audited == 0

    # Positive control: deleting the agent's OWN binding succeeds AND appends
    # the audit event inside the committed transaction.
    resp_own = await bindings_client.delete(
        f"/api/v1/agents/{payload['agent_a']}/bindings/{own_binding_id}",
        headers=_auth_headers(payload),
    )
    assert resp_own.status_code == 204, resp_own.text

    async with db_engine.connect() as conn, conn.begin():
        audited_ok = (
            await conn.execute(
                text("SELECT count(*) FROM audit_events WHERE payload_json->>'binding_id' = :bid"),
                {"bid": str(own_binding_id)},
            )
        ).scalar_one()
    assert audited_ok == 1
