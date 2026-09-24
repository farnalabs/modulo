"""FAR-1181: pipeline graph node env/config credential masking.

Covers both sides of the masked graph round-trip:
- READ: GET /pipelines/{id}/graph and snapshot detail mask credential-bearing
  node fields (env_vars / context_files / composite_parameter_values /
  parameter_overrides) reusing the shipped maskers.
- WRITE: a full-replace graph write (PATCH /graph) round-tripping a masked
  read resolves mask echoes against the stored graph, so the mask literals are
  never persisted over the stored secrets.
"""

import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.middleware.sensitive_mask import (
    SENSITIVE_VALUE_MASK,
    mask_pipeline_graph_node,
    merge_masked_graph_nodes,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.uuid4()
_NOW = "2025-01-01T00:00:00Z"

_GHP_SECRET = "ghp_" + "0123456789abcdef" * 3  # over the pattern's 36-char floor
_STRIPE_SECRET = "sk_live_" + "0123456789abcdef" * 2


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture
def client() -> Any:
    from tests.unit.api.mock_session import configure_mock_session

    mock_session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = lambda: mock_session
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
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Read-side helper: mask_pipeline_graph_node
# ---------------------------------------------------------------------------


def test_mask_graph_node_masks_whole_value_under_sensitive_env_key() -> None:
    node = {"id": "n1", "env_vars": {"GITHUB_TOKEN": _GHP_SECRET}}

    masked = mask_pipeline_graph_node(node)

    assert masked["env_vars"]["GITHUB_TOKEN"] == SENSITIVE_VALUE_MASK
    # The caller's node dict is never mutated.
    assert node["env_vars"]["GITHUB_TOKEN"] == _GHP_SECRET


def test_mask_graph_node_masks_secret_value_under_plain_env_key() -> None:
    node = {"id": "n1", "env_vars": {"WEBHOOK_BODY": f"payload {_STRIPE_SECRET} end"}}

    masked = mask_pipeline_graph_node(node)

    assert masked["env_vars"]["WEBHOOK_BODY"] == f"payload {SENSITIVE_VALUE_MASK} end"


def test_mask_graph_node_leaves_plain_env_value_untouched() -> None:
    plain_url = "https://example.com/api"
    node = {"id": "n1", "env_vars": {"APP_URL": plain_url}}

    masked = mask_pipeline_graph_node(node)

    assert masked["env_vars"]["APP_URL"] == plain_url


def test_mask_graph_node_masks_context_file_values() -> None:
    node = {"id": "n1", "context_files": {"/tmp/creds.txt": f"token={_GHP_SECRET}"}}

    masked = mask_pipeline_graph_node(node)

    assert masked["context_files"]["/tmp/creds.txt"] == f"token={SENSITIVE_VALUE_MASK}"


def test_mask_graph_node_masks_deep_parameter_values_by_key() -> None:
    node = {
        "id": "n1",
        "composite_parameter_values": {"cfg": {"api_key": _STRIPE_SECRET}},
        "parameter_overrides": {"nested": {"password": "hunter2-secret"}},
    }

    masked = mask_pipeline_graph_node(node)

    assert masked["composite_parameter_values"]["cfg"]["api_key"] == SENSITIVE_VALUE_MASK
    assert masked["parameter_overrides"]["nested"]["password"] == SENSITIVE_VALUE_MASK


def test_mask_graph_node_non_secret_fields_pass_through() -> None:
    node = {
        "id": "n1",
        "label": "My Node",
        "connector_binding": {"connector_instance_id": "abc"},
        "env_vars": None,
    }

    masked = mask_pipeline_graph_node(node)

    assert masked["label"] == "My Node"
    assert masked["connector_binding"] == {"connector_instance_id": "abc"}
    assert masked["env_vars"] is None


def test_mask_graph_node_fail_closed_scrubs_when_masker_raises(monkeypatch: Any) -> None:
    node = {
        "id": "n1",
        "env_vars": {"APP_URL": "https://example.com"},
        "context_files": {"/tmp/f.txt": "plain text"},
    }

    def _boom(text: str) -> str:
        raise AssertionError("masker sentinel failure")

    monkeypatch.setattr("modulo.api.middleware.sensitive_mask.mask_secret_values_in_text", _boom)

    masked = mask_pipeline_graph_node(node)

    # Fail-closed: NO raw value survives any masker failure.
    assert masked["env_vars"]["APP_URL"] == SENSITIVE_VALUE_MASK
    assert masked["context_files"]["/tmp/f.txt"] == SENSITIVE_VALUE_MASK
    # The caller's node dict is still untouched.
    assert node["env_vars"]["APP_URL"] == "https://example.com"


# ---------------------------------------------------------------------------
# Write-side helper: merge_masked_graph_nodes
# ---------------------------------------------------------------------------


def test_merge_resolves_masked_echo_against_stored_value() -> None:
    stored = [{"id": "n1", "env_vars": {"GITHUB_TOKEN": _GHP_SECRET}}]
    incoming = [{"id": "n1", "env_vars": {"GITHUB_TOKEN": SENSITIVE_VALUE_MASK, "PLAIN": "x"}}]

    merged = merge_masked_graph_nodes(incoming, stored)

    assert merged[0]["env_vars"]["GITHUB_TOKEN"] == _GHP_SECRET
    assert merged[0]["env_vars"]["PLAIN"] == "x"


def test_merge_drops_masked_echo_without_stored_counterpart() -> None:
    incoming = [{"id": "n1", "env_vars": {"NEW_TOKEN": SENSITIVE_VALUE_MASK}}]

    merged = merge_masked_graph_nodes(incoming, [])

    assert "NEW_TOKEN" not in merged[0]["env_vars"]


def test_merge_drops_masked_echo_on_new_unmatched_node() -> None:
    incoming = [{"id": "new-node", "env_vars": {"TOKEN": SENSITIVE_VALUE_MASK}}]

    merged = merge_masked_graph_nodes(incoming, [{"id": "other", "env_vars": {}}])

    assert "TOKEN" not in merged[0]["env_vars"]


def test_merge_preserves_full_replace_semantics_for_non_echo_keys() -> None:
    stored = [{"id": "n1", "env_vars": {"REMOVED": "old-value", "KEPT": "kept-value"}}]
    incoming = [{"id": "n1", "env_vars": {"KEPT": "new-value"}}]

    merged = merge_masked_graph_nodes(incoming, stored)

    assert merged[0]["env_vars"] == {"KEPT": "new-value"}


def test_merge_deep_dict_echo_merges_against_stored() -> None:
    stored_cfg = {"cfg": {"api_key": _STRIPE_SECRET, "untouched": 1}}
    stored = [{"id": "n1", "composite_parameter_values": stored_cfg}]
    incoming = [
        {
            "id": "n1",
            "composite_parameter_values": {"cfg": {"api_key": SENSITIVE_VALUE_MASK}},
        }
    ]

    merged = merge_masked_graph_nodes(incoming, stored)

    assert merged[0]["composite_parameter_values"]["cfg"]["api_key"] == _STRIPE_SECRET
    # Non-echo stored parts survive the deep merge.
    assert merged[0]["composite_parameter_values"]["cfg"]["untouched"] == 1


def test_merge_echo_free_deep_dict_taken_wholesale() -> None:
    stored = [{"id": "n1", "parameter_overrides": {"old_key": "old", "untouched": 1}}]
    incoming = [{"id": "n1", "parameter_overrides": {"fresh": "value"}}]

    merged = merge_masked_graph_nodes(incoming, stored)

    # Full-replace semantics: removed keys are NOT resurrected from the stored
    # dict when the caller's dict carries no mask echo at all.
    assert merged[0]["parameter_overrides"] == {"fresh": "value"}


def test_merge_leaves_non_dict_entries_untouched() -> None:
    merged = merge_masked_graph_nodes(["oops"], [])  # type: ignore[list-item]

    assert merged == ["oops"]


# ---------------------------------------------------------------------------
# Endpoint round-trips
# ---------------------------------------------------------------------------

_NODE_WITH_SECRET = {
    "id": "2c7e9a10-8f3a-4d61-9b2c-4a5e6f809010",
    "node_type": "agent",
    "agent_id": "11111111-1111-1111-1111-111111111111",
    "position": {"x": 0, "y": 0},
    "env_vars": {"GITHUB_TOKEN": _GHP_SECRET, "APP_URL": "https://example.com"},
}


def _make_pipeline(graph_nodes_json: list[dict[str, Any]]) -> MagicMock:
    pipeline = MagicMock()
    pipeline.id = _PIPELINE_ID
    pipeline.organisation_id = _ORG_ID
    pipeline.name = "Test Pipeline"
    pipeline.description = None
    pipeline.visibility = "org"
    pipeline.owner_team_id = None
    pipeline.folder_id = None
    pipeline.created_by = uuid.uuid4()
    pipeline.account_id = pipeline.created_by
    pipeline.graph_nodes_json = graph_nodes_json
    pipeline.rate_limit_config = None
    pipeline.max_duration_seconds = None
    pipeline.archived_at = None
    pipeline.snapshot_count = 0
    pipeline.created_at = _NOW
    pipeline.updated_at = _NOW
    pipeline.run_context_defaults = {}
    pipeline.default_autonomy_level = "manual_approval"
    pipeline.node_timeout_seconds = 300
    pipeline.max_concurrent_runs = 5
    pipeline.lock_wait_timeout_seconds = 300
    return pipeline


def test_get_graph_masks_node_env(client: TestClient) -> None:
    with (
        patch(
            "modulo.api.routes.pipelines.get_pipeline_graph",
            return_value=([dict(_NODE_WITH_SECRET)], []),
        ),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/graph")

    assert resp.status_code == 200
    env = resp.json()["nodes"][0]["env_vars"]
    assert env["GITHUB_TOKEN"] == SENSITIVE_VALUE_MASK
    assert env["APP_URL"] == "https://example.com"


def test_patch_graph_resolves_masked_echo_against_stored(client: TestClient) -> None:
    pipeline = _make_pipeline([dict(_NODE_WITH_SECRET)])
    validation = MagicMock()
    validation.issues = []
    masked_node = dict(_NODE_WITH_SECRET)
    masked_node["env_vars"] = {
        "GITHUB_TOKEN": SENSITIVE_VALUE_MASK,
        "APP_URL": "https://example.com",
    }

    with (
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            return_value=([dict(_NODE_WITH_SECRET)], []),
        ) as replace_mock,
        patch(
            "modulo.api.routes.pipelines.GraphValidator.validate_definition",
            return_value=validation,
        ),
        patch(
            "modulo.api.routes.pipelines._resolve_graph_references",
            return_value=([], []),
        ),
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": [masked_node], "edges": []},
        )

    assert resp.status_code == 200
    written_nodes = replace_mock.await_args.kwargs["nodes"]
    # The mask echo was resolved against the stored graph BEFORE the write.
    assert written_nodes[0]["env_vars"]["GITHUB_TOKEN"] == _GHP_SECRET


def test_patch_graph_persists_echoless_deletion(client: TestClient) -> None:
    pipeline = _make_pipeline([dict(_NODE_WITH_SECRET)])
    validation = MagicMock()
    validation.issues = []
    emptied_node = {**_NODE_WITH_SECRET, "env_vars": {}}

    with (
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            return_value=([dict(emptied_node)], []),
        ) as replace_mock,
        patch(
            "modulo.api.routes.pipelines.GraphValidator.validate_definition",
            return_value=validation,
        ),
        patch(
            "modulo.api.routes.pipelines._resolve_graph_references",
            return_value=([], []),
        ),
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": [emptied_node], "edges": []},
        )

    assert resp.status_code == 200
    written_nodes = replace_mock.await_args.kwargs["nodes"]
    # Full-replace honored: a key the caller removed stays removed.
    assert "GITHUB_TOKEN" not in written_nodes[0]["env_vars"]


