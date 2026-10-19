"""FAR-1829: ``PATCH /api/v1/pipelines/{pipeline_id}/graph/nodes/{node_id}``.

The node-targeted patch exists so editing ONE field of ONE node does not
require a whole-graph rewrite (the caller re-emitting every node, multi-KB
``agent_commands`` and masked credentials included, where a one-integer change
could silently drop a field). The design contract these tests pin:

* it is a THIN ADAPTER over the existing full-graph write — the merge is handed
  to ``_apply_graph_update``, so ``replace_pipeline_graph`` (the shared write)
  receives the WHOLE merged graph, never a node fragment;
* the allowlist is CLOSED — a credential-bearing dict, a structural field or an
  unknown key is a 422 naming the offending field, never a passthrough;
* validation runs on the MERGED node, not on the partial body, so a body value
  that is only invalid *in combination with the stored node* is rejected (the
  property a body-only check cannot have);
* unknown ``node_id`` is a 404, and the route carries the same permission /
  team-scope gates as its siblings (structure pinned in
  ``test_route_introspection.py``, BEHAVIOUR — both layers of the two-layer
  team gate actually executing — in
  ``test_pipeline_graph_node_patch_team_gate.py``).

These tests drive the REAL route handler with the real in-transaction team-gate
helper, the real merge helpers and the real request-validation path; only the
row write (``replace_pipeline_graph``), the reference resolver and the graph
validator's DB-backed pass are doubled — the doubled write's call kwargs are
the observation for "the shared write was handed this payload".
"""

from __future__ import annotations

