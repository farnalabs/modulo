"""Unit tests for the FAR-759 install-time provenance snapshot helpers.

Covers ``compute_entity_fingerprint``, ``snapshot_entity_fingerprints`` and
``_map_pin_entity_ids`` in ``modulo.core.library_service.install`` — the
snapshot the collection upgrade flow consumes.  Unit tier: no DB.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modulo.core.library_service.install import (
    _map_pin_entity_ids,
    agent_entity_fingerprint,
    compute_entity_fingerprint,
    schema_entity_fingerprint,
    snapshot_entity_fingerprints,
)


def _pin(slug: str, primitive_type: str, row_id: str | None = None, content: dict | None = None):
    return SimpleNamespace(
        id=row_id or str(uuid.uuid4()),
        slug=slug,
        primitive_type=primitive_type,
        content_json=content if content is not None else {},
    )


class TestComputeEntityFingerprint:
    async def test_schema_success(self) -> None:
        org = uuid.uuid4()
        eid = uuid.uuid4()
        schema = SimpleNamespace(organisation_id=org, description="d", abstract_name="a")
        version_row = SimpleNamespace(definition_json={"type": "object"})
        session = AsyncMock()
        session.get = AsyncMock(return_value=schema)
        session.scalar = AsyncMock(return_value=version_row)
        fp = await compute_entity_fingerprint(session, org, "schema", eid)
        assert fp == schema_entity_fingerprint("d", "a", {"type": "object"})

    async def test_schema_no_version_row(self) -> None:
        org = uuid.uuid4()
        schema = SimpleNamespace(organisation_id=org, description=None, abstract_name=None)
        session = AsyncMock()
        session.get = AsyncMock(return_value=schema)
        session.scalar = AsyncMock(return_value=None)
        fp = await compute_entity_fingerprint(session, org, "schema", uuid.uuid4())
        assert fp == schema_entity_fingerprint(None, None, None)

    async def test_schema_missing(self) -> None:
        session = AsyncMock()
        session.get = AsyncMock(return_value=None)
        assert await compute_entity_fingerprint(session, uuid.uuid4(), "schema", uuid.uuid4()) is None

    async def test_schema_wrong_org(self) -> None:
        session = AsyncMock()
        session.get = AsyncMock(return_value=SimpleNamespace(organisation_id=uuid.uuid4()))
        assert await compute_entity_fingerprint(session, uuid.uuid4(), "schema", uuid.uuid4()) is None

    async def test_agent_success(self) -> None:
        org = uuid.uuid4()
        agent = SimpleNamespace(
            organisation_id=org,
            name="A",
            description=None,
            prompt_template="p",
            connector_type_refs=[],
            evals=None,
            retry_policy={},
            token_budget=None,
            input_schema_id=None,
            output_schema_id=None,
            input_schema_version=None,
            output_schema_version=None,
        )
        session = AsyncMock()
        session.get = AsyncMock(return_value=agent)
        fp = await compute_entity_fingerprint(session, org, "agent", uuid.uuid4())
        assert fp == agent_entity_fingerprint(agent)

    async def test_agent_missing(self) -> None:
        session = AsyncMock()
        session.get = AsyncMock(return_value=None)
        assert await compute_entity_fingerprint(session, uuid.uuid4(), "agent", uuid.uuid4()) is None

    async def test_agent_wrong_org(self) -> None:
        session = AsyncMock()
        session.get = AsyncMock(return_value=SimpleNamespace(organisation_id=uuid.uuid4()))
        assert await compute_entity_fingerprint(session, uuid.uuid4(), "agent", uuid.uuid4()) is None

    async def test_unknown_type(self) -> None:
        assert await compute_entity_fingerprint(AsyncMock(), uuid.uuid4(), "pipeline", uuid.uuid4()) is None


class TestSnapshotEntityFingerprints:
    async def test_skips_pipeline_and_none_fingerprints(self) -> None:
        org = uuid.uuid4()
        schema_id = str(uuid.uuid4())
        agent_id = str(uuid.uuid4())
        pipeline_id = str(uuid.uuid4())
        entities = [
            {"entity_type": "schema", "entity_id": schema_id},
            {"entity_type": "pipeline", "entity_id": pipeline_id},
            {"entity_type": "agent", "entity_id": agent_id},
        ]

        async def _fp(_s, _o, etype, _eid):
            return "fp" if etype == "schema" else None

        with patch("modulo.core.library_service.install.compute_entity_fingerprint", side_effect=_fp):
            out = await snapshot_entity_fingerprints(AsyncMock(), org, entities)

        assert out == {schema_id: "fp"}

    async def test_all_fingerprinted(self) -> None:
        org = uuid.uuid4()
        schema_id = str(uuid.uuid4())
        agent_id = str(uuid.uuid4())
        entities = [
            {"entity_type": "schema", "entity_id": schema_id},
            {"entity_type": "agent", "entity_id": agent_id},
        ]
        with patch(
            "modulo.core.library_service.install.compute_entity_fingerprint",
            new_callable=AsyncMock,
            return_value="fp",
        ):
            out = await snapshot_entity_fingerprints(AsyncMock(), org, entities)
        assert out == {schema_id: "fp", agent_id: "fp"}


class TestMapPinEntityIds:
    def test_standalone_schema_and_agent(self) -> None:
        s_pin = _pin("s1", "schema", row_id="s-prim")
        a_pin = _pin("a1", "agent", row_id="a-prim")
        result = {"schemas": {"s-prim": "local-s"}, "agents": {"a-prim": "local-a"}}
        out = _map_pin_entity_ids([s_pin, a_pin], result)
        assert out == {"s1": {"schema": ["local-s"]}, "a1": {"agent": ["local-a"]}}

    def test_workflow_consumes_pools_in_order(self) -> None:
        wf = _pin(
            "wf",
            "workflow",
            row_id="wf-prim",
            content={"bundle": {"agents": [{"x": 1}, {"x": 2}], "schemas": [{"y": 1}]}},
        )
        result = {
            "agents": {"wf-a1": "la1", "wf-a2": "la2"},
            "schemas": {"wf-s1": "ls1"},
        }
        out = _map_pin_entity_ids([wf], result)
        assert out == {"wf": {"agent": ["la1", "la2"], "schema": ["ls1"]}}

    def test_workflow_without_bundle_wrapper(self) -> None:
        wf = _pin("wf", "workflow", content={"agents": [{"x": 1}]})
        result = {"agents": {"wf-a": "la"}, "schemas": {}}
        out = _map_pin_entity_ids([wf], result)
        assert out == {"wf": {"agent": ["la"]}}

    def test_pipeline_template_agents(self) -> None:
        tmpl = _pin("t", "pipeline_template", content={"agents": [{"x": 1}, {"x": 2}]})
        result = {"agents": {"t-a1": "la1", "t-a2": "la2"}, "schemas": {}}
        out = _map_pin_entity_ids([tmpl], result)
        assert out == {"t": {"agent": ["la1", "la2"]}}

    def test_missing_local_id_skipped(self) -> None:
        s_pin = _pin("s1", "schema", row_id="s-prim")
        out = _map_pin_entity_ids([s_pin], {"schemas": {}, "agents": {}})
        assert out == {}

    def test_agent_missing_local_id_skipped(self) -> None:
        a_pin = _pin("a1", "agent", row_id="a-prim")
        out = _map_pin_entity_ids([a_pin], {"schemas": {}, "agents": {}})
        assert out == {}

    def test_unknown_primitive_type_ignored(self) -> None:
        prim = _pin("x", "library_collection", row_id="x-prim")
        out = _map_pin_entity_ids([prim], {"schemas": {"x-prim": "local"}, "agents": {}})
        assert out == {}

    def test_workflow_pool_shorter_than_embedded(self) -> None:
        wf = _pin("wf", "workflow", content={"bundle": {"agents": [{"x": 1}, {"x": 2}], "schemas": []}})
        result = {"agents": {"wf-a": "la"}, "schemas": {}}
        out = _map_pin_entity_ids([wf], result)
        assert out == {"wf": {"agent": ["la"]}}
