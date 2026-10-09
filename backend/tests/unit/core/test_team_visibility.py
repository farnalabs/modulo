"""Unit tests for cross-team binding enforcement (PRD §9.3, FAR-1515, FAR-1618).

Covers both resource types that the pipeline-save command layer gates on:
team-private connector instances and team-private model backends. The two
halves share the ``_find_team_scope_mismatches`` fetch-and-filter pattern, so
each half is exercised end-to-end (pure rule -> detail builder -> async DB
fetch) to prove the shared abstraction is not only tested through the
connector half.

The rule has ONE direction: a team-PRIVATE row is only usable by its owner
team's pipeline (a different team, or an org pipeline with no owner team, is
a mismatch), while an ORG-visibility row is shared across the organisation
and never mismatches (FAR-1618 removed the reverse direction FAR-1515 had
briefly added — a team pipeline pinning an org connector — together with the
FAR-516 run-gate it mirrored). The connector and model-backend predicates now
say exactly the same thing, so both direction matrices are asserted here.
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
        # Team-private connector: only its own team's pipeline may bind it.
        ("team", _TEAM_A, _TEAM_B, True),
        ("team", _TEAM_A, _TEAM_A, False),
        ("team", _TEAM_A, None, True),
        ("team", None, _TEAM_A, True),
        # Org pipeline (owner_team_id=None) + org connector: saves, unchanged.
        ("org", None, None, False),
        ("org", _TEAM_A, None, False),
        (None, None, None, False),
        # FAR-1618: an ORG-visibility connector is shared across the org, so a
        # TEAM pipeline binding it is NOT a mismatch (reverts FAR-1515's
        # reverse direction and the FAR-516 run-gate it mirrored).
        ("org", None, _TEAM_A, False),
        ("org", _TEAM_A, _TEAM_B, False),
        (None, _TEAM_A, _TEAM_B, False),
        (None, None, _TEAM_A, False),
    ],
    ids=[
        "team_conn_other_team",
        "team_conn_own_team",
        "team_conn_org_pipeline",
        "team_conn_no_owner",
        "org_conn_org_pipeline",
        "org_conn_owned_by_no_team",
        "default_vis_org_pipeline",
        "org_conn_team_pipeline",
        "org_conn_owned_by_other_team",
        "default_vis_other_team",
        "default_vis_team_pipeline",
    ],
)
def test_connector_team_mismatch_rule(
    visibility: str | None,
    connector_team: uuid.UUID | None,
    pipeline_team: uuid.UUID | None,
    expected: bool,
) -> None:
    assert connector_team_mismatch(visibility, connector_team, pipeline_team) is expected


def test_team_pipeline_accepts_an_org_visibility_connector() -> None:
    """FAR-1618: the exact gap this reverses — a team pipeline may pin an org connector.

    An org-visibility connector is shared across the organisation (teams are a
    visibility grouping, not a credential trust boundary), so binding it to a
    team pipeline is NOT a mismatch — neither at save time nor at run time,
    the FAR-516 run-gate having been removed with this rule. Before FAR-1618
    both assertions below returned True (the FAR-1515 reverse direction).
    """
    assert connector_team_mismatch("org", None, _TEAM_A) is False
    assert connector_team_mismatch(None, None, _TEAM_A) is False
    # ...and the same is true whatever team owns the org-visibility connector.
    assert connector_team_mismatch("org", _TEAM_B, _TEAM_A) is False


def test_org_pipeline_still_accepts_an_org_connector() -> None:
    """Regression guard: the org-pipeline rule must NOT change (FAR-1515)."""
    assert connector_team_mismatch("org", None, None) is False
    assert connector_team_mismatch(None, None, None) is False
    assert connector_team_mismatch("org", _TEAM_A, None) is False


# ---------------------------------------------------------------------------
# connector_team_mismatch_detail
# ---------------------------------------------------------------------------


def test_detail_contains_named_error() -> None:
    mismatch = ConnectorTeamMismatch(
        connector_id=uuid.uuid4(),
        connector_name="eng-db",
        connector_owner_team_id=_TEAM_A,
        pipeline_owner_team_id=_TEAM_B,
        connector_visibility="team",
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
        connector_visibility="team",
        node_id=_NODE_ID,
    )
    m2 = ConnectorTeamMismatch(
        connector_id=uuid.uuid4(),
        connector_name="db-b",
        connector_owner_team_id=_TEAM_B,
        pipeline_owner_team_id=_TEAM_A,
        connector_visibility="team",
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


def test_detail_builder_has_no_org_only_branch() -> None:
    """FAR-1618: the org-only message branch was removed with the rule it served.

    An org-visibility connector never mismatches, so the gate can no longer
    build a ``ConnectorTeamMismatch`` for one — and the detail builder no
    longer renders the removed "is org-only / flip the connector to `team`"
    text. This pins the removal from both ends: re-introducing the rule
    fails the predicate tests above, re-introducing just the dead message
    branch fails here.
    """
    mismatch = ConnectorTeamMismatch(
        connector_id=uuid.uuid4(),
        connector_name="shared-ci",
        connector_owner_team_id=None,
        pipeline_owner_team_id=_TEAM_A,
        connector_visibility="org",
        node_id=_NODE_ID,
    )
    detail = connector_team_mismatch_detail([mismatch])
    assert detail.startswith(CONNECTOR_TEAM_MISMATCH)
    assert "shared-ci" in detail
    assert "is org-only" not in detail
    assert "flip the connector to `team`" not in detail
    assert "duplicate it" not in detail


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
async def test_org_connector_is_allowed_on_an_org_pipeline() -> None:
    """FAR-1515: the org-pipeline rule is unchanged - org connector still saves."""
    conn = _connector(visibility="org", owner_team_id=None, name="shared")
    bindings = [{"node_id": _NODE_ID, "connector_instance_id": str(conn.id)}]
    session = _mock_session([conn])
    assert not await find_connector_team_mismatches(session, _ORG_ID, None, bindings)


@pytest.mark.asyncio
async def test_org_connector_on_a_team_pipeline_is_allowed() -> None:
    """FAR-1618: team pipeline + org-visibility connector is NOT a mismatch.

    Reverses FAR-1515's reverse direction: an org-visibility connector is
    shared across the organisation, so the save gate finds nothing to refuse.
    Before FAR-1618 this returned exactly one mismatch (and its 409 detail
    named the removed "flip the connector to `team`" fix).
    """
    conn = _connector(visibility="org", owner_team_id=None, name="shared")
    bindings = [{"node_id": _NODE_ID, "connector_instance_id": str(conn.id)}]
    session = _mock_session([conn])
    mismatches = await find_connector_team_mismatches(session, _ORG_ID, _TEAM_A, bindings)
    assert not mismatches


@pytest.mark.asyncio
async def test_org_connector_owned_by_another_team_is_allowed() -> None:
    """FAR-1618: ownership never makes an org-visibility connector team-private."""
    conn = _connector(visibility="org", owner_team_id=_TEAM_B, name="shared")
    bindings = [{"node_id": _NODE_ID, "connector_instance_id": str(conn.id)}]
    session = _mock_session([conn])
    mismatches = await find_connector_team_mismatches(session, _ORG_ID, _TEAM_A, bindings)
    assert not mismatches


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
async def test_missing_connector_raises_the_named_refusal() -> None:
    """FAR-1515 CRITICAL 1: absent from the team-blind org read = fail closed.

    This used to return no mismatches (the id was silently skipped), which is
    exactly how a team-private row HIDDEN by ``rls_team_isolation`` slipped
    through the gate. The candidate read is now team-blind, so an absent row
    can only mean the organisation has no such connector - and a binding the
    gate cannot validate must be refused, never waved through. The refusal
    carries the same machine-readable ``connector_team_mismatch`` prefix so
    every surface maps it to the same named 409.
    """
    from modulo.core.team_visibility import ConnectorBindingMissingError

    session = _mock_session([])
    missing_id = uuid.uuid4()
    bindings = [{"node_id": _NODE_ID, "connector_instance_id": str(missing_id)}]
    with pytest.raises(ConnectorBindingMissingError) as excinfo:
        await find_connector_team_mismatches(session, _ORG_ID, _TEAM_B, bindings)
    assert excinfo.value.missing == [(missing_id, _NODE_ID)]
    detail = str(excinfo.value)
    assert detail.startswith(CONNECTOR_TEAM_MISMATCH)
    assert str(missing_id) in detail
    assert "does not resolve to a connector in this organisation" in detail


@pytest.mark.asyncio
async def test_connector_from_other_org_raises_the_named_refusal() -> None:
    """Another org's connector is absent from the org-scoped read -> refused.

    Same fail-closed rule as the missing-id case: the gate only ever judges
    rows it can see, so a binding it cannot see cannot be judged - and an
    unjudged binding must not save.
    """
    from modulo.core.team_visibility import ConnectorBindingMissingError

    conn = _connector(visibility="team", owner_team_id=_TEAM_A, name="other-org")
    conn.organisation_id = uuid.uuid4()
    bindings = [{"node_id": _NODE_ID, "connector_instance_id": str(conn.id)}]
    session = _mock_session([])
    with pytest.raises(ConnectorBindingMissingError) as excinfo:
        await find_connector_team_mismatches(session, _ORG_ID, _TEAM_B, bindings)
    assert excinfo.value.missing == [(conn.id, _NODE_ID)]


@pytest.mark.asyncio
async def test_a_found_mismatch_takes_precedence_over_a_missing_id() -> None:
    """When BOTH a cross-team violation and an unresolvable id are present the
    real mismatch is returned (the caller refuses with its actionable detail);
    the missing id only raises when it is the SOLE refusal."""
    from modulo.core.team_visibility import ConnectorBindingMissingError

    conn = _connector(visibility="team", owner_team_id=_TEAM_A, name="eng-db")
    bindings = [
        {"node_id": _NODE_ID, "connector_instance_id": str(conn.id)},
        {"node_id": _NODE_ID, "connector_instance_id": str(uuid.uuid4())},
    ]
    session = _mock_session([conn])
    mismatches = await find_connector_team_mismatches(session, _ORG_ID, _TEAM_B, bindings)
    assert [m.connector_name for m in mismatches] == ["eng-db"]
    # ...and the sole-refusal case still raises rather than returning [].
    session = _mock_session([])
    with pytest.raises(ConnectorBindingMissingError):
        await find_connector_team_mismatches(session, _ORG_ID, _TEAM_B, bindings)


@pytest.mark.asyncio
async def test_invalid_binding_ids_are_ignored() -> None:
    session = _mock_session([])
    bindings = [{"node_id": _NODE_ID, "connector_instance_id": "not-a-uuid"}]
    assert not await find_connector_team_mismatches(session, _ORG_ID, _TEAM_B, bindings)
    session.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# model_backend_team_mismatch (pure rule, PRD §9.3 mirror of the connector rule)
#
# The two predicates now say the same thing (FAR-1618 restated the parity
# note): an org-visibility row never mismatches, a team-private row
# mismatches every pipeline outside its owner team. ModelBackendHub resolves
# a pin with hub.get(backend_id) and never consults visibility — and after
# FAR-1618 neither does the connector hub — so neither rule has an
# invocation-time counterpart to mirror. The rows below pin that the
# model-backend direction matrix is UNCHANGED by this ticket.
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
