"""Integration tests for AgentRunnerBinding CRUD (FAR-592 / D6).

Covers: save-replace + UNIQUE (org, agent, target_env_var), the DB-level
RESTRICT blocking a bound backend delete, org teardown with bindings present,
and RLS org isolation (org A cannot read or resolve org B's bindings).
"""

import json
import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.runner_bindings import resolve_agent_bindings
from modulo.db.crud.agent import create_agent
from modulo.db.crud.agent_runner_binding import (
    delete_binding,
    get_binding,
    list_bindings_for_agent,
    replace_agent_bindings,
)
from modulo.db.crud.model_backend import create_model_backend, delete_model_backend
from modulo.db.rls import set_rls_execution_context, set_rls_org

pytestmark = pytest.mark.integration

# Raw-seed SQL (run through ``db_engine`` — the container superuser, which
# bypasses RLS — so the seed is committed before the app-role paths run).
_INSERT_ORG_SQL = text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :n, :s, '{}'::json)")

_INSERT_AGENT_SQL = text(
    "INSERT INTO agents (id, organisation_id, name, prompt_template, account_id, "
    "is_executable, prompt_always_visible, prompt_version_history, connector_type_refs, "
    "required_environment_capabilities, retry_policy) "
    "VALUES (:id, :oid, :n, 't', :uid, true, false, '[]'::json, '[]'::json, '[]'::json, '{}'::json)"
)

_INSERT_MODEL_BACKEND_SQL = text(
    "INSERT INTO model_backends (id, organisation_id, name, display_name, provider, model_id, "
    "credentials_ciphertext, account_id, default_params, cost_tracking, visibility, tier) "
    "VALUES (:id, :oid, :n, 'd', :provider, :model_id, decode('636970686572','hex'), :uid, "
    "'{}'::json, 'enabled', 'org', 'native')"
)

_INSERT_BINDING_SQL = text(
    "INSERT INTO agent_runner_bindings (id, organisation_id, agent_id, model_backend_id, "
    "target_env_var, source_field, account_id) "
    "VALUES (:id, :oid, :aid, :bid, :target, :source, :uid)"
)

_INSERT_LOCAL_PROFILE_SQL = text(
    "INSERT INTO environment_profiles (id, organisation_id, name, provider_type, config_json, "
    "capabilities_json, secret_refs_json, account_id) "
    "VALUES (:id, :oid, :n, 'local', CAST(:cfg AS json), '[]'::json, '[]'::json, :uid)"
)

_INSERT_SECRET_SQL = text(
    "INSERT INTO secrets (id, organisation_id, key, encrypted_value) VALUES (:id, :oid, :key, :value)"
)


def _binding_spec(backend_id: uuid.UUID, user_id: uuid.UUID, *, target: str = "OPENCODE_API_KEY") -> dict:
    return {
        "_backend_id": backend_id,
        "_account_id": user_id,
        "target_env_var": target,
        "source_field": "api_key",
    }


async def _seed_agent_and_backend(
    rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID, name: str
) -> tuple:
    agent = await create_agent(
        rls_session,
        org_id=test_org,
        name=name,
        account_id=test_user,
        prompt_template="run",
    )
    mb = await create_model_backend(
        rls_session,
        org_id=test_org,
        name=f"BindBackend-{uuid.uuid4().hex[:6]}",
        display_name="B",
        provider="anthropic",
        model_id="stub-model",
        credentials_ciphertext=b"cipher",
        account_id=test_user,
    )
    return agent, mb


