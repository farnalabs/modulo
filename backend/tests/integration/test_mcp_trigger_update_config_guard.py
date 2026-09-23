"""Real-session integration coverage for the FAR-1144 MCP trigger config gate.

The unit test ``test_unrecognised_config_key_in_config_json_returns_validation``
mocks ``mcp_server._session`` and therefore cannot observe the commit that
``s.begin()`` performs on a clean exit. That is precisely the regression the
review flagged: the MCP ``update_trigger`` tool must not persist a merged
``config_json`` it has just rejected.

This module runs the tool against a real Postgres session (testcontainers) and
asserts that a rejected update leaves the stored ``config_json`` byte-identical
— the persist-before-validate guarantee.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.api import mcp_server as ms
from modulo.db.models.trigger import Trigger
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


async def _seed_webhook_trigger(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    user_id: uuid.UUID,
    config_json: dict,
) -> uuid.UUID:
    trigger_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO triggers (id, organisation_id, pipeline_id, "
                "trigger_type, active, max_concurrent_runs, config_json, account_id) "
                "VALUES (:id, :oid, :pid, 'webhook', true, 5, (:config)::json, :uid)",
            ),
            {
                "id": str(trigger_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "config": json.dumps(config_json),
                "uid": str(user_id),
            },
        )
    return trigger_id


async def _read_config(db_engine: AsyncEngine, trigger_id: uuid.UUID) -> dict:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        trigger = (await session.execute(select(Trigger).where(Trigger.id == trigger_id))).scalar_one()
        return json.loads(json.dumps(trigger.config_json))


async def test_rejected_mcp_update_does_not_persist_merged_config(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A config_json update carrying an unread key returns validation AND must
    not write the merged (invalid) config to the database."""
    stored_config = {"input_template": {"greeting": "hi"}}
    trigger_id = await _seed_webhook_trigger(db_engine, test_org, test_pipeline, test_user, stored_config)

    factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)

    @asynccontextmanager
    async def real_session(org_id: uuid.UUID):
        # Mirrors mcp_server._session: a clean exit of ``s.begin()`` commits.
        async with factory() as s, s.begin():
            await set_rls_org(s, org_id)
            yield s

    tokens = (
        ms._ctx_org_id.set(test_org),
        ms._ctx_user_id.set(test_user),
        ms._ctx_role.set("admin"),
    )
    try:
        with (
            patch.object(ms, "_session", real_session),
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_check_agent_tool_scope"),
        ):
            result = await ms.update_trigger(trigger_id=str(trigger_id), config_json={"events": ["a"]})
    finally:
        ms._ctx_org_id.reset(tokens[0])
        ms._ctx_user_id.reset(tokens[1])
        ms._ctx_role.reset(tokens[2])

    assert result["error"] == "validation"
    assert "events" in result["detail"]
    assert "merged config_json" in result["detail"]

    assert await _read_config(db_engine, trigger_id) == stored_config
