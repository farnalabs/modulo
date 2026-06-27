"""Integration tests for Agent CRUD.

Agents have FK constraints on schema_versions (input + output) and model_backends.
Each test that creates an agent uses session-scoped helpers to set those up once.

RLS is set to test_org; all ORM changes are rolled back after each test.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.agent import (
    create_agent,
    delete_agent,
    get_agent,
    list_agents,
    update_agent,
)
from modulo.db.crud.model_backend import create_model_backend
from modulo.db.crud.schema import create_schema, create_schema_version

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Per-test helper: create the schema/version/model_backend prerequisites.
# These are rolled back with the rls_session at end of each test.
# ---------------------------------------------------------------------------


async def _make_prerequisites(
    session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID
) -> tuple[uuid.UUID, str, uuid.UUID]:
    """Return (schema_id, version_string, model_backend_id) for agent creation."""
    schema = await create_schema(
        session,
        org_id=test_org,
        name=f"AgentSchema-{uuid.uuid4().hex[:6]}",
        created_by=test_user,
    )
    sv = await create_schema_version(
        session,
        org_id=test_org,
        schema_id=schema.id,
        version="1.0",
        version_number=1,
        definition_json={"type": "object"},
        created_by=test_user,
    )
    mb = await create_model_backend(
        session,
        org_id=test_org,
        name=f"AgentMB-{uuid.uuid4().hex[:6]}",
        display_name="Stub",
        provider="anthropic",
        model_id="stub-model",
        credentials_ciphertext=b"fake-cipher",
        created_by=test_user,
    )
    return schema.id, sv.version, mb.id


async def test_create_agent(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    schema_id, version, mb_id = await _make_prerequisites(rls_session, test_org, test_user)
    agent = await create_agent(
        rls_session,
        org_id=test_org,
        name="My Agent",
        created_by=test_user,
        input_schema_id=schema_id,
        input_schema_version=version,
        output_schema_id=schema_id,
        output_schema_version=version,
        prompt_template="Do something useful.",
        model_backend_id=mb_id,
    )
    assert agent.id is not None
    assert agent.name == "My Agent"
    assert agent.organisation_id == test_org


async def test_get_agent_returns_existing(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    schema_id, version, mb_id = await _make_prerequisites(rls_session, test_org, test_user)
    agent = await create_agent(
        rls_session,
        org_id=test_org,
        name="Fetch Agent",
        created_by=test_user,
        input_schema_id=schema_id,
        input_schema_version=version,
        output_schema_id=schema_id,
        output_schema_version=version,
        prompt_template="Fetch.",
        model_backend_id=mb_id,
    )
    fetched = await get_agent(rls_session, agent.id)
    assert fetched is not None
    assert fetched.id == agent.id


async def test_get_agent_returns_none_for_unknown(rls_session: AsyncSession) -> None:
    assert await get_agent(rls_session, uuid.uuid4()) is None


async def test_list_agents_pagination(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    schema_id, version, mb_id = await _make_prerequisites(rls_session, test_org, test_user)
    for i in range(3):
        await create_agent(
            rls_session,
            org_id=test_org,
            name=f"List Agent {i}",
            created_by=test_user,
            input_schema_id=schema_id,
            input_schema_version=version,
            output_schema_id=schema_id,
            output_schema_version=version,
            prompt_template="List.",
            model_backend_id=mb_id,
        )
    page1 = await list_agents(rls_session, page=1, page_size=2)
    assert page1.total >= 3
    assert len(page1.items) == 2
    assert page1.page == 1


async def test_update_agent(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    schema_id, version, mb_id = await _make_prerequisites(rls_session, test_org, test_user)
    agent = await create_agent(
        rls_session,
        org_id=test_org,
        name="Update Agent",
        created_by=test_user,
        input_schema_id=schema_id,
        input_schema_version=version,
        output_schema_id=schema_id,
        output_schema_version=version,
        prompt_template="Old template.",
        model_backend_id=mb_id,
    )
    updated = await update_agent(rls_session, agent.id, {"prompt_template": "New template."})
    assert updated is not None
    assert updated.prompt_template == "New template."


async def test_update_agent_unknown_returns_none(rls_session: AsyncSession) -> None:
    assert await update_agent(rls_session, uuid.uuid4(), {"name": "x"}) is None


async def test_delete_agent(rls_session: AsyncSession, test_org: uuid.UUID, test_user: uuid.UUID) -> None:
    schema_id, version, mb_id = await _make_prerequisites(rls_session, test_org, test_user)
    agent = await create_agent(
        rls_session,
        org_id=test_org,
        name="Delete Agent",
        created_by=test_user,
        input_schema_id=schema_id,
        input_schema_version=version,
        output_schema_id=schema_id,
        output_schema_version=version,
        prompt_template="Bye.",
        model_backend_id=mb_id,
    )
    assert await delete_agent(rls_session, agent.id) is True
    assert await get_agent(rls_session, agent.id) is None


async def test_delete_agent_unknown_returns_false(rls_session: AsyncSession) -> None:
    assert await delete_agent(rls_session, uuid.uuid4()) is False