async def test_replace_agent_bindings_round_trip(rls_session, test_org, test_user) -> None:
    agent, mb = await _seed_agent_and_backend(rls_session, test_org, test_user, "BindAgent-rt")
    created = await replace_agent_bindings(
        rls_session,
        org_id=test_org,
        agent_id=agent.id,
        bindings_specs=[_binding_spec(mb.id, test_user)],
    )
    assert len(created) == 1
    assert created[0].target_env_var == "OPENCODE_API_KEY"
    assert (await list_bindings_for_agent(rls_session, agent.id))[0].id == created[0].id

    # Replace wholesale with a different var: the old row is gone.
    replaced = await replace_agent_bindings(
        rls_session,
        org_id=test_org,
        agent_id=agent.id,
        bindings_specs=[_binding_spec(mb.id, test_user, target="STRIPE_KEY")],
    )
    assert [b.target_env_var for b in await list_bindings_for_agent(rls_session, agent.id)] == ["STRIPE_KEY"]
    assert await get_binding(rls_session, created[0].id) is None
    assert replaced[0].target_env_var == "STRIPE_KEY"


async def test_unique_target_env_var_per_agent(rls_session, test_org, test_user) -> None:
    agent, mb = await _seed_agent_and_backend(rls_session, test_org, test_user, "BindAgent-dup")
    specs = [_binding_spec(mb.id, test_user), _binding_spec(mb.id, test_user)]
    with pytest.raises(HTTPException) as excinfo:
        await replace_agent_bindings(rls_session, org_id=test_org, agent_id=agent.id, bindings_specs=specs)
    assert "more than once" in str(excinfo.value.detail)


async def test_delete_bound_backend_blocked_at_db_level(rls_session, test_org, test_user) -> None:
    agent, mb = await _seed_agent_and_backend(rls_session, test_org, test_user, "BindAgent-restrict")
    await replace_agent_bindings(
        rls_session, org_id=test_org, agent_id=agent.id, bindings_specs=[_binding_spec(mb.id, test_user)]
    )
    with pytest.raises(HTTPException) as excinfo:
        await delete_model_backend(rls_session, mb.id)
    assert "Cannot delete model backend" in str(excinfo.value.detail)
    assert "Re-bind" in str(excinfo.value.detail)


async def test_rls_org_isolation(db_engine: AsyncEngine, app_engine: AsyncEngine, test_org: uuid.UUID) -> None:
    """Org A cannot read or resolve org B's bindings (RLS org isolation).

    Runs on ``app_engine`` (non-superuser role, RLS applies).
    """
    foreign_org = uuid.uuid4()
    foreign_user = uuid.uuid4()
    foreign_agent = uuid.uuid4()
    foreign_backend = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            _INSERT_ORG_SQL,
            {"id": str(foreign_org), "n": "RLS Other Org", "s": f"rls-{foreign_org.hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :e, :n, 'h', 'local', true)"
            ),
            {"id": str(foreign_user), "e": "rls-bind@example.com", "n": "RLS Bind"},
        )
        await conn.execute(
            _INSERT_AGENT_SQL,
            {"id": str(foreign_agent), "oid": str(foreign_org), "n": "ForeignBindAgent", "uid": str(foreign_user)},
        )
        await conn.execute(
            _INSERT_MODEL_BACKEND_SQL,
            {
                "id": str(foreign_backend),
                "oid": str(foreign_org),
                "n": f"FB-{foreign_backend.hex[:6]}",
                "provider": "anthropic",
                "model_id": "m",
                "uid": str(foreign_user),
            },
        )
        await conn.execute(
            _INSERT_BINDING_SQL,
            {
                "id": str(uuid.uuid4()),
                "oid": str(foreign_org),
                "aid": str(foreign_agent),
                "bid": str(foreign_backend),
                "target": "SECRET_VAR",
                "source": "api_key",
                "uid": str(foreign_user),
            },
        )

    # Under org B's RLS context: org A's agent sees zero bindings in the
    # table (the foreign binding is org B's) and resolution for the foreign
    # agent resolves NOTHING from org A's view.
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, test_org)
        await set_rls_execution_context(session)
        result = await resolve_agent_bindings(
            session_factory=lambda: async_sessionmaker(app_engine, expire_on_commit=False)(),
            org_id=test_org,
            agent_id=foreign_agent,
        )
        # No org-A binding rows exist for that agent -> resolution is empty
        # (RLS confines, never leaks). Nothing org-B may leak through.
        assert not result

    # Read-path: list_bindings (agent FK filter) still respects RLS — for
    # the org-A view the foreign agent's rows are invisible.
    async with factory() as session, session.begin():
        await set_rls_org(session, test_org)
        await set_rls_execution_context(session)
        rows = await list_bindings_for_agent(session, foreign_agent)
        assert not rows