import uuid
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from types import SimpleNamespace, UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from annotated_types import Gt
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import Field, ValidationError
from pydantic.fields import FieldInfo
from sqlalchemy import Select
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context, get_settings
from modulo.api.main import app
from modulo.api.routes.pipelines import (
    PipelineGraphEdge,
    PipelineGraphNode,
    PipelineGraphNodePatch,
    _build_node_patch_graph,
    _edge_to_dict,
    _joined_validation_detail,
    _stored_edge_model,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.pipeline_engine.git_content import GitContentRefError
from modulo.db.crud.hitl_review_guard import GuardrailBindingStripDenied
from modulo.settings import Settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
_SECRET = "sk-live-DO-NOT-LEAK-12345"

#: The stored graph every ``FROM pipelines`` row read serves (the request-time
#: team gate, the in-transaction gate and ``get_pipeline`` inside the delegated
#: write), plus the persisted ``PipelineEdge`` rows the locked graph read
#: serves. Reset by the ``client`` fixture's teardown.
_STORED = SimpleNamespace(nodes=[], edges=[], owner_team_id=None)


def _sandbox_node(node_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    """A minimal VALID llm-mode ``sandbox_agent`` stored node.

    Llm mode requires a non-empty ``agent_prompt`` and ``agent_commands``
    (``sandbox_mode._validate_sandbox_mode_config``), so both are seeded — a
    target node that failed its own field validation would make every 422 below
    meaningless.
    """
    node: dict[str, Any] = {
        "id": str(node_id),
        "node_type": "sandbox_agent",
        "position": {"x": 0, "y": 0},
        "template_id": "opencode",
        "agent_prompt": "do the thing",
        "agent_commands": ["opencode run --task"],
        "label": "Review",
        "timeout_seconds": 600,
        "env_vars": {"API_TOKEN": _SECRET},
        "context_files": {"/home/user/.env": "TOKEN=1"},
    }
    node.update(extra)
    return node


def _agent_node(node_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    """A minimal valid bound ``agent`` node — the node type whose
    type-specific rules a partial body alone can never check."""
    node: dict[str, Any] = {
        "id": str(node_id),
        "node_type": "agent",
        "position": {"x": 0, "y": 0},
        "agent_id": str(uuid.uuid4()),
        "label": "Agent",
    }
    node.update(extra)
    return node


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@contextmanager
def _graph_write_doubles() -> Iterator[None]:
    """Double only the row write, the reference resolver and the DB-backed
    validator pass.

    Everything else — the request model, the allowlist rejection, the locked
    row read, the merge helpers, the Agent-row sync call, the response — runs
    for real.
    """
    validation = MagicMock()
    validation.issues = []
    with (
        patch("modulo.api.routes.pipelines.GraphValidator.validate_definition", return_value=validation),
        patch("modulo.api.routes.pipelines._resolve_graph_references", return_value=([], [])),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        yield


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

    default_side_effect = mock_session.execute.side_effect

    def execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        rendered = str(stmt) if isinstance(stmt, Select) else ""
        if "FROM pipelines" in rendered:
            row = MagicMock()
            row.visibility = "org"
            row.owner_team_id = _STORED.owner_team_id
            row.deleted_at = None
            # Read FRESH: tests seed ``_STORED.nodes`` after this fixture runs.
            row.graph_nodes_json = _STORED.nodes
            result = MagicMock()
            # Row-shaped reads (``get_pipeline``, the FOR UPDATE team gate).
            result.scalar_one_or_none.return_value = row
            result.scalar_one.return_value = row
            # Tuple-shaped read (``team_scope_resolver``'s ``SELECT
            # owner_team_id, visibility``): org-visible, no owner team.
            result.first.return_value = (None, "org")
            return result
        if "FROM pipeline_edges" in rendered:
            # Read FRESH: tests seed ``_STORED.edges`` after this fixture runs.
            # A node patch never changes topology, so the seeded rows pass
            # through the read + revalidation untouched.
            result = MagicMock()
            result.scalars.return_value = list(_STORED.edges)
            return result
        return default_side_effect(stmt, *args, **kwargs)

    mock_session.execute.side_effect = execute

    async def override_session() -> AsyncMock:
        yield mock_session

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)  # type: ignore[misc]
    app.dependency_overrides.clear()
    _STORED.nodes = []
    _STORED.edges = []
    _STORED.owner_team_id = None


def _patch_url(node_id: uuid.UUID) -> str:
    return f"/api/v1/pipelines/{_PIPELINE_ID}/graph/nodes/{node_id}"


def _write_double(nodes: list[dict[str, Any]]) -> Any:
    """Double the shared write, returning the graph it was handed."""
    return patch(
        "modulo.api.routes.pipelines.replace_pipeline_graph",
        return_value=(nodes, []),
    )


# ---------------------------------------------------------------------------
# Happy path: one field, one node, the whole graph handed to the shared write
# ---------------------------------------------------------------------------


def test_patch_changes_only_the_supplied_field_on_the_target_node(client: TestClient) -> None:
    """A one-integer edit must not drop anything: every OTHER field of the
    target (credentials included) and every OTHER node survive unchanged, and
    the shared write receives the whole merged graph."""
    target_id = uuid.uuid4()
    other_id = uuid.uuid4()
    target = _sandbox_node(target_id)
    other = _agent_node(other_id, description="untouched")
    _STORED.nodes = [target, other]

    with _graph_write_doubles(), _write_double([target, other]) as write:
        resp = client.patch(_patch_url(target_id), json={"timeout_seconds": 900})

    assert resp.status_code == 200, resp.text
    written_nodes = write.call_args.kwargs["nodes"]
    assert len(written_nodes) == 2, "the adapter must hand over the WHOLE graph, not a node fragment"
    written_by_id = {n["id"]: n for n in written_nodes}
    assert written_by_id[str(target_id)]["timeout_seconds"] == 900
    # Untouched fields on the patched node — the class of silent drop the
    # whole-graph rewrite made possible — are intact.
    assert written_by_id[str(target_id)]["env_vars"] == {"API_TOKEN": _SECRET}
    assert written_by_id[str(target_id)]["context_files"] == {"/home/user/.env": "TOKEN=1"}
    assert written_by_id[str(target_id)]["agent_prompt"] == "do the thing"
    assert written_by_id[str(target_id)]["label"] == "Review"
    assert written_by_id[str(target_id)]["agent_commands"] == ["opencode run --task"]
    # The other node keeps every field it was stored with. (The written dict is
    # a full model dump, so it also carries model defaults the sparse stored
    # dict omitted — exactly what a full-graph write has always persisted.)
    other_written = written_by_id[str(other_id)]
    assert {key: other_written.get(key) for key in other} == other
    # The audit companion knows which field moved — names only.
    assert write.call_args.kwargs["recording"].patched_fields == ["timeout_seconds"]
    # No topology change: the patch writes no edges of its own.
    assert not write.call_args.kwargs["edges"]


def test_patch_can_clear_a_nullable_field_with_null(client: TestClient) -> None:
    """An explicitly supplied ``null`` clears the stored value (an absent key
    would leave it alone): ``stall_timeout_seconds`` stored, ``null`` supplied."""
    target_id = uuid.uuid4()
    target = _sandbox_node(target_id, stall_timeout_seconds=120)
    _STORED.nodes = [target]

    with _graph_write_doubles(), _write_double([dict(target)]) as write:
        resp = client.patch(_patch_url(target_id), json={"stall_timeout_seconds": None})

    assert resp.status_code == 200, resp.text
    written = write.call_args.kwargs["nodes"][0]
    assert written["stall_timeout_seconds"] is None


def test_empty_patch_body_supplies_no_patched_fields(client: TestClient) -> None:
    """An empty body still re-saves (attribution fires as for any write) but
    carries no ``patched_fields``, so the pre-existing full-replace callers'
    payload shape is unchanged."""
    target_id = uuid.uuid4()
    target = _sandbox_node(target_id)
    _STORED.nodes = [target]

    with _graph_write_doubles(), _write_double([dict(target)]) as write:
        resp = client.patch(_patch_url(target_id), json={})

    assert resp.status_code == 200, resp.text
    assert not write.call_args.kwargs["recording"].patched_fields


def test_patch_syncs_agent_commands_to_the_bound_agent_row(client: TestClient) -> None:
    """``agent_commands`` is on the allowlist ONLY because the adapter reuses
    the full-graph write, which runs the ``_sync_agent_row_commands`` Agent-row
    sync in the same transaction — so "what you PATCH is what runs" (FAR-488)
    holds here exactly as it does for a full replace."""
    target_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    target = _sandbox_node(target_id, agent_id=str(agent_id), agent_commands=["opencode run -- old"])
    _STORED.nodes = [target]

    with (
        _graph_write_doubles(),
        _write_double([dict(target)]),
        patch(
            "modulo.api.routes.pipelines._sync_agent_row_commands",
            new=AsyncMock(return_value=1),
        ) as sync,
    ):
        resp = client.patch(_patch_url(target_id), json={"agent_commands": ["opencode run -- new"]})

    assert resp.status_code == 200, resp.text
    sync.assert_awaited_once()
    synced_nodes = sync.await_args.kwargs["nodes"]
    assert any(n.get("agent_commands") == ["opencode run -- new"] for n in synced_nodes)


def test_unknown_node_id_is_404(client: TestClient) -> None:
    """The path addresses a node that is not in the committed graph → 404, and
    no write is attempted."""
    _STORED.nodes = [_sandbox_node(uuid.uuid4())]

    with (
        _graph_write_doubles(),
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            new=AsyncMock(),
        ) as write,
    ):
        resp = client.patch(_patch_url(uuid.uuid4()), json={"timeout_seconds": 900})

    assert resp.status_code == 404, resp.text
    assert "Node not found" in resp.json()["detail"]
    write.assert_not_awaited()


# ---------------------------------------------------------------------------
# The CLOSED allowlist: never patchable, always a typed 422 field path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        # credential-bearing dicts — a partial body must never drop a secret
        "env_vars",
        "context_files",
        "composite_parameter_values",
        "parameter_overrides",
        # structural / topology — node_id addresses the node, never a body field
        "id",
        "node_type",
        "agent_id",
        "position",
        "node_id",
        # anything at all outside the allowlist
        "timeout",
        "mode",
    ],
    ids=[
        "env_vars",
        "context_files",
        "composite_parameter_values",
        "parameter_overrides",
        "id",
        "node_type",
        "agent_id",
        "position",
        "node_id",
        "unknown-key-timeout",
        "unknown-key-mode",
    ],
)
def test_non_allowlisted_field_is_rejected_with_a_typed_path(client: TestClient, field: str) -> None:
    """422 with ``body.<field>`` in the problem ``detail`` — the offending key
    is named, and nothing reaches the shared write."""
    target_id = uuid.uuid4()
    _STORED.nodes = [_sandbox_node(target_id)]

    with (
        _graph_write_doubles(),
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            new=AsyncMock(),
        ) as write,
    ):
        resp = client.patch(_patch_url(target_id), json={field: "anything"})

    assert resp.status_code == 422, resp.text
    assert f"body.{field}" in resp.json()["detail"], resp.json()["detail"]
    write.assert_not_awaited()


