"""Unit tests for the collection_install_id stamping logic in library_service.install.

These cover the ``session.no_autoflush`` path added in the fix for PR #473:

    `backend/src/modulo/core/library_service/install.py`

The ``no_autoflush`` block defers the autoflush triggered by the
``session.get`` lookups until the ``CollectionInstall`` provenance row has been
added, so stamping ``collection_install_id`` before its FK target exists no
longer violates the ``fk_agents_collection_install_id`` constraint (migration
0223). The integration round-trip exercises this implicitly, but integration
suites are excluded from the SonarCloud coverage denominator, so we add a direct
unit test of ``_stamp_install_id`` to keep the new-code coverage gate green.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from modulo.core.library_service.install import _stamp_install_id


def _make_session(*, lookup) -> MagicMock:
    session = MagicMock(name="session")
    no_autoflush = MagicMock(name="no_autoflush")
    no_autoflush.__enter__ = MagicMock(return_value=None)
    no_autoflush.__exit__ = MagicMock(return_value=False)
    session.no_autoflush = no_autoflush
    session.get = AsyncMock(side_effect=lookup)
    return session


def _entity(org_id: uuid.UUID, *, matches: bool) -> MagicMock:
    entity = MagicMock(name="entity")
    entity.organisation_id = org_id if matches else uuid.uuid4()
    entity.collection_install_id = None
    return entity


async def test_stamps_matching_schemas_agents_and_pipeline():
    org_id = uuid.uuid4()
    install_id = uuid.uuid4()
    schema_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()

    result = {
        "schemas": {"s1": str(schema_id)},
        "agents": {"a1": str(agent_id)},
        "pipeline_id": str(pipeline_id),
    }

    entities = {
        str(schema_id): _entity(org_id, matches=True),
        str(agent_id): _entity(org_id, matches=True),
        str(pipeline_id): _entity(org_id, matches=True),
    }

    def _lookup(model, key):
        return entities[str(key)]

    session = _make_session(lookup=_lookup)

    stamped = await _stamp_install_id(session, org_id, install_id, result)

    # The no_autoflush context must be entered while stamping.
    session.no_autoflush.__enter__.assert_called_once()
    # One lookup per entity type.
    assert session.get.await_count == 3
    # Every matching entity is stamped with the install id.
    assert entities[str(schema_id)].collection_install_id == install_id
    assert entities[str(agent_id)].collection_install_id == install_id
    assert entities[str(pipeline_id)].collection_install_id == install_id
    # Tracking records are returned with the correct entity types.
    assert {e["entity_type"] for e in stamped} == {"schema", "agent", "pipeline"}


async def test_skips_entity_when_org_mismatch():
    org_id = uuid.uuid4()
    install_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    result = {"agents": {"a1": str(agent_id)}}

    entities = {str(agent_id): _entity(org_id, matches=False)}

    session = _make_session(lookup=lambda model, key: entities[str(key)])

    stamped = await _stamp_install_id(session, org_id, install_id, result)

    # Owned by a different org -> not stamped, not tracked.
    assert entities[str(agent_id)].collection_install_id is None
    assert stamped == []


async def test_skips_when_entity_not_found():
    org_id = uuid.uuid4()
    install_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    result = {"agents": {"a1": str(agent_id)}}

    session = _make_session(lookup=lambda model, key: None)

    stamped = await _stamp_install_id(session, org_id, install_id, result)

    assert stamped == []


async def test_no_pipeline_lookup_when_pipeline_id_absent():
    org_id = uuid.uuid4()
    install_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    result = {"agents": {"a1": str(agent_id)}}

    entities = {str(agent_id): _entity(org_id, matches=True)}

    session = _make_session(lookup=lambda model, key: entities[str(key)])

    stamped = await _stamp_install_id(session, org_id, install_id, result)

    assert entities[str(agent_id)].collection_install_id == install_id
    # No pipeline entry in the result -> only the agent lookup occurs.
    assert session.get.await_count == 1
    assert stamped == [{"entity_type": "agent", "entity_id": str(agent_id)}]