async def test_delete_binding_scoped_to_agent(rls_session, test_org, test_user) -> None:
    """A binding owned by a DIFFERENT same-org agent must NOT be deleted by a
    wrong-agent DELETE: the call returns False and the row survives, so the
    endpoint can 404 instead of deleting the wrong agent's binding and writing
    an audit event naming the wrong resource.
    """
    agent_a, mb_a = await _seed_agent_and_backend(rls_session, test_org, test_user, "BindAgent-A")
    agent_b, mb_b = await _seed_agent_and_backend(rls_session, test_org, test_user, "BindAgent-B")
    created_a = await replace_agent_bindings(
        rls_session, org_id=test_org, agent_id=agent_a.id, bindings_specs=[_binding_spec(mb_a.id, test_user)]
    )
    created_b = await replace_agent_bindings(
        rls_session, org_id=test_org, agent_id=agent_b.id, bindings_specs=[_binding_spec(mb_b.id, test_user)]
    )

    # Wrong-agent DELETE is scoped out: not deleted, row survives.
    assert await delete_binding(rls_session, created_b[0].id, agent_id=agent_a.id) is False
    assert (await get_binding(rls_session, created_b[0].id)) is not None

    # Correct-agent DELETE removes the row.
    assert await delete_binding(rls_session, created_a[0].id, agent_id=agent_a.id) is True
    assert (await get_binding(rls_session, created_a[0].id)) is None


async def test_org_teardown_with_bindings_present(db_engine: AsyncEngine, test_user: uuid.UUID) -> None:
    """RESTRICT + teardown: delete_organisation cannot abort on the bindings."""
    from modulo.db.crud.organisation import delete_organisation

    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    backend_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            _INSERT_ORG_SQL,
            {"id": str(org_id), "n": f"Teardown-{org_id.hex[:6]}", "s": f"td-{org_id.hex[:8]}"},
        )
        await conn.execute(
            _INSERT_AGENT_SQL,
            {"id": str(agent_id), "oid": str(org_id), "n": "TeardownAgent", "uid": str(test_user)},
        )
        await conn.execute(
            _INSERT_MODEL_BACKEND_SQL,
            {
                "id": str(backend_id),
                "oid": str(org_id),
                "n": f"TD-{backend_id.hex[:6]}",
                "provider": "anthropic",
                "model_id": "m",
                "uid": str(test_user),
            },
        )
        await conn.execute(
            _INSERT_BINDING_SQL,
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "aid": str(agent_id),
                "bid": str(backend_id),
                "target": "KEY_VAR",
                "source": "api_key",
                "uid": str(test_user),
            },
        )

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        await set_rls_execution_context(session)
        assert await delete_organisation(session, org_id) is True

    async with db_engine.connect() as conn:
        binding_count = await conn.execute(
            text("SELECT count(*) FROM agent_runner_bindings WHERE organisation_id = :oid"),
            {"oid": str(org_id)},
        )
        assert int(binding_count.scalar_one()) == 0


# ---------------------------------------------------------------------------
# Provision-time resolution (FAR-592 / D6): Local-tier refusal, opt-in,
# decrypt-and-inject, and the export→import name-based round trip.
# ---------------------------------------------------------------------------


