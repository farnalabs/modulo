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
  team-scope gates as its siblings (the latter is pinned in
  ``test_route_introspection.py``).

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
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Select

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context, get_settings
from modulo.api.main import app
from modulo.api.routes.pipelines import (
    PipelineGraphNode,
    PipelineGraphNodePatch,
    _build_node_patch_graph,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
_SECRET = "sk-live-DO-NOT-LEAK-12345"

#: The stored graph every ``FROM pipelines`` row read serves (the request-time
#: team gate, the in-transaction gate and ``get_pipeline`` inside the delegated
#: write). Reset by the ``client`` fixture's teardown.
_STORED = SimpleNamespace(nodes=[], owner_team_id=None)


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
            # A node patch never changes topology: "no edges" is the stored
            # graph every test here seeds.
            result = MagicMock()
            result.scalars.return_value = []
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
    assert write.call_args.kwargs["patched_fields"] == ["timeout_seconds"]
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
    assert not write.call_args.kwargs["patched_fields"]


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


def test_every_patchable_field_exists_on_the_node_model() -> None:
    """The patch model MIRRORS ``PipelineGraphNode``. A field renamed there but
    not here would silently patch a key the node model drops on serialisation —
    a write that reads back right and changes nothing."""
    patch_fields = set(PipelineGraphNodePatch.model_fields)
    node_fields = set(PipelineGraphNode.model_fields)
    unknown = patch_fields - node_fields
    assert not unknown, f"PipelineGraphNodePatch declares fields unknown to PipelineGraphNode: {sorted(unknown)}"


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