def test_credential_dict_is_refused_before_any_read_or_write(client: TestClient) -> None:
    """The credential dicts are UNPATCHABLE rather than mergeable: a body that
    supplies a partial ``env_vars`` is refused outright, so a secret can never
    be half-cleared by a one-field edit."""
    target_id = uuid.uuid4()
    _STORED.nodes = [_sandbox_node(target_id)]

    with (
        _graph_write_doubles(),
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            new=AsyncMock(),
        ) as write,
    ):
        resp = client.patch(_patch_url(target_id), json={"env_vars": {}})

    assert resp.status_code == 422, resp.text
    assert "body.env_vars" in resp.json()["detail"]
    write.assert_not_awaited()


def test_value_out_of_bounds_is_rejected_by_the_field_constraint(client: TestClient) -> None:
    """``timeout_seconds``'s own 60-604800 constraint applies to the BODY too,
    so an out-of-range value never reaches the merge."""
    target_id = uuid.uuid4()
    _STORED.nodes = [_sandbox_node(target_id)]

    with (
        _graph_write_doubles(),
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            new=AsyncMock(),
        ) as write,
    ):
        resp = client.patch(_patch_url(target_id), json={"timeout_seconds": 30})

    assert resp.status_code == 422, resp.text
    assert "timeout_seconds" in resp.json()["detail"]
    write.assert_not_awaited()


# ---------------------------------------------------------------------------
# Validate the MERGED node, not the partial body
# ---------------------------------------------------------------------------


def test_type_rule_violation_of_the_patched_node_is_rejected(client: TestClient) -> None:
    """``single_pr_per_run: true`` is a perfectly valid patch BODY; it is only
    invalid once merged into an ``agent`` node (a sandbox_agent-only field). A
    body-only check would pass it and store a declared-but-unread flag — a
    silent no-op at run time. The merged-node check rejects it, and the write
    never runs."""
    target_id = uuid.uuid4()
    _STORED.nodes = [_agent_node(target_id)]

    with (
        _graph_write_doubles(),
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            new=AsyncMock(),
        ) as write,
    ):
        resp = client.patch(_patch_url(target_id), json={"single_pr_per_run": True})

    assert resp.status_code == 422, resp.text
    assert "single_pr_per_run" in resp.json()["detail"], resp.json()["detail"]
    write.assert_not_awaited()


def test_cross_field_violation_against_the_stored_node_is_rejected(client: TestClient) -> None:
    """The cross-field proof: a script-mode sandbox node already carries
    ``script_command``. Patching ``agent_commands`` is valid on its own and is
    only invalid once merged with the STORED node — the two are mutually
    exclusive. A partial-body validation is structurally incapable of this
    check: it is handed neither field's counterpart."""
    target_id = uuid.uuid4()
    target = _sandbox_node(
        target_id,
        mode="script",
        script_command="python run.py",
        agent_commands=None,
    )
    _STORED.nodes = [target]

    with (
        _graph_write_doubles(),
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            new=AsyncMock(),
        ) as write,
    ):
        resp = client.patch(_patch_url(target_id), json={"agent_commands": ["opencode run -- x"]})

    assert resp.status_code == 422, resp.text
    assert "agent_commands" in resp.json()["detail"], resp.json()["detail"]
    write.assert_not_awaited()


def test_egress_policy_and_allowlist_are_patchable_together(client: TestClient) -> None:
    """The coupled egress pair must move as a unit — INTO ``selected``.

    ``_validate_sandbox_egress_allowlist_config`` fail-closes the pair:
    ``selected`` REQUIRES a non-empty allowlist and any other policy must NOT
    carry one. Before ``egress_allowlist`` joined the allowlist, every attempt
    to patch INTO (or out of) ``selected`` 422'd unconditionally, so
    ``egress_policy`` could never reach its purpose. Both halves supplied in
    one body must land together."""
    target_id = uuid.uuid4()
    target = _sandbox_node(target_id)
    _STORED.nodes = [target]

    with _graph_write_doubles(), _write_double([dict(target)]) as write:
        resp = client.patch(
            _patch_url(target_id),
            json={
                "egress_policy": "selected",
                "egress_allowlist": [{"host": "api.github.com", "port": 443}],
            },
        )

    assert resp.status_code == 200, resp.text
    written = write.call_args.kwargs["nodes"][0]
    assert written["egress_policy"] == "selected"
    assert written["egress_allowlist"] == [{"host": "api.github.com", "port": 443}]
    assert write.call_args.kwargs["recording"].patched_fields == ["egress_allowlist", "egress_policy"]