async def _seed_resolution_scene(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    profile_opt_in: bool,
    secret_value: str = "stanza-secret-value",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Commit an agent + binding + local profile (+ backend secret) via raw SQL.

    Raw SQL through ``db_engine`` (the container superuser) bypasses RLS for
    the SEED; the resolution itself runs on the app role so RLS applies.
    """
    agent_id = uuid.uuid4()
    backend_id = uuid.uuid4()
    profile_id = uuid.uuid4()

    from cryptography.fernet import Fernet

    from modulo.settings import get_settings

    encrypted_secret = Fernet(get_settings().fernet_key.encode()).encrypt(
        json.dumps({"api_key": secret_value}).encode("utf-8")
    )
    config_json = '{"allow_runner_env_bindings": true}' if profile_opt_in else "{}"

    stmts: list[tuple[Any, dict[str, Any]]] = [
        (
            _INSERT_AGENT_SQL,
            {"id": str(agent_id), "oid": str(org_id), "n": "ResolveAgent", "uid": str(user_id)},
        ),
        (
            _INSERT_MODEL_BACKEND_SQL,
            {
                "id": str(backend_id),
                "oid": str(org_id),
                "n": f"Resolve-{backend_id.hex[:6]}",
                "provider": "custom",
                "model_id": "stub-model",
                "uid": str(user_id),
            },
        ),
        (
            _INSERT_BINDING_SQL,
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "aid": str(agent_id),
                "bid": str(backend_id),
                "target": "OPENCODE_API_KEY",
                "source": "api_key",
                "uid": str(user_id),
            },
        ),
        (
            _INSERT_LOCAL_PROFILE_SQL,
            {
                "id": str(profile_id),
                "oid": str(org_id),
                "n": f"LocalProfile-{profile_id.hex[:6]}",
                "cfg": config_json,
                "uid": str(user_id),
            },
        ),
        (
            _INSERT_SECRET_SQL,
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "key": str(backend_id),
                "value": encrypted_secret,
            },
        ),
    ]
    async with db_engine.connect() as conn, conn.begin():
        for stmt, params in stmts:
            await conn.execute(stmt, params)
    return agent_id, backend_id, profile_id


def _app_session_factory(app_engine: AsyncEngine) -> Any:
    return async_sessionmaker(app_engine, expire_on_commit=False)


async def test_local_profile_refuses_bindings_without_opt_in(
    db_engine: AsyncEngine, app_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """Local + bindings + no opt-in -> typed provision-time refusal (D7 posture)."""
    from modulo.core.runner_bindings import LocalProviderBindingsRefusedError

    agent_id, _backend_id, profile_id = await _seed_resolution_scene(
        db_engine, test_org, test_user, profile_opt_in=False
    )

    with pytest.raises(LocalProviderBindingsRefusedError):
        await resolve_agent_bindings(
            session_factory=_app_session_factory(app_engine),
            org_id=test_org,
            agent_id=agent_id,
            environment_profile_id=profile_id,
        )


async def test_local_profile_opt_in_resolves_decrypted_secret(
    db_engine: AsyncEngine, app_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """Explicit opt-in: the runner env sees the DECRYPTED credential value."""
    agent_id, _backend_id, profile_id = await _seed_resolution_scene(
        db_engine, test_org, test_user, profile_opt_in=True, secret_value="decrypted-stanza-key"
    )

    resolved = await resolve_agent_bindings(
        session_factory=_app_session_factory(app_engine),
        org_id=test_org,
        agent_id=agent_id,
        environment_profile_id=profile_id,
    )
    assert resolved == {"OPENCODE_API_KEY": "decrypted-stanza-key"}


async def test_unknown_source_field_fails_resolution_typed(
    db_engine: AsyncEngine, app_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """A source field the stored secret does not carry -> typed resolution error."""
    from modulo.core.runner_bindings import AgentBindingResolutionError

    agent_id, _backend_id, profile_id = await _seed_resolution_scene(
        db_engine, test_org, test_user, profile_opt_in=True
    )

    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE agent_runner_bindings SET source_field = 'root_password' WHERE agent_id = :aid"),
            {"aid": str(agent_id)},
        )

    with pytest.raises(AgentBindingResolutionError, match="root_password"):
        await resolve_agent_bindings(
            session_factory=_app_session_factory(app_engine),
            org_id=test_org,
            agent_id=agent_id,
            environment_profile_id=profile_id,
        )


async def test_binding_export_import_round_trip(
    db_engine: AsyncEngine, app_engine: AsyncEngine, test_org: uuid.UUID, test_user: uuid.UUID
) -> None:
    """Exported agent carries NAME-BASED bindings; the import rebinds by name."""
    from cryptography.fernet import Fernet

    from modulo.core.workflow_import_export import (
        export_pipeline_bundle,
        extract_bundle_json_from_zip,
        materialize_import,
    )
    from modulo.db.crud.pipeline import create_pipeline
    from modulo.settings import get_settings

    agent_id, backend_id, _profile_id = await _seed_resolution_scene(
        db_engine, test_org, test_user, profile_opt_in=True
    )
    backend_name = f"Resolve-{backend_id.hex[:6]}"

    # Export org A: pipeline -> agent -> binding (backend pulled into the bundle).
    factory = _app_session_factory(app_engine)
    async with factory() as session, session.begin():
        await set_rls_org(session, test_org)
        await set_rls_execution_context(session)
        pipeline = await create_pipeline(
            session,
            org_id=test_org,
            name=f"RT-Pipeline-{backend_id.hex[:6]}",
            account_id=test_user,
        )
        pipeline.graph_nodes_json = [{"agent_id": str(agent_id), "output_schema_id": None}]
        await session.flush()
        data = await export_pipeline_bundle(session, pipeline.id)

    bundle = extract_bundle_json_from_zip(data)
    assert bundle["agents"][0]["model_backend_bindings"] == [
        {"model_backend_name": backend_name, "target_env_var": "OPENCODE_API_KEY", "source_field": "api_key"}
    ]

    # Import into a FRESH org B whose same-named backend is a DIFFERENT row.
    org_b = uuid.uuid4()
    backend_b = uuid.uuid4()
    encrypted_secret = Fernet(get_settings().fernet_key.encode()).encrypt(
        json.dumps({"api_key": "stanza-secret-value"}).encode("utf-8")
    )
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            _INSERT_ORG_SQL,
            {"id": str(org_b), "n": "RT Import Org", "s": f"rt-{org_b.hex[:8]}"},
        )
        await conn.execute(
            _INSERT_MODEL_BACKEND_SQL,
            {
                "id": str(backend_b),
                "oid": str(org_b),
                "n": backend_name,
                "provider": "custom",
                "model_id": "stub-model",
                "uid": str(test_user),
            },
        )
        await conn.execute(
            _INSERT_SECRET_SQL,
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_b),
                "key": str(backend_b),
                "value": encrypted_secret,
            },
        )

    async with factory() as session, session.begin():
        await set_rls_org(session, org_b)
        await set_rls_execution_context(session)
        result = await materialize_import(
            session,
            org_b,
            test_user,
            bundle,
            model_backend_overrides={str(backend_id): str(backend_b)},
        )
        assert not result["warnings"], result["warnings"]
        imported_agent_id = uuid.UUID(result["agents"][str(agent_id)])

        rows = await session.execute(
            text(
                "SELECT model_backend_id, target_env_var, source_field FROM agent_runner_bindings WHERE agent_id = :aid"
            ),
            {"aid": imported_agent_id},
        )
        imported = rows.all()
    assert imported == [(backend_b, "OPENCODE_API_KEY", "api_key")], imported

    # The round-tripped binding RESOLVES in org B against org B's backend.
    resolved = await resolve_agent_bindings(
        session_factory=factory,
        org_id=org_b,
        agent_id=imported_agent_id,
    )
    assert resolved == {"OPENCODE_API_KEY": "stanza-secret-value"}