def test_snapshot_detail_masks_graph_nodes(client: TestClient) -> None:
    snapshot_id = uuid.uuid4()
    snapshot = SimpleNamespace(
        id=snapshot_id,
        pipeline_id=_PIPELINE_ID,
        snapshot_version=1,
        tag=None,
        notes=None,
        created_at=_NOW,
        account_id=_USER_ID,
        version_kind="edit",
        created_kind="edit",
        draft=False,
        channel="none",
        graph_json={"nodes": [dict(_NODE_WITH_SECRET)], "edges": []},
        connector_bindings_json=[],
        schema_pins_json=[],
        prompt_pins_json=[],
        model_backend_pins_json=[],
        default_autonomy_level="manual_approval",
        max_autonomy_level=None,
        run_context_defaults={},
    )

    with (
        patch(
            "modulo.api.routes.pipelines.get_snapshot_detail",
            return_value=snapshot,
        ),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{snapshot_id}")

    assert resp.status_code == 200
    env = resp.json()["graph_json"]["nodes"][0]["env_vars"]
    assert env["GITHUB_TOKEN"] == SENSITIVE_VALUE_MASK
    assert env["APP_URL"] == "https://example.com"


# ---------------------------------------------------------------------------
# Run fixture export: GET /runs/{id}/export-fixture
# ---------------------------------------------------------------------------

_RUN_ID = uuid.uuid4()
_RUN_PIPELINE_ID = uuid.uuid4()


def test_export_fixture_masks_snapshot_graph_credentials(client: TestClient) -> None:
    """The snapshot graph in the fixture export is masked like every other read.

    Snapshots store the graph with REAL stored values (mask echoes are resolved
    back to secrets on write), so serialising ``snapshot.graph_json`` raw would
    leak node ``env_vars`` / ``context_files`` / parameter values to any caller
    holding ``run.output`` — the same credential class the graph, snapshot
    detail, MCP tool and composite-template reads mask.
    """
    from modulo.db.crud.run_node_outputs import RunBlobs

    node = {
        "id": "n1",
        "node_type": "agent",
        # Key tier (GITHUB_TOKEN) AND value tier (the ghp_ pattern under a
        # key that is not itself key-classified).
        "env_vars": {
            "GITHUB_TOKEN": _GHP_SECRET,
            "DEPLOY_NOTES": f"rollback to v1 with {_GHP_SECRET}",
            "APP_URL": "https://example.com",
        },
        "context_files": {"/tmp/creds.txt": f"token={_STRIPE_SECRET}"},
        "composite_parameter_values": {"cfg": {"api_key": _STRIPE_SECRET}},
        "parameter_overrides": {"nested": {"password": "hunter2-secret"}},
    }
    graph_json = {"nodes": [node], "edges": []}
    run = MagicMock()
    run.id = _RUN_ID
    run.pipeline_id = _RUN_PIPELINE_ID
    run.snapshot_id = uuid.uuid4()
    run.status = "complete"
    run.input_payload = {"prompt": "ship it", "api_key": _GHP_SECRET}
    run.outputs_json = {"node-1": "done"}
    run.node_telemetry_json = None
    snapshot = SimpleNamespace(id=run.snapshot_id, graph_json=graph_json)

    with (
        patch("modulo.api.routes.runs.get_run", new=AsyncMock(return_value=run)),
        patch(
            "modulo.api.routes.runs._load_snapshot_for_run",
            new=AsyncMock(return_value=snapshot),
        ),
        patch(
            "modulo.api.routes.runs.read_run_blobs",
            new=AsyncMock(return_value=RunBlobs(outputs={"node-1": "done"}, telemetry={}, markers=None)),
        ),
        patch("modulo.api.routes.runs.set_rls_org", new=AsyncMock()),
    ):
        resp = client.get(f"/api/v1/runs/{_RUN_ID}/export-fixture")

    assert resp.status_code == 200
    body = resp.json()

    # The snapshot graph must NOT carry the raw stored secrets.
    graph_dump = json.dumps(body["snapshot_graph_json"])
    assert _GHP_SECRET not in graph_dump
    assert _STRIPE_SECRET not in graph_dump
    assert "hunter2-secret" not in graph_dump

    masked_node = body["snapshot_graph_json"]["nodes"][0]
    assert masked_node["env_vars"]["GITHUB_TOKEN"] == SENSITIVE_VALUE_MASK
    assert "rollback to v1 with" in masked_node["env_vars"]["DEPLOY_NOTES"]
    # Non-secret values still surface so the fixture stays usable.
    assert masked_node["env_vars"]["APP_URL"] == "https://example.com"
    # Non-secret graph structure passes through unchanged.
    assert body["snapshot_graph_json"]["edges"] == graph_json["edges"]

    # input_payload / outputs_json masking is unchanged by the graph fix.
    assert _GHP_SECRET not in json.dumps(body["input_payload"])
    assert body["input_payload"] == {"prompt": "ship it", "api_key": SENSITIVE_VALUE_MASK}
    assert body["outputs_json"] == {"node-1": "done"}


# ---------------------------------------------------------------------------
# MCP resource: modulo://pipelines/{id}/snapshots/{snapshot_id}
# ---------------------------------------------------------------------------


async def _call_mcp_snapshot_resource(snapshot: Any) -> str:
    import modulo.api.mcp_server as ms

    ms._ctx_org_id.set(_ORG_ID)
    mock_session: Any = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
        patch.object(ms, "_session", return_value=session_cm),
        patch("modulo.api.mcp_server._pipeline_owner_team_id", new=AsyncMock(return_value=None)),
        patch(
            "modulo.db.crud.pipeline_snapshot_versioning.get_snapshot_detail",
            new=AsyncMock(return_value=snapshot),
        ),
    ):
        return await ms.resource_pipeline_snapshot_detail(str(uuid.uuid4()), str(uuid.uuid4()))