def test_egress_pair_can_be_cleared_out_of_selected(client: TestClient) -> None:
    """The reverse direction: a stored ``selected`` policy + allowlist moves to
    ``deny_all`` with an explicit ``null`` clearing the companion allowlist —
    the half that was unreachable while only ``egress_policy`` was patchable."""
    target_id = uuid.uuid4()
    target = _sandbox_node(
        target_id,
        egress_policy="selected",
        egress_allowlist=[{"host": "api.github.com", "port": 443}],
    )
    _STORED.nodes = [target]

    with _graph_write_doubles(), _write_double([dict(target)]) as write:
        resp = client.patch(
            _patch_url(target_id),
            json={"egress_policy": "deny_all", "egress_allowlist": None},
        )

    assert resp.status_code == 200, resp.text
    written = write.call_args.kwargs["nodes"][0]
    assert written["egress_policy"] == "deny_all"
    assert written["egress_allowlist"] is None


def test_egress_policy_patched_onto_a_non_sandbox_node_is_rejected(client: TestClient) -> None:
    """FAR-1846: ``egress_policy`` is valid at the body level but only the
    sandbox path derives an egress surface, so merged onto an ``agent`` node it
    is a declared-but-unread no-op. Before the fix this PATCH returned 200 and
    silently persisted the field."""
    target_id = uuid.uuid4()
    _STORED.nodes = [_agent_node(target_id)]

    with (
        _graph_write_doubles(),
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            new=AsyncMock(),
        ) as write,
    ):
        resp = client.patch(_patch_url(target_id), json={"egress_policy": "deny_all"})

    assert resp.status_code == 422, resp.text
    assert "egress_policy" in resp.json()["detail"], resp.json()["detail"]
    write.assert_not_awaited()


def test_egress_allowlist_patched_onto_a_non_sandbox_node_is_rejected(client: TestClient) -> None:
    """FAR-1846: same for the companion allowlist — its sole reader is the
    sandbox egress check, which never runs for a non-sandbox node."""
    target_id = uuid.uuid4()
    _STORED.nodes = [_agent_node(target_id)]

    with (
        _graph_write_doubles(),
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            new=AsyncMock(),
        ) as write,
    ):
        resp = client.patch(
            _patch_url(target_id),
            json={"egress_allowlist": [{"host": "example.com", "port": 443}]},
        )

    assert resp.status_code == 422, resp.text
    assert "egress_allowlist" in resp.json()["detail"], resp.json()["detail"]
    write.assert_not_awaited()


def test_egress_pair_echoed_as_null_on_a_non_sandbox_node_still_saves(client: TestClient) -> None:
    """FAR-1846: the rejection is by VALUE, so a body that echoes both fields
    as ``null`` on a non-sandbox node is the absent/default state and keeps
    saving — presence alone must not over-reject a uniform round-tripping
    form."""
    target_id = uuid.uuid4()
    target = _agent_node(target_id)
    _STORED.nodes = [target]

    with _graph_write_doubles(), _write_double([dict(target)]) as write:
        resp = client.patch(
            _patch_url(target_id),
            json={"egress_policy": None, "egress_allowlist": None},
        )

    assert resp.status_code == 200, resp.text
    written = write.call_args.kwargs["nodes"][0]
    assert written["egress_policy"] is None
    assert written["egress_allowlist"] is None


def test_merged_node_rejects_a_boolean_field_supplied_as_null(client: TestClient) -> None:
    """``enable_heartbeat: null`` is accepted by the patch body (the field is
    nullable there so ``None`` can mean "clear"); the MERGED node's own ``bool``
    field forbids it, so the write is refused rather than storing a value the
    stall detector cannot read."""
    target_id = uuid.uuid4()
    _STORED.nodes = [_sandbox_node(target_id, enable_heartbeat=True)]

    with (
        _graph_write_doubles(),
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            new=AsyncMock(),
        ) as write,
    ):
        resp = client.patch(_patch_url(target_id), json={"enable_heartbeat": None})

    assert resp.status_code == 422, resp.text
    assert "enable_heartbeat" in resp.json()["detail"], resp.json()["detail"]
    write.assert_not_awaited()


def test_model_level_rejection_renders_without_an_empty_field_path() -> None:
    """A ``model_validator`` error carries ``loc == ()`` — it is attributable
    to no single field, so ``_joined_validation_detail`` must render the bare
    Pydantic message (whose text names the offending field) with NO path
    prefix — never the ``": message"`` shape an empty dotted path produces."""
    merged = {**_agent_node(uuid.uuid4()), "await_completion": True}
    with pytest.raises(ValidationError) as exc_info:
        PipelineGraphNode.model_validate(merged)

    detail = _joined_validation_detail(exc_info.value)

    assert not detail.startswith(":"), detail
    assert "await_completion" in detail, detail


def test_cross_field_model_validator_error_has_no_double_colon_in_the_response(client: TestClient) -> None:
    """The HTTP-level counterpart: ``await_completion=true`` patched onto an
    ``agent`` node is rejected by the MERGED node's model validator, and the
    422 detail must name the field WITHOUT the empty-path ``": "`` (a leading
    colon + colon pair from the message join)."""
    target_id = uuid.uuid4()
    _STORED.nodes = [_agent_node(target_id)]

    with (
        _graph_write_doubles(),
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            new=AsyncMock(),
        ) as write,
    ):
        resp = client.patch(_patch_url(target_id), json={"await_completion": True})

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "await_completion" in detail, detail
    # "…merging the patch: <msg>" renders as ONE colon join; the empty-loc bug
    # produced "…patch: : Value error, …" (an empty path prefixed by ": ").
    assert ": :" not in detail, detail
    assert not detail.startswith(":"), detail
    write.assert_not_awaited()


def test_put_graph_duplicate_node_ids_render_without_an_empty_field_path(client: TestClient) -> None:
    """The SHARED request-validation renderer (``problem_from_validation_error``)
    carries the same empty-``loc`` rule as ``_joined_validation_detail``: a
    top-level model-level error renders its bare message, never ``": msg"``.
    ``PUT /graph``'s ``reject_database_conflicts`` validator — this feature's
    own sibling route — is the model validator whose errors flow through that
    renderer, so the detail must not start with ``":"`` and must name the
    offending condition either way the path is (or is not) prefixed."""
    dup_id = uuid.uuid4()
    nodes = [_agent_node(dup_id), _agent_node(dup_id)]

    resp = client.put(
        f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
        json={"nodes": nodes, "edges": []},
    )

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "must be unique" in detail, detail
    assert not detail.startswith(":"), detail
    assert ": :" not in detail, detail


