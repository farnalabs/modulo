"""Unit tests for cross-team binding enforcement (PRD §9.3).

Covers both resource types that the pipeline-save command layer gates on:
team-private connector instances and team-private model backends. The two
halves share the ``_find_team_scope_mismatches`` fetch-and-filter pattern, so
each half is exercised end-to-end (pure rule -> detail builder -> async DB
fetch) to prove the shared abstraction is not only tested through the
connector half.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.team_visibility import (
    CONNECTOR_TEAM_MISMATCH,
    MODEL_BACKEND_TEAM_MISMATCH,
    ConnectorTeamMismatch,
    ModelBackendTeamMismatch,
    connector_team_mismatch,
    connector_team_mismatch_detail,
    extract_connector_bindings,
    find_connector_team_mismatches,
    find_model_backend_team_mismatches,
    model_backend_team_mismatch,
    model_backend_team_mismatch_detail,
)
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.model_backend import ModelBackend

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_NODE_ID = str(uuid.uuid4())


def _connector(*, visibility: str, owner_team_id: uuid.UUID | None, name: str = "c") -> ConnectorInstance:
    return ConnectorInstance(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        name=name,
        owner_team_id=owner_team_id,
        visibility=visibility,
    )


def _mock_session(rows: list[object]) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    session.execute = AsyncMock(return_value=result)
    return session


def _model_backend(*, visibility: str, owner_team_id: uuid.UUID | None, name: str = "mb") -> ModelBackend:
    return ModelBackend(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        name=name,
        display_name=name,
        provider="openai",
        model_id="gpt-4o",
        credentials_ciphertext=b"x",
        owner_team_id=owner_team_id,
        visibility=visibility,
    )


# ---------------------------------------------------------------------------
# connector_team_mismatch (pure rule)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("visibility", "connector_team", "pipeline_team", "expected"),
    [
        ("team", _TEAM_A, _TEAM_B, True),
        ("team", _TEAM_A, _TEAM_A, False),
        ("team", _TEAM_A, None, True),
        ("team", None, _TEAM_A, True),
        ("org", _TEAM_A, _TEAM_B, False),
        ("org", None, None, False),
        (None, _TEAM_A, _TEAM_B, False),
    ],
)
def test_connector_team_mismatch_rule(
    visibility: str | None,
    connector_team: uuid.UUID | None,
    pipeline_team: uuid.UUID | None,
    expected: bool,
) -> None:
    assert connector_team_mismatch(visibility, connector_team, pipeline_team) is expected


# ---------------------------------------------------------------------------
# connector_team_mismatch_detail
# ---------------------------------------------------------------------------


def test_detail_contains_named_error() -> None:
    mismatch = ConnectorTeamMismatch(
        connector_id=uuid.uuid4(),
        connector_name="eng-db",
        connector_owner_team_id=_TEAM_A,
        pipeline_owner_team_id=_TEAM_B,
        node_id=_NODE_ID,
    )
    detail = connector_team_mismatch_detail([mismatch])
    assert detail.startswith(CONNECTOR_TEAM_MISMATCH)
    assert "eng-db" in detail
    assert str(_TEAM_A) in detail
    assert str(_TEAM_B) in detail


def test_detail_joins_multiple_mismatches() -> None:
    m1 = ConnectorTeamMismatch(
        connector_id=uuid.uuid4(),
        connector_name="db-a",
        connector_owner_team_id=_TEAM_A,
        pipeline_owner_team_id=_TEAM_B,
        node_id=_NODE_ID,
    )
    m2 = ConnectorTeamMismatch(
        connector_id=uuid.uuid4(),
        connector_name="db-b",
        connector_owner_team_id=_TEAM_B,
        pipeline_owner_team_id=_TEAM_A,
        node_id="node-2",
    )
    detail = connector_team_mismatch_detail([m1, m2])
    assert detail.startswith(CONNECTOR_TEAM_MISMATCH)
    assert "db-a" in detail
    assert "db-b" in detail
    assert str(_TEAM_A) in detail
    assert str(_TEAM_B) in detail
    assert detail.count("is team-private") == 2
    assert "; " in detail


# ---------------------------------------------------------------------------
# extract_connector_bindings (graph node → snapshot binding descriptors)
# ---------------------------------------------------------------------------


def test_extract_connector_bindings_valid_node() -> None:
    node_id = str(uuid.uuid4())
    instance_id = str(uuid.uuid4())
    nodes = [{"id": node_id, "connector_binding": {"instance_id": instance_id}}]
    assert extract_connector_bindings(nodes) == [{"node_id": node_id, "connector_instance_id": instance_id}]


def test_extract_connector_bindings_skips_nodes_without_binding() -> None:
    node_id = str(uuid.uuid4())
    assert not extract_connector_bindings([{"id": node_id}])


def test_extract_connector_bindings_skips_non_dict_bindings() -> None:
    node_id = str(uuid.uuid4())
    for bad in ("instance_id", 42, None, ["instance_id"]):
        nodes = [{"id": node_id, "connector_binding": bad}]
        assert not extract_connector_bindings(nodes)


def test_extract_connector_bindings_skips_missing_instance_id() -> None:
    node_id = str(uuid.uuid4())
    nodes = [
        {"id": node_id, "connector_binding": {}},
        {"id": node_id, "connector_binding": {"instance_id": None}},
    ]
    assert not extract_connector_bindings(nodes)


def test_extract_connector_bindings_missing_node_id_stringifies_none() -> None:
    instance_id = str(uuid.uuid4())
    nodes = [{"connector_binding": {"instance_id": instance_id}}]
    assert extract_connector_bindings(nodes) == [{"node_id": "None", "connector_instance_id": instance_id}]


# ---------------------------------------------------------------------------
# find_connector_team_mismatches (async DB check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_bindings_return_no_mismatches() -> None:
    session = _mock_session([])
    assert not await find_connector_team_mismatches(session, _ORG_ID, _TEAM_A, [])
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_team_connector_is_allowed() -> None:
    conn = _connector(visibility="team", owner_team_id=_TEAM_A, name="eng-db")
    bindings = [{"node_id": _NODE_ID, "connector_instance_id": str(conn.id)}]
    session = _mock_session([conn])
    assert not await find_connector_team_mismatches(session, _ORG_ID, _TEAM_A, bindings)


@pytest.mark.asyncio
async def test_org_connector_is_allowed_across_teams() -> None:
    conn = _connector(visibility="org", owner_team_id=None, name="shared")
    bindings = [{"node_id": _NODE_ID, "connector_instance_id": str(conn.id)}]
    session = _mock_session([conn])
    assert not await find_connector_team_mismatches(session, _ORG_ID, _TEAM_B, bindings)


@pytest.mark.asyncio
async def test_cross_team_connector_returns_mismatch() -> None:
    conn = _connector(visibility="team", owner_team_id=_TEAM_A, name="eng-db")
    bindings = [{"node_id": _NODE_ID, "connector_instance_id": str(conn.id)}]
    session = _mock_session([conn])
    mismatches = await find_connector_team_mismatches(session, _ORG_ID, _TEAM_B, bindings)
    assert len(mismatches) == 1
    assert mismatches[0].connector_id == conn.id
    assert mismatches[0].connector_owner_team_id == _TEAM_A
    assert mismatches[0].pipeline_owner_team_id == _TEAM_B
    assert mismatches[0].node_id == _NODE_ID


@pytest.mark.asyncio
async def test_team_connector_on_org_pipeline_returns_mismatch() -> None:
    conn = _connector(visibility="team", owner_team_id=_TEAM_A, name="eng-db")
    bindings = [{"node_id": _NODE_ID, "connector_instance_id": str(conn.id)}]
    session = _mock_session([conn])
    mismatches = await find_connector_team_mismatches(session, _ORG_ID, None, bindings)
    assert len(mismatches) == 1
    assert mismatches[0].connector_id == conn.id
    assert mismatches[0].connector_name == "eng-db"
    assert mismatches[0].connector_owner_team_id == _TEAM_A
    assert mismatches[0].pipeline_owner_team_id is None
    assert mismatches[0].node_id == _NODE_ID


@pytest.mark.asyncio
async def test_binding_without_node_id_reports_none_node() -> None:
    conn = _connector(visibility="team", owner_team_id=_TEAM_A, name="eng-db")
    bindings = [{"connector_instance_id": str(conn.id)}]
    session = _mock_session([conn])
    mismatches = await find_connector_team_mismatches(session, _ORG_ID, _TEAM_B, bindings)
    assert len(mismatches) == 1
    assert mismatches[0].node_id is None


@pytest.mark.asyncio
async def test_mixed_valid_and_invalid_instance_ids() -> None:
    conn = _connector(visibility="team", owner_team_id=_TEAM_A, name="eng-db")
    bindings: list[dict[str, str | None]] = [
        {"node_id": _NODE_ID, "connector_instance_id": str(conn.id)},
        {"node_id": _NODE_ID, "connector_instance_id": "not-a-uuid"},
        {"node_id": _NODE_ID, "connector_instance_id": None},
    ]
    session = _mock_session([conn])
    mismatches = await find_connector_team_mismatches(session, _ORG_ID, _TEAM_B, bindings)
    assert len(mismatches) == 1
    assert mismatches[0].connector_id == conn.id


@pytest.mark.asyncio
async def test_missing_connector_is_ignored() -> None:
    session = _mock_session([])
    bindings = [{"node_id": _NODE_ID, "connector_instance_id": str(uuid.uuid4())}]
    assert not await find_connector_team_mismatches(session, _ORG_ID, _TEAM_B, bindings)


@pytest.mark.asyncio
async def test_connector_from_other_org_is_ignored() -> None:
    conn = _connector(visibility="team", owner_team_id=_TEAM_A, name="other-org")
    conn.organisation_id = uuid.uuid4()
    bindings = [{"node_id": _NODE_ID, "connector_instance_id": str(conn.id)}]
    session = _mock_session([])
    assert not await find_connector_team_mismatches(session, _ORG_ID, _TEAM_B, bindings)


@pytest.mark.asyncio
async def test_invalid_binding_ids_are_ignored() -> None:
    session = _mock_session([])
    bindings = [{"node_id": _NODE_ID, "connector_instance_id": "not-a-uuid"}]
    assert not await find_connector_team_mismatches(session, _ORG_ID, _TEAM_B, bindings)
    session.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# model_backend_team_mismatch (pure rule, PRD §9.3 mirror of the connector rule)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("visibility", "backend_team", "pipeline_team", "expected"),
    [
        ("team", _TEAM_A, _TEAM_B, True),
        ("team", _TEAM_A, _TEAM_A, False),
        ("team", _TEAM_A, None, True),
        ("team", None, _TEAM_A, True),
        ("org", _TEAM_A, _TEAM_B, False),
        ("org", None, None, False),
        (None, _TEAM_A, _TEAM_B, False),
    ],
)
def test_model_backend_team_mismatch_rule(
    visibility: str | None,
    backend_team: uuid.UUID | None,
    pipeline_team: uuid.UUID | None,
    expected: bool,
) -> None:
    assert model_backend_team_mismatch(visibility, backend_team, pipeline_team) is expected


# ---------------------------------------------------------------------------
# model_backend_team_mismatch_detail
# ---------------------------------------------------------------------------


def test_model_backend_detail_contains_named_error() -> None:
    mismatch = ModelBackendTeamMismatch(
        model_backend_id=uuid.uuid4(),
        model_backend_name="productive",
        model_backend_owner_team_id=_TEAM_A,
        pipeline_owner_team_id=_TEAM_B,
        node_id=_NODE_ID,
    )
    detail = model_backend_team_mismatch_detail([mismatch])
    assert detail.startswith(MODEL_BACKEND_TEAM_MISMATCH)
    assert "productive" in detail
    assert str(_TEAM_A) in detail
    assert str(_TEAM_B) in detail


def test_model_backend_detail_joins_multiple_mismatches() -> None:
    m1 = ModelBackendTeamMismatch(
        model_backend_id=uuid.uuid4(),
        model_backend_name="mb-a",
        model_backend_owner_team_id=_TEAM_A,
        pipeline_owner_team_id=_TEAM_B,
        node_id=_NODE_ID,
    )
    m2 = ModelBackendTeamMismatch(
        model_backend_id=uuid.uuid4(),
        model_backend_name="mb-b",
        model_backend_owner_team_id=_TEAM_B,
        pipeline_owner_team_id=_TEAM_A,
        node_id="node-2",
    )
    detail = model_backend_team_mismatch_detail([m1, m2])
    assert detail.startswith(MODEL_BACKEND_TEAM_MISMATCH)
    assert "mb-a" in detail
    assert "mb-b" in detail
    assert str(_TEAM_A) in detail
    assert str(_TEAM_B) in detail
    assert detail.count("is team-private") == 2
    assert "; " in detail


def test_model_backend_detail_empty_mismatches() -> None:
    detail = model_backend_team_mismatch_detail([])
    assert detail.startswith(MODEL_BACKEND_TEAM_MISMATCH)
    assert "is team-private" not in detail


# ---------------------------------------------------------------------------
# find_model_backend_team_mismatches (async DB check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_pins_return_no_mismatches() -> None:
    session = _mock_session([])
    assert not await find_model_backend_team_mismatches(session, _ORG_ID, _TEAM_A, [])
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_team_backend_is_allowed() -> None:
    mb = _model_backend(visibility="team", owner_team_id=_TEAM_A, name="prod")
    pins = [{"node_id": _NODE_ID, "model_backend_id": str(mb.id)}]
    session = _mock_session([mb])
    assert not await find_model_backend_team_mismatches(session, _ORG_ID, _TEAM_A, pins)


@pytest.mark.asyncio
async def test_org_backend_is_allowed_across_teams() -> None:
    mb = _model_backend(visibility="org", owner_team_id=None, name="shared")
    pins = [{"node_id": _NODE_ID, "model_backend_id": str(mb.id)}]
    session = _mock_session([mb])
    assert not await find_model_backend_team_mismatches(session, _ORG_ID, _TEAM_B, pins)


@pytest.mark.asyncio
async def test_cross_team_backend_returns_mismatch() -> None:
    mb = _model_backend(visibility="team", owner_team_id=_TEAM_A, name="prod")
    pins = [{"node_id": _NODE_ID, "model_backend_id": str(mb.id)}]
    session = _mock_session([mb])
    mismatches = await find_model_backend_team_mismatches(session, _ORG_ID, _TEAM_B, pins)
    assert len(mismatches) == 1
    assert mismatches[0].model_backend_id == mb.id
    assert mismatches[0].model_backend_name == "prod"
    assert mismatches[0].model_backend_owner_team_id == _TEAM_A
    assert mismatches[0].pipeline_owner_team_id == _TEAM_B
    assert mismatches[0].node_id == _NODE_ID


@pytest.mark.asyncio
async def test_team_backend_on_org_pipeline_returns_mismatch() -> None:
    mb = _model_backend(visibility="team", owner_team_id=_TEAM_A, name="prod")
    pins = [{"node_id": _NODE_ID, "model_backend_id": str(mb.id)}]
    session = _mock_session([mb])
    mismatches = await find_model_backend_team_mismatches(session, _ORG_ID, None, pins)
    assert len(mismatches) == 1
    assert mismatches[0].pipeline_owner_team_id is None
    assert mismatches[0].node_id == _NODE_ID


@pytest.mark.asyncio
async def test_pin_without_node_id_reports_none_node() -> None:
    mb = _model_backend(visibility="team", owner_team_id=_TEAM_A, name="prod")
    pins = [{"model_backend_id": str(mb.id)}]
    session = _mock_session([mb])
    mismatches = await find_model_backend_team_mismatches(session, _ORG_ID, _TEAM_B, pins)
    assert len(mismatches) == 1
    assert mismatches[0].node_id is None


@pytest.mark.asyncio
async def test_mixed_valid_and_invalid_model_backend_ids() -> None:
    mb = _model_backend(visibility="team", owner_team_id=_TEAM_A, name="prod")
    pins: list[dict[str, str | None]] = [
        {"node_id": _NODE_ID, "model_backend_id": str(mb.id)},
        {"node_id": _NODE_ID, "model_backend_id": "not-a-uuid"},
        {"node_id": _NODE_ID, "model_backend_id": None},
    ]
    session = _mock_session([mb])
    mismatches = await find_model_backend_team_mismatches(session, _ORG_ID, _TEAM_B, pins)
    assert len(mismatches) == 1
    assert mismatches[0].model_backend_id == mb.id


@pytest.mark.asyncio
async def test_missing_model_backend_is_ignored() -> None:
    session = _mock_session([])
    pins = [{"node_id": _NODE_ID, "model_backend_id": str(uuid.uuid4())}]
    assert not await find_model_backend_team_mismatches(session, _ORG_ID, _TEAM_B, pins)


@pytest.mark.asyncio
async def test_model_backend_from_other_org_is_ignored() -> None:
    mb = _model_backend(visibility="team", owner_team_id=_TEAM_A, name="other-org")
    mb.organisation_id = uuid.uuid4()
    pins = [{"node_id": _NODE_ID, "model_backend_id": str(mb.id)}]
    session = _mock_session([])
    assert not await find_model_backend_team_mismatches(session, _ORG_ID, _TEAM_B, pins)


@pytest.mark.asyncio
async def test_invalid_model_backend_pins_are_ignored() -> None:
    session = _mock_session([])
    pins = [{"node_id": _NODE_ID, "model_backend_id": "not-a-uuid"}]
    assert not await find_model_backend_team_mismatches(session, _ORG_ID, _TEAM_B, pins)
    session.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# extract_connector_bindings -- non-string node id edge
# ---------------------------------------------------------------------------


def test_extract_connector_bindings_non_str_node_id() -> None:
    instance_id = str(uuid.uuid4())
    nodes = [{"id": 42, "connector_binding": {"instance_id": instance_id}}]
    assert extract_connector_bindings(nodes) == [{"node_id": "42", "connector_instance_id": instance_id}]
