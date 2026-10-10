"""Save-time graph enforcement on the node-conversion endpoints (FAR-1163 follow-up).

``_save_locked_graph`` is the single chokepoint both node-conversion endpoints
(``convert-to-agent`` / ``revert-to-manual``) persist through. Until now it only
called ``_save_graph`` - it skipped all three save-time gates the sibling
graph-write endpoints run (``replace_pipeline_graph_endpoint`` and
``_apply_graph_update`` / ``update_pipeline_endpoint``):

1. ``_enforce_connector_team_bindings`` - 409 ``connector_team_mismatch`` when a
   team-private connector is bound from outside its team,
2. ``_resolve_graph_references`` - the FAR-418 capability-scope widening guard
   plus unknown agent/schema ids and the model-backend team check (422/409),
3. ``_validate_graph_save`` - save-time graph validation, run AFTER the write
   inside the same transaction so a blocking code rolls the write back (422).

So a convert-to-agent request could persist a cross-team connector binding that
``PATCH /graph`` rejects with ``409 connector_team_mismatch``.

These tests drive ``_save_locked_graph`` directly with a session double that
dispatches on SQL text (so a statement-order change cannot silently swap
answers) and assert:

* a cross-team connector binding is rejected 409, named
  ``connector_team_mismatch``, and the rejection happens BEFORE the graph write
  (``_save_graph`` is never called),
* an ORG-visibility connector on a TEAM pipeline is ACCEPTED (FAR-1618 — org
  resources are shared across the organisation; this reverses the FAR-1515
  reverse direction and the FAR-516 run-gate it mirrored),
* an org connector on an ORG pipeline, or the pipeline's own team connector,
  is accepted,
* ``_resolve_graph_references`` runs before the write and its 422 stops the write,
* ``_validate_graph_save`` runs AFTER the write, receives the converted node's
  connector binding, and its blocking 422 propagates (rolling the txn back),
* the non-blocking (advisory) half of ``_validate_graph_save`` is RETURNED to
  the caller as the third element so the conversion endpoints can surface
  ``validation_issues`` like their siblings (FAR-1277),
* a stored node the current schema can no longer parse fails closed with a
  422 naming that node, rather than being reference-checked as unchecked.

The enforcement RULES themselves are covered elsewhere — the team-mismatch
predicates by ``tests/unit/core/test_team_visibility.py``, the capability-scope
predicate by ``tests/unit/api/test_pipelines_routes_coverage.py``; what is new
here is that the node-conversion save path CALLS them, and in the same order
the siblings use.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from modulo.auth.jwt import TenantPrincipal

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_NODE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_AGENT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_CONNECTOR_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_PIPELINE_TEAM = uuid.UUID("55555555-5555-5555-5555-555555555555")
#: The team that owns the connector in the cross-team case - deliberately NOT
#: ``_PIPELINE_TEAM``.
_OTHER_TEAM = uuid.UUID("66666666-6666-6666-6666-666666666666")
_PREFIX = "modulo.api.routes.pipelines."


# ---------------------------------------------------------------------------
# Session double
# ---------------------------------------------------------------------------


def _rows_result(rows: list[Any]) -> MagicMock:
    """A result whose ``.scalars().all()`` and ``.scalar_one_or_none()`` agree.

    ``_find_team_scope_mismatches`` reads ``.scalars().all()``; the endpoint's
    own connector lookup reads ``.scalar_one_or_none()``. Both must answer from
    the same row set, so the double serves one list and derives both views.
    """
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = rows
    result.scalars.return_value = scalars
    result.scalar_one_or_none.return_value = rows[0] if rows else None
    result.first.return_value = rows[0] if rows else None
    return result


def _connector_row(*, visibility: str, owner_team_id: uuid.UUID | None) -> MagicMock:
    row = MagicMock()
    row.id = _CONNECTOR_ID
    row.organisation_id = _ORG_ID
    row.name = "github-connector"
    row.connector_type_id = "github"
    row.visibility = visibility
    row.owner_team_id = owner_team_id
    return row


def _enforcement_session(*, connector_visibility: str, connector_owner_team: uuid.UUID | None) -> AsyncMock:
    """Session double answering the queries the conversion save path issues.

    Dispatched on SQL text rather than call position, so inserting a statement
    cannot silently swap an answer onto the wrong query.
    """
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.in_transaction = MagicMock(return_value=True)
    session.info = {}
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = MagicMock(return_value=bind)

    connector = _connector_row(visibility=connector_visibility, owner_team_id=connector_owner_team)

    async def _execute(stmt: object, *_args: Any, **_kwargs: Any) -> MagicMock:
        sql = str(stmt)
        if "connector_instances" in sql:
            return _rows_result([connector])
        # Everything else the save path may ask for (model backends, guardrail
        # rows, graph-validator probes) resolves to "no rows" - the passing
        # paths below must not depend on any of them existing.
        return _rows_result([])

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _gate_clearing_connector_session() -> AsyncMock:
    """Session double whose connector CLEARS the team gate on the team pipeline.

    ``_PIPELINE_TEAM`` owns both the pipeline (``_save``'s default) and this
    connector, so ``connector_team_mismatch`` is False and the save reaches
    the step under test — reference resolution, post-write validation, the
    advisory return, the edge mapping. Every test that needs to get PAST the
    connector gate uses this instead of hand-picking a visibility: a
    DIFFERENT team's connector (or one owned by nobody) is the 409 case, so
    that is what would stop the request before the step it exists to
    exercise. (Since FAR-1618 an org connector would also clear the gate, but
    the team-owned one is the conservative choice: it exercises the strictest
    passing path.)
    """
    return _enforcement_session(connector_visibility="team", connector_owner_team=_PIPELINE_TEAM)


# ---------------------------------------------------------------------------
# Graph + principal fixtures
# ---------------------------------------------------------------------------


def _converted_node() -> dict[str, Any]:
    """The stored manual node AFTER convert-to-agent mutates it."""
    return {
        "id": str(_NODE_ID),
        "node_type": "agent",
        "agent_id": str(_AGENT_ID),
        "connector_binding": {"type": "github", "instance_id": str(_CONNECTOR_ID)},
        "position": {"x": 0, "y": 0},
        "label": "qa",
    }


def _principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="enforcement-test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )


async def _save(
    session: AsyncMock,
    nodes: list[dict[str, Any]] | None = None,
    *,
    pipeline_owner_team: uuid.UUID | None = _PIPELINE_TEAM,
) -> tuple[list[dict[str, Any]], list[Any], list[Any]] | None:
    from modulo.api.routes.pipelines import _save_locked_graph

    return await _save_locked_graph(
        session,
        pipeline_id=_PIPELINE_ID,
        org_id=_ORG_ID,
        principal=_principal(),
        pipeline_owner_team_id=pipeline_owner_team,
        nodes=nodes if nodes is not None else [_converted_node()],
        edges=[],
    )


# ---------------------------------------------------------------------------
# 1. Cross-team connector binding: 409, before the write
# ---------------------------------------------------------------------------


async def test_cross_team_connector_binding_is_rejected_409() -> None:
    """A team-private connector owned by ANOTHER team must not be persisted.

    This is the exact request ``PATCH /graph`` rejects with
    ``409 connector_team_mismatch``; the node-conversion save path must apply
    the same gate. The named error in ``detail`` is what lets a client branch
    on it, so assert on it rather than only the status.
    """
    session = _enforcement_session(connector_visibility="team", connector_owner_team=_OTHER_TEAM)
    save = AsyncMock(return_value=([], []))

    with (
        patch(f"{_PREFIX}_save_graph", new=save),
        patch(f"{_PREFIX}_resolve_graph_references", new=AsyncMock(return_value=([], []))),
        patch(f"{_PREFIX}_validate_graph_save", new=AsyncMock(return_value=[])),
        pytest.raises(HTTPException) as excinfo,
    ):
        await _save(session)

    assert excinfo.value.status_code == 409, excinfo.value.detail
    assert "connector_team_mismatch" in str(excinfo.value.detail), excinfo.value.detail
    save.assert_not_awaited()


async def test_cross_team_connector_binding_is_rejected_before_the_write() -> None:
    """The gate must fire BEFORE ``_save_graph``, not after.

    Rejecting post-write would still roll the transaction back, but the sibling
    endpoints order the binding check ahead of the write so no graph-mutation
    side effect (Agent-row sync, audit rows) is ever staged for a binding that
    cannot be stored. Assert the ordering, not just the status.
    """
    session = _enforcement_session(connector_visibility="team", connector_owner_team=_OTHER_TEAM)
    save = AsyncMock(return_value=([], []))
    resolve = AsyncMock(return_value=([], []))

    with (
        patch(f"{_PREFIX}_save_graph", new=save),
        patch(f"{_PREFIX}_resolve_graph_references", new=resolve),
        pytest.raises(HTTPException) as excinfo,
    ):
        await _save(session)

    assert excinfo.value.status_code == 409
    save.assert_not_awaited()
    resolve.assert_not_awaited()


async def test_org_visibility_connector_on_team_pipeline_reaches_the_write() -> None:
    """FAR-1618: a TEAM pipeline pinning an ORG-visibility connector PERSISTS.

    Org-wide resources are shared across the organisation — teams are a
    visibility grouping, not a credential trust boundary — so the save gate
    has nothing to refuse. This is the exact request the FAR-1515 reverse
    direction rejected 409 (with a "flip the connector to `team`" detail) and
    that the FAR-516 run-gate would then have failed at execution time; both
    are removed, and ``_save_graph`` must now be reached.
    """
    session = _enforcement_session(connector_visibility="org", connector_owner_team=None)
    saved_nodes = [_converted_node()]
    save = AsyncMock(return_value=(saved_nodes, []))

    with (
        patch(f"{_PREFIX}_save_graph", new=save),
        patch(f"{_PREFIX}_resolve_graph_references", new=AsyncMock(return_value=([], []))),
        patch(f"{_PREFIX}_validate_graph_save", new=AsyncMock(return_value=[])),
    ):
        # ``_save``'s default pipeline owner IS ``_PIPELINE_TEAM`` - a team pipeline.
        result = await _save(session)

    assert result is not None
    assert result[0] == saved_nodes
    save.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. Permitted connectors are accepted: org pipeline + org connector,
#    or the pipeline's own team connector (FAR-1515)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("visibility", "owner_team", "pipeline_owner_team"),
    [
        # An ORG-wide connector is usable by an ORG pipeline (unchanged rule).
        ("org", None, None),
        # A team-private connector IS usable by a pipeline owned by that team.
        ("team", _PIPELINE_TEAM, _PIPELINE_TEAM),
        # FAR-1618: an ORG-wide connector is usable by a TEAM pipeline too —
        # org resources stay shared across the organisation.
        ("org", None, _PIPELINE_TEAM),
        # The remaining direction is refused and has its own test: a
        # team-private connector on an org pipeline or another team's
        # pipeline (covered by the mismatch predicate tests).
    ],
    ids=["org_visible_on_org_pipeline", "same_team", "org_visible_on_team_pipeline"],
)
async def test_allowed_connector_bindings_reach_the_write(
    visibility: str,
    owner_team: uuid.UUID | None,
    pipeline_owner_team: uuid.UUID | None,
) -> None:
    """The gate must not blanket-block: permitted bindings still get persisted."""
    session = _enforcement_session(connector_visibility=visibility, connector_owner_team=owner_team)
    saved_nodes = [_converted_node()]
    save = AsyncMock(return_value=(saved_nodes, []))

    with (
        patch(f"{_PREFIX}_save_graph", new=save),
        patch(f"{_PREFIX}_resolve_graph_references", new=AsyncMock(return_value=([], []))),
        patch(f"{_PREFIX}_validate_graph_save", new=AsyncMock(return_value=[])),
    ):
        result = await _save(session, pipeline_owner_team=pipeline_owner_team)

    assert result is not None
    assert result[0] == saved_nodes
    save.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. _resolve_graph_references runs before the write
# ---------------------------------------------------------------------------


async def test_resolve_graph_references_runs_before_the_write_and_stops_it() -> None:
    """A reference-resolution failure (422) must stop the graph write.

    Mirrors the sibling order: resolve BEFORE ``replace_pipeline_graph``. If it
    ran after, the write would already be staged and only the rollback would
    hide it.
    """
    session = _gate_clearing_connector_session()
    save = AsyncMock(return_value=([], []))
    resolve = AsyncMock(side_effect=HTTPException(status_code=422, detail="Unknown agent IDs"))

    with (
        patch(f"{_PREFIX}_save_graph", new=save),
        patch(f"{_PREFIX}_resolve_graph_references", new=resolve),
        patch(f"{_PREFIX}_validate_graph_save", new=AsyncMock(return_value=[])),
        pytest.raises(HTTPException) as excinfo,
    ):
        await _save(session)

    assert excinfo.value.status_code == 422
    resolve.assert_awaited_once()
    save.assert_not_awaited()


async def test_resolve_graph_references_receives_the_pipeline_owner_team() -> None:
    """The model-backend team check is only meaningful with the OWNER team id.

    Passing ``None`` here would make every team-private backend look usable by
    an org pipeline - the exact gap the sibling call sites close by passing
    ``pipeline.owner_team_id``.
    """
    session = _gate_clearing_connector_session()
    resolve = AsyncMock(return_value=([], []))

    with (
        patch(f"{_PREFIX}_save_graph", new=AsyncMock(return_value=([], []))),
        patch(f"{_PREFIX}_resolve_graph_references", new=resolve),
        patch(f"{_PREFIX}_validate_graph_save", new=AsyncMock(return_value=[])),
    ):
        await _save(session)

    assert resolve.await_count == 1
    _, kwargs = resolve.await_args
    assert kwargs["pipeline_owner_team_id"] == _PIPELINE_TEAM, kwargs


# ---------------------------------------------------------------------------
# 4. _validate_graph_save runs AFTER the write and sees the new binding
# ---------------------------------------------------------------------------


async def test_validate_graph_save_runs_after_the_write() -> None:
    """Validation must see the POST-write graph, so a blocking code rolls it back."""
    session = _gate_clearing_connector_session()
    saved_nodes = [_converted_node()]
    save = AsyncMock(return_value=(saved_nodes, []))
    validate = AsyncMock(return_value=[])

    with (
        patch(f"{_PREFIX}_save_graph", new=save),
        patch(f"{_PREFIX}_resolve_graph_references", new=AsyncMock(return_value=([], []))),
        patch(f"{_PREFIX}_validate_graph_save", new=validate),
    ):
        await _save(session)

    save.assert_awaited_once()
    validate.assert_awaited_once()
    assert validate.await_args_list[0].args or validate.await_args_list[0].kwargs


async def test_validate_graph_save_receives_the_converted_nodes_connector_binding() -> None:
    """The validator must see the binding the request just introduced.

    Feeding it the PRE-conversion bindings would let a guardrail/redaction rule
    keyed on the new connector slip through untouched.
    """
    session = _gate_clearing_connector_session()
    validate = AsyncMock(return_value=[])

    with (
        patch(f"{_PREFIX}_save_graph", new=AsyncMock(return_value=([_converted_node()], []))),
        patch(f"{_PREFIX}_resolve_graph_references", new=AsyncMock(return_value=([], []))),
        patch(f"{_PREFIX}_validate_graph_save", new=validate),
    ):
        await _save(session)

    kwargs = validate.await_args.kwargs
    assert kwargs["connector_bindings"] == [{"node_id": str(_NODE_ID), "connector_instance_id": str(_CONNECTOR_ID)}], (
        kwargs
    )


async def test_validate_graph_save_failure_propagates_out_of_the_save() -> None:
    """A blocking validation code must escape as 422 so the txn rolls back.

    ``_save_locked_graph`` deliberately does NOT swallow it: the sibling
    endpoints let ``_reject_graph_validation_issues``'s 422 propagate out of
    ``session.begin()`` so the already-performed write is undone.

    FAR-1277 widened the SUCCESS return to ``(nodes, edges, issues)``; this
    test is the pairing half of that change - the blocking path must still
    raise rather than come back as a third tuple element.
    """
    session = _gate_clearing_connector_session()
    validate = AsyncMock(side_effect=HTTPException(status_code=422, detail="GUARDRAIL_CAP_EXCEEDED"))

    with (
        patch(f"{_PREFIX}_save_graph", new=AsyncMock(return_value=([_converted_node()], []))),
        patch(f"{_PREFIX}_resolve_graph_references", new=AsyncMock(return_value=([], []))),
        patch(f"{_PREFIX}_validate_graph_save", new=validate),
        pytest.raises(HTTPException) as excinfo,
    ):
        await _save(session)

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail == "GUARDRAIL_CAP_EXCEEDED"
    validate.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4b. FAR-1277: advisory (non-blocking) issues come BACK to the caller
# ---------------------------------------------------------------------------


async def test_advisory_issues_are_returned_by_the_save() -> None:
    """A non-blocking issue must be RETURNED, not only logged.

    ``PATCH /graph`` and ``PATCH /{id}`` answer with ``validation_issues``; the
    conversion endpoints must be able to do the same, so the shared chokepoint
    widens its return to ``(nodes, edges, issues)``. Anything that only logged
    the advisory half would leave the two surfaces inconsistent.
    """
    session = _gate_clearing_connector_session()
    saved_nodes = [_converted_node()]
    advisory = MagicMock(code="guardrail_cap_advisory", severity="warning", message="over cap", node_id=_NODE_ID)

    with (
        patch(f"{_PREFIX}_save_graph", new=AsyncMock(return_value=(saved_nodes, []))),
        patch(f"{_PREFIX}_resolve_graph_references", new=AsyncMock(return_value=([], []))),
        patch(f"{_PREFIX}_validate_graph_save", new=AsyncMock(return_value=[advisory])),
    ):
        result = await _save(session)

    assert result is not None
    assert len(result) == 3, f"expected (nodes, edges, issues), got {len(result)} elements"
    saved_result_nodes, saved_edges, issues = result
    assert saved_result_nodes == saved_nodes
    assert saved_edges == []
    assert issues == [advisory], issues


# ---------------------------------------------------------------------------
# 5. A node the current schema can no longer parse fails closed, named
# ---------------------------------------------------------------------------


async def test_unparsable_stored_node_is_422_and_names_the_node() -> None:
    """A stored node that clears neither validation tier must not be written.

    ``_resolve_graph_references`` needs typed models (agent id, capability
    scope, schema pins); a node that cannot be typed cannot be checked, so the
    conversion fails closed with the offending node id instead of proceeding
    unchecked - and with a detail that names the node, so the failure is
    diagnosable rather than a bare "Data validation failed".
    """
    session = _gate_clearing_connector_session()
    broken = {"id": "not-a-uuid", "node_type": "agent", "position": {"x": 0, "y": 0}}
    save = AsyncMock(return_value=([], []))

    with (
        patch(f"{_PREFIX}_save_graph", new=save),
        patch(f"{_PREFIX}_resolve_graph_references", new=AsyncMock(return_value=([], []))),
        patch(f"{_PREFIX}_validate_graph_save", new=AsyncMock(return_value=[])),
        pytest.raises(HTTPException) as excinfo,
    ):
        await _save(session, nodes=[broken])

    assert excinfo.value.status_code == 422
    assert "not-a-uuid" in str(excinfo.value.detail), excinfo.value.detail
    save.assert_not_awaited()


# ---------------------------------------------------------------------------
# 6. A missing pipeline row still short-circuits to None (404 upstream)
# ---------------------------------------------------------------------------


async def test_missing_pipeline_row_returns_none() -> None:
    """``_save_graph`` returning None (row gone) must not run validation.

    The endpoint maps None to 404; validating a graph that was never written
    would report issues for a pipeline that no longer exists.
    """
    session = _gate_clearing_connector_session()
    validate = AsyncMock(return_value=[])

    with (
        patch(f"{_PREFIX}_save_graph", new=AsyncMock(return_value=None)),
        patch(f"{_PREFIX}_resolve_graph_references", new=AsyncMock(return_value=([], []))),
        patch(f"{_PREFIX}_validate_graph_save", new=validate),
    ):
        result = await _save(session)

    assert result is None
    validate.assert_not_awaited()


# ---------------------------------------------------------------------------
# 7. Stored graph array entries and persisted edge rows reach the write gate
# ---------------------------------------------------------------------------


def test_graph_nodes_as_models_skips_non_dict_entries() -> None:
    """A non-dict entry in the stored graph array must be skipped, not crash.

    ``graph_nodes_json`` is untyped JSON, so a malformed array can hold a
    scalar where a node dict is expected. ``_graph_nodes_as_models`` deliberately
    skips those entries rather than raising, so the entries that ARE dicts still
    get typed and reference-checked. This drives the skip guard directly - the
    endpoint path filters through ``extract_connector_bindings`` first.
    """
    from modulo.api.routes.pipelines import _graph_nodes_as_models

    models = _graph_nodes_as_models([_converted_node(), "not-a-node", 42, None])

    assert len(models) == 1
    assert str(models[0].id) == str(_NODE_ID)


async def test_saved_edge_rows_are_mapped_to_the_validator_shape() -> None:
    """Persisted edge rows must reach ``_validate_graph_save`` in validator shape.

    The sibling graph-write endpoints build the validator's reduced edge shape
    from the request payload; the node-conversion save holds PERSISTED rows, so
    ``_edge_row_to_validator`` translates them. A non-empty saved-edge list is
    the only way that helper runs, so this asserts the translation end to end -
    including the port defaults for a row that carries neither port.
    """
    session = _gate_clearing_connector_session()
    target_id = uuid.uuid4()

    def _edge(*, source_port: str | None, target_port: str | None) -> MagicMock:
        edge = MagicMock()
        edge.id = uuid.uuid4()
        edge.source_node_id = _NODE_ID
        edge.target_node_id = target_id
        edge.edge_type = "default"
        edge.condition_expression = None
        edge.hitl_review_config = None
        edge.source_port = source_port
        edge.target_port = target_port
        return edge

    explicit = _edge(source_port="out", target_port="in")
    defaulted = _edge(source_port=None, target_port=None)
    validate = AsyncMock(return_value=[])

    with (
        patch(f"{_PREFIX}_save_graph", new=AsyncMock(return_value=([_converted_node()], [explicit, defaulted]))),
        patch(f"{_PREFIX}_resolve_graph_references", new=AsyncMock(return_value=([], []))),
        patch(f"{_PREFIX}_validate_graph_save", new=validate),
    ):
        result = await _save(session)

    assert result is not None
    edges = validate.await_args.kwargs["validator_graph"]["edges"]
    assert edges == [
        {
            "source": str(_NODE_ID),
            "target": str(target_id),
            "type": "default",
            "condition_expression": None,
            "hitl_review_config": None,
            "source_port": "out",
            "target_port": "in",
        },
        {
            "source": str(_NODE_ID),
            "target": str(target_id),
            "type": "default",
            "condition_expression": None,
            "hitl_review_config": None,
            "source_port": "out",
            "target_port": "in",
        },
    ]