# ---------------------------------------------------------------------------
# The adapter itself (read + merge + revalidate), without the HTTP layer
# ---------------------------------------------------------------------------


def test_build_node_patch_graph_preserves_node_order() -> None:
    """The merge rewrites ONE node's model and keeps every other node — and the
    list order — exactly as stored; reordering on every one-field edit would
    churn ``graph_nodes_json`` for no reason."""
    first_id = uuid.uuid4()
    target_id = uuid.uuid4()
    last_id = uuid.uuid4()
    stored = [_agent_node(first_id), _sandbox_node(target_id), _agent_node(last_id)]

    graph = _build_node_patch_graph(
        stored,
        [],
        node_id=target_id,
        patch=PipelineGraphNodePatch.model_validate({"description": "patched"}),
    )

    assert [str(n.id) for n in graph.nodes] == [str(first_id), str(target_id), str(last_id)]
    assert graph.nodes[1].description == "patched"
    assert graph.nodes[0].description is None
    assert graph.nodes[2].description is None


def test_build_node_patch_graph_404s_for_an_unknown_node() -> None:
    """The unknown-node 404 comes from the merge helper itself, before any
    write is reachable."""
    stored = [_agent_node(uuid.uuid4())]

    with pytest.raises(HTTPException) as exc_info:
        _build_node_patch_graph(
            stored,
            [],
            node_id=uuid.uuid4(),
            patch=PipelineGraphNodePatch.model_validate({"label": "x"}),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Node not found"


# ---------------------------------------------------------------------------
# The allowlist model itself — drift guards
# ---------------------------------------------------------------------------

#: The ONLY permitted annotation divergence between the two models: these four
#: node fields are non-nullable ``bool`` on :class:`PipelineGraphNode`, while
#: the patch body widens them to ``bool | None`` for a uniform body shape (an
#: explicit ``null`` on them is then rejected by the MERGED node with a 422 —
#: see the model docstring). Every other patchable field's annotation must be
#: IDENTICAL to the node's.
_NULL_WIDENED_NON_NULLABLE_BOOLEANS = frozenset(
    {"enable_heartbeat", "idempotent", "single_pr_per_run", "await_completion"}
)

#: Constraint metadata classes the parity guard KNOWS how to compare, by
#: ``type(meta).__name__``. Membership is checked LOUDLY: any other metadata
#: class on a patchable field pair fails the test naming the class, never
#: skipped (a whitelist the guard consults but does not police is exactly the
#: drift FAR-1829's iteration-2 review found — a ``Gt`` / pattern constraint
#: the old map did not name was compared by neither the annotation check nor
#: the constraint check, so a patch field could silently drop it and the body
#: would accept a value the merged node rejects).
#:
#: ``_PydanticGeneralMetadata`` is pydantic's internal carrier for
#: ``Field(pattern=...)`` (and friends) in this pydantic version; its
#: ``__eq__`` is identity-based, so the guard compares constraints by their
#: value-based ``repr``, not by ``==`` (observed: two ``Field(pattern="^a")``
#: instances are NOT ``==`` but share a repr).
_HANDLED_CONSTRAINT_KINDS = frozenset(
    {
        "Ge",
        "Le",
        "Gt",
        "Lt",
        "MaxLen",
        "MinLen",
        "MultipleOf",
        "Pattern",
        "StringConstraints",
        "Strict",
        "_PydanticGeneralMetadata",
    }
)


def _annotation_metadata(annotation: Any) -> Iterator[Any]:
    """Every metadata object embedded in an ``Annotated`` layer of ``annotation``.

    Pydantic moves ``Field(...)`` kwargs into ``field.metadata``, but a
    ``StrictBool | None`` field KEEPS its ``Strict`` inside the annotation's
    ``Annotated`` layer — a constraint must be found in either location or the
    comparison below silently loses it.
    """
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        yield from args[1:]
        yield from _annotation_metadata(args[0])
    elif origin in (Union, UnionType):
        for arg in get_args(annotation):
            yield from _annotation_metadata(arg)


def _constraint_metadata(field: Any) -> dict[str, list[str]]:
    """Every constraint metadata object on ``field``, by class name → sorted reprs.

    Reads BOTH locations Pydantic can stash a constraint — ``field.metadata``
    and the annotation's ``Annotated`` layers — so a constraint kind present
    on either side of a node/patch pair is always seen.
    """
    groups: dict[str, list[str]] = {}

    def add(meta: Any) -> None:
        groups.setdefault(type(meta).__name__, []).append(repr(meta))

    for meta in field.metadata:
        add(meta)
    for meta in _annotation_metadata(field.annotation):
        add(meta)
    return {kind: sorted(reprs) for kind, reprs in groups.items()}


def _assert_field_constraints_mirror(name: str, node_field: Any, patch_field: Any) -> None:
    """The node and patch field must carry the SAME constraints, of EVERY kind
    present on EITHER side — and any constraint class this guard does not
    understand fails loudly by name rather than being skipped."""
    node_groups = _constraint_metadata(node_field)
    patch_groups = _constraint_metadata(patch_field)
    unhandled = sorted((set(node_groups) | set(patch_groups)) - _HANDLED_CONSTRAINT_KINDS)
    assert not unhandled, (
        f"{name}: unhandled constraint metadata class(es) {unhandled} — the parity guard only "
        "compares classes in _HANDLED_CONSTRAINT_KINDS; extend the guard (never skip the class) "
        "before a patchable field may carry one"
    )
    for kind in sorted(set(node_groups) | set(patch_groups)):
        node_reprs = node_groups.get(kind, [])
        patch_reprs = patch_groups.get(kind, [])
        assert node_reprs == patch_reprs, (
            f"{name}: constraint {kind} must mirror between node and patch — node={node_reprs!r} patch={patch_reprs!r}"
        )


def _literal_groups(annotation: Any) -> tuple[tuple[Any, ...], ...]:
    """Every ``Literal[...]`` value tuple reachable from an annotation."""
    if get_origin(annotation) is Literal:
        return (get_args(annotation),)
    groups: list[tuple[Any, ...]] = []
    for arg in get_args(annotation):
        groups.extend(_literal_groups(arg))
    return tuple(groups)


def _strip_optional(annotation: Any) -> Any:
    """Remove ``None`` from a union annotation (identity when not optional)."""
    args = get_args(annotation)
    if type(None) not in args:
        return annotation
    rest = [arg for arg in args if arg is not type(None)]
    return rest[0] if len(rest) == 1 else annotation


def _is_bool_like(annotation: Any) -> bool:
    """``bool`` itself, or an ``Annotated[bool, ...]`` such as ``StrictBool``."""
    if annotation is bool:
        return True
    if get_origin(annotation) is Annotated:
        return get_args(annotation)[0] is bool
    return False


def _is_strict_field(field: Any) -> bool:
    """True when the field rejects lax coercion (strict bool, either encoding)."""
    if any(getattr(meta, "strict", None) is True for meta in field.metadata):
        return True
    annotation = field.annotation
    candidates = [annotation, *[arg for arg in get_args(annotation) if arg is not type(None)]]
    for candidate in candidates:
        if get_origin(candidate) is Annotated and any(
            getattr(meta, "strict", None) is True for meta in get_args(candidate)[1:]
        ):
            return True
    return False


def test_every_patchable_field_exists_on_the_node_model() -> None:
    """The patch model MIRRORS ``PipelineGraphNode``. A field renamed there but
    not here would silently patch a key the node model drops on serialisation —
    a write that reads back right and changes nothing."""
    patch_fields = set(PipelineGraphNodePatch.model_fields)
    node_fields = set(PipelineGraphNode.model_fields)
    unknown = patch_fields - node_fields
    assert not unknown, f"PipelineGraphNodePatch declares fields unknown to PipelineGraphNode: {sorted(unknown)}"


def test_patchable_field_annotations_and_constraints_mirror_the_node_model() -> None:
    """Field-by-field parity: the patch body's annotations and constraints must
    actually MIRROR ``PipelineGraphNode`` — not merely use the same names.

    For every patchable field:

    * the annotation is IDENTICAL to the node's, except the documented
      ``None``-widening of the four non-nullable booleans (which must remain
      a plain ``bool`` on the node and a ``None``-stripped bool — strictness
      preserved — on the patch);
    * every constraint metadata object the node field carries is carried with
      the SAME value by the patch field — of EVERY constraint kind present on
      EITHER side (``Ge`` / ``Le`` / ``Gt`` / ``Lt`` / ``MaxLen`` / ``MinLen``
      / ``MultipleOf`` / pattern / ``Strict`` / …), compared by
      ``_assert_field_constraints_mirror``, which also fails loudly by name on
      any constraint class it does not understand rather than skipping it;
    * every ``Literal`` value group the node field carries is identical on the
      patch field.

    The constraint loop runs on the UNION of kinds present on either side, so
    a constraint on the NODE that the patch field omits is caught: without it
    the body would ACCEPT a value the merged node's own field validator
    REJECTS, breaking the "anything the body accepts, the merged node
    accepts" guarantee. The reverse direction — a constraint present only on
    the body — cannot make the body accept a value the node rejects (it only
    TIGHTENS the body, making a legitimate node value unpatchable), but it is
    still a mirror break, so it fails the same check.
    """
    patch_fields = PipelineGraphNodePatch.model_fields
    node_fields = PipelineGraphNode.model_fields

    divergent = {name for name, pf in patch_fields.items() if pf.annotation != node_fields[name].annotation}
    assert divergent == _NULL_WIDENED_NON_NULLABLE_BOOLEANS, (
        "every patchable field's annotation must be identical to the node's except the "
        f"documented None-widening of the four non-nullable booleans: divergent={sorted(divergent)}"
    )

    for name, pf in patch_fields.items():
        nf = node_fields[name]

        _assert_field_constraints_mirror(name, nf, pf)

        assert _literal_groups(pf.annotation) == _literal_groups(nf.annotation), (
            f"{name}: node Literal {_literal_groups(nf.annotation)} not mirrored by the "
            f"patch annotation {_literal_groups(pf.annotation)}"
        )

        if name in _NULL_WIDENED_NON_NULLABLE_BOOLEANS:
            assert nf.annotation is bool, f"{name}: expected a non-nullable bool on the node, got {nf.annotation!r}"
            assert _is_bool_like(_strip_optional(pf.annotation)), (
                f"{name}: the patch annotation must be the node's bool widened to optional, got {pf.annotation!r}"
            )
            assert _is_strict_field(pf) == _is_strict_field(nf), (
                f"{name}: strictness must be preserved across the None-widening "
                f"(node strict={_is_strict_field(nf)}, patch strict={_is_strict_field(pf)})"
            )


@pytest.mark.parametrize(
    ("node_annotation", "expected_kind"),
    [
        # ``gt`` — the kind the pre-completion whitelist (Ge/Le/MaxLen/MinLen)
        # silently ignored, live on ``PipelineGraphNode.wait_timeout``.
        (Annotated[float, Gt(gt=0)], "Gt"),
        # ``pattern`` — arrives as pydantic's internal general-metadata class,
        # NOT as a ``Pattern`` instance, so a name-based map misses it twice.
        (Annotated[str, Field(pattern="^a")], "_PydanticGeneralMetadata"),
    ],
)
def test_constraint_mirror_catches_a_kind_the_old_whitelist_would_ignore(
    node_annotation: Any, expected_kind: str
) -> None:
    """The guard must catch an OMITTED constraint of any kind — the exact
    defect the old ``_CONSTRAINT_ATTRS`` map had: a node field carrying
    ``Gt`` / a pattern was compared by neither the annotation check (the
    annotation is unchanged) nor the constraint check (the class was not in
    the four-name map), so a patch field could silently drop the constraint
    and the body would accept a value the merged node rejects."""
    node_like = FieldInfo.from_annotation(node_annotation)
    # The patch side's ANNOTATION is irrelevant here — the mirror check reads
    # constraint metadata only — so an empty FieldInfo stands for "the patch
    # field omits the constraint".
    patch_like = FieldInfo()

    with pytest.raises(AssertionError, match=expected_kind):
        _assert_field_constraints_mirror("synthetic", node_like, patch_like)


class _NovelConstraint:
    """A metadata class the parity guard has never seen."""


def test_constraint_mirror_fails_loudly_on_an_unhandled_constraint_class() -> None:
    """Even when BOTH sides carry the same constraint, a class outside
    ``_HANDLED_CONSTRAINT_KINDS`` must fail the guard naming the class — a
    guard that cannot compare a constraint kind must never silently skip it
    (that silent skip WAS the old whitelist's defect)."""
    node_like = FieldInfo.from_annotation(Annotated[int, _NovelConstraint()])
    patch_like = FieldInfo.from_annotation(Annotated[int, _NovelConstraint()])

    with pytest.raises(AssertionError, match="_NovelConstraint"):
        _assert_field_constraints_mirror("synthetic", node_like, patch_like)


def test_never_patchable_fields_are_absent_from_the_allowlist() -> None:
    """The credential dicts and structural fields must never appear on the
    patch model — their absence IS the enforcement (``extra="forbid"`` turns
    any attempt into a typed 422). ``agent_prompt`` is deliberately here too:
    on an ``agent`` node the runtime reads the bound Agent row's
    ``prompt_template`` instead, so a node-level patch would read back correct
    while the run used the old prompt — the FAR-488 desynchronisation class the
    Agent-row sync exists to prevent, with no sync available for this field."""
    forbidden = {
        "env_vars",
        "context_files",
        "composite_parameter_values",
        "parameter_overrides",
        "id",
        "node_type",
        "agent_id",
        "position",
        "connector_binding",
        "agent_prompt",
    }
    declared = set(PipelineGraphNodePatch.model_fields)
    leaked = forbidden & declared
    assert not leaked, f"never-patchable fields leaked into the allowlist: {sorted(leaked)}"


def test_patch_model_forbids_extra_keys() -> None:
    """The allowlist is closed at the model, not by a handler-side filter."""
    assert PipelineGraphNodePatch.model_config.get("extra") == "forbid"


# ---------------------------------------------------------------------------
# Stored-edge handling: a node patch must never touch an edge, and must never
# be blocked by one that no longer validates strictly
# ---------------------------------------------------------------------------


def _edge_row(**overrides: Any) -> SimpleNamespace:
    """A persisted ``PipelineEdge`` row shape, as ``_load_locked_pipeline_graph``
    returns it (ORM attributes only — no model semantics)."""
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "source_node_id": uuid.uuid4(),
        "target_node_id": uuid.uuid4(),
        "edge_type": "normal",
        "condition_expression": None,
        "hitl_review_config": None,
        "source_port": "out",
        "target_port": "in",
    }
    row.update(overrides)
    return SimpleNamespace(**row)