async def test_mcp_snapshot_detail_masks_node_credentials() -> None:
    node = {
        "id": "n1",
        "node_type": "agent",
        "agent_prompt": "bright prompt",
        "agent_commands": ["run it"],
        # Key tier (GITHUB_TOKEN is key-classified) AND value tier (the ghp_
        # pattern under a non-sensitive key must still be redacted).
        "env_vars": {
            "GITHUB_TOKEN": _GHP_SECRET,
            "DEPLOY_NOTES": f"rollback to v1 with {_GHP_SECRET}",
            "APP_URL": "https://example.com",
        },
        "context_files": {"/tmp/creds.txt": f"token={_STRIPE_SECRET}"},
        "composite_parameter_values": {"cfg": {"api_key": _STRIPE_SECRET}},
        "parameter_overrides": {"nested": {"password": "hunter2-secret"}},
    }
    snapshot = SimpleNamespace(
        id=uuid.uuid4(),
        snapshot_version=2,
        graph_json={"nodes": [node], "edges": []},
        connector_bindings_json=[],
    )

    result = await _call_mcp_snapshot_resource(snapshot)

    assert _GHP_SECRET not in result
    assert _STRIPE_SECRET not in result
    assert "hunter2-secret" not in result
    # Masking preserves the node context itself — non-secret values still render.
    assert "APP_URL" in result
    assert "https://example.com" in result
    # Value-tier redaction keeps the surrounding non-secret text intact.
    assert "rollback to v1 with" in result
    # The mask literal survives json.dumps() unicode-escaping in the node JSON.
    assert json.dumps(SENSITIVE_VALUE_MASK)[1:-1] in result