def test_patch_preserves_the_pipeline_existing_edges(client: TestClient) -> None:
    """THE edge-preservation wiring: the pipeline has a committed edge, the
    patch targets one node, and the shared write must receive that edge
    UNCHANGED (a node patch never changes topology). Before this test every
    node-patch test seeded an empty edge list, so the whole stored-edge read →
    revalidate → hand-over path was unpinned."""
    target_id = uuid.uuid4()
    other_id = uuid.uuid4()
    target = _sandbox_node(target_id)
    other = _agent_node(other_id)
    _STORED.nodes = [target, other]
    _STORED.edges = [
        _edge_row(source_node_id=target_id, target_node_id=other_id, edge_type="normal"),
    ]

    with _graph_write_doubles(), _write_double([target, other]) as write:
        resp = client.patch(_patch_url(target_id), json={"timeout_seconds": 900})

    assert resp.status_code == 200, resp.text
    written_edges = write.call_args.kwargs["edges"]
    assert len(written_edges) == 1, "the stored edge must be handed to the shared write unchanged"
    assert str(written_edges[0]["source_node_id"]) == str(target_id)
    assert str(written_edges[0]["target_node_id"]) == str(other_id)
    assert written_edges[0]["edge_type"] == "normal"
    # The node order and content are still the patch's responsibility.
    written_nodes = write.call_args.kwargs["nodes"]
    assert [n["id"] for n in written_nodes] == [str(target_id), str(other_id)]
    assert written_nodes[0]["timeout_seconds"] == 900


def test_stored_edge_model_round_trips_a_valid_edge() -> None:
    """The first tier of the stored-edge tolerance: a valid persisted row
    re-validates STRICTLY — no legacy fallback needed — and every field the
    node patch must not touch survives the round-trip."""
    row = _edge_row()

    model = _stored_edge_model(row)

    assert model.id == row.id
    assert model.source_node_id == row.source_node_id
    assert model.target_node_id == row.target_node_id
    assert model.edge_type == "normal"
    assert model.source_port == "out"
    assert model.target_port == "in"
    # The strict tier really is what accepted it (the legacy tier is not
    # doing the work): the same payload validates without any context.
    PipelineGraphEdge.model_validate(_edge_to_dict(row))


def test_stored_edge_model_tolerates_a_pre_farc613_gate_description_under_legacy_read() -> None:
    """The legacy-read tier: a stored HITL gate whose description predates the
    FAR-613 20-char minimum is rejected STRICTLY but accepted by the helper —
    the same stored-data tolerance the stored-node path has, so an unrelated
    one-field node patch is never blocked by a rule tightened since the edge's
    own save time. Fails if the legacy tier is removed (the strict rejection
    below would then escape the helper as a 422)."""
    row = _edge_row(
        hitl_review_config={"label": "Review", "description": "short", "claim_expiry_minutes": 30},
    )
    with pytest.raises(ValidationError):
        PipelineGraphEdge.model_validate(_edge_to_dict(row))

    model = _stored_edge_model(row)

    assert model.hitl_review_config is not None
    assert model.hitl_review_config.description == "short"


def test_stored_edge_model_422s_when_no_tier_accepts_the_edge() -> None:
    """Both tiers exhausted (an invalid ``edge_type`` fails even the legacy
    read — the pattern is a write-time invariant, not a tightened rule): the
    helper fails CLOSED with a 422 naming the stored edge, never a silent
    skip or a generic 500."""
    row = _edge_row(edge_type="bogus")

    with pytest.raises(HTTPException) as exc_info:
        _stored_edge_model(row)

    assert exc_info.value.status_code == 422
    detail = str(exc_info.value.detail)
    assert "no longer validates" in detail
    assert str(row.id) in detail, "the 422 must name the offending stored edge"


def test_build_node_patch_graph_422s_on_a_merged_graph_conflict() -> None:
    """The graph-level revalidation after the per-edge tolerance: two edges
    that each validate alone but share one (source, target, type) path are
    rejected by ``PipelineGraphUpdate``'s uniqueness rule, and the adapter
    surfaces that as the 422 "Merged graph rejected" — never a 500 from the
    model layer escaping the helper."""
    target_id = uuid.uuid4()
    stored = [_sandbox_node(target_id)]
    shared_path = {"source_node_id": uuid.uuid4(), "target_node_id": uuid.uuid4(), "edge_type": "normal"}
    dup_a = _edge_row(**shared_path)
    dup_b = _edge_row(**shared_path)  # distinct id, identical path

    with pytest.raises(HTTPException) as exc_info:
        _build_node_patch_graph(
            stored,
            [dup_a, dup_b],
            node_id=target_id,
            patch=PipelineGraphNodePatch.model_validate({"timeout_seconds": 900}),
        )

    assert exc_info.value.status_code == 422
    assert "Merged graph rejected" in str(exc_info.value.detail)


def test_build_node_patch_graph_skips_non_dict_stored_entries() -> None:
    """The order-preserving merge loop tolerates a corrupt non-dict entry in
    ``graph_nodes_json`` (defensive: the target-search loop already skips it)
    by continuing past it, keeping every dict node — and the stored ORDER —
    intact, with the target still patched."""
    first_id = uuid.uuid4()
    target_id = uuid.uuid4()
    last_id = uuid.uuid4()
    stored: list[Any] = [_agent_node(first_id), "corrupt-entry", _sandbox_node(target_id), _agent_node(last_id)]

    graph = _build_node_patch_graph(
        stored,
        [],
        node_id=target_id,
        patch=PipelineGraphNodePatch.model_validate({"timeout_seconds": 900}),
    )

    assert [str(n.id) for n in graph.nodes] == [str(first_id), str(target_id), str(last_id)]
    assert graph.nodes[1].timeout_seconds == 900


# ---------------------------------------------------------------------------
# The node-patch route's error arms — parity with the full-replace route
# ---------------------------------------------------------------------------


def test_guardrail_strip_denial_is_translated_to_403(client: TestClient) -> None:
    """Parity with ``PUT /graph``: a ``GuardrailBindingStripDenied`` raised by
    the delegated write is translated by the route's denial handler into the
    denial's own HTTP status (403) + detail — never the decorator's 500."""
    target_id = uuid.uuid4()
    bound_id = uuid.uuid4()
    _STORED.nodes = [_sandbox_node(target_id)]
    denied = GuardrailBindingStripDenied(
        stripped_node_ids=[str(bound_id)],
        detail=(
            "Non-admin cannot strip a guardrail binding: removing node(s) "
            + str(bound_id)
            + " from the graph would drop a node-bound guardrail. Only an "
            "admin can remove a node that has a bound guardrail."
        ),
    )

    with (
        _graph_write_doubles(),
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", side_effect=denied),
    ):
        resp = client.patch(_patch_url(target_id), json={"timeout_seconds": 900})

    assert resp.status_code == 403, resp.text
    assert "strip a guardrail binding" in resp.json()["detail"]


def test_unpinned_git_ref_sync_failure_is_mapped_to_422(client: TestClient) -> None:
    """Parity with ``PUT /graph``: the Agent-row command sync failing closed on
    an unpinned git content ref inside the delegated write surfaces HTTP 422
    (not the decorator's 500), naming the unpinned ref."""
    target_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    target = _sandbox_node(
        target_id,
        agent_id=str(agent_id),
        agent_commands=["git+https://github.com/acme/widgets.git@main#prompts/x.md"],
    )
    _STORED.nodes = [target]
    sync_error = GitContentRefError("agent_commands[0] git content ref ... is not pinned to a commit SHA")

    with (
        _graph_write_doubles(),
        patch(
            "modulo.api.routes.pipelines._sync_agent_row_commands",
            new=AsyncMock(side_effect=sync_error),
        ),
    ):
        resp = client.patch(_patch_url(target_id), json={"timeout_seconds": 900})

    assert resp.status_code == 422, resp.text
    assert "not pinned" in resp.json()["detail"]


def test_db_migration_error_is_mapped_to_501(client: TestClient) -> None:
    """Parity with ``PUT /graph``: a ``ProgrammingError`` from the delegated
    write (a relation the current schema has not migrated yet) surfaces the
    typed 501 "run database migrations" contract, not a generic 500."""
    target_id = uuid.uuid4()
    _STORED.nodes = [_sandbox_node(target_id)]

    with (
        _graph_write_doubles(),
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            side_effect=ProgrammingError("stmt", {}, Exception("relation does not exist")),
        ),
    ):
        resp = client.patch(_patch_url(target_id), json={"timeout_seconds": 900})

    assert resp.status_code == 501, resp.text
    assert "migrations" in resp.json()["detail"]
