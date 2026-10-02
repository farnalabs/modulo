"""FAR-1380: composite library primitive content masking.

A ``composite`` library primitive stores its sub-pipeline graph in
``content_json`` with the SAME credential-bearing node fields a pipeline graph
stores (env_vars / context_files / composite_parameter_values /
parameter_overrides). Restores the FAR-1181 read-mask invariant on the library
surface: every read that serialises a ``LibraryPrimitiveResponse`` masks those
node fields with the shipped per-node masker, and the composite write entries
follow the FAR-1374 echo-merge + sentinel-422 contract.

Surface enumeration: the surfaces below are the ONLY ones serialising
``library_primitives.content_json`` to a caller; the response-model validator
(``LibraryPrimitiveResponse._mask_composite_graph_credentials``) is the single
chokepoint behind all of them, so a new read surface inherits the mask rather
than silently reopening the hole. The MCP ``modulo://library/{type}/{slug}``
resource serialises ``content_json`` itself (no ``LibraryPrimitiveResponse``)
and is masked at its call site — covered by its own test below.
"""

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.base import PageResult
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
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
def client() -> TestClient:
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
# Fixtures
# ---------------------------------------------------------------------------

_COMPOSITE_SECRET_NODE: dict[str, Any] = {
    "id": "n1",
    "node_type": "agent",
    "position": {"x": 0, "y": 0},
    "env_vars": {
        # Key tier: GITHUB_TOKEN is key-classified.
        "GITHUB_TOKEN": _GHP_SECRET,
        # Not a credential: must survive so the primitive stays usable.
        "APP_URL": "https://example.com",
    },
    "context_files": {"/tmp/creds.txt": f"token={_STRIPE_SECRET}"},
    "composite_parameter_values": {"cfg": {"api_key": _STRIPE_SECRET}},
    "parameter_overrides": {"nested": {"password": "hunter2-secret"}},
}


def _make_composite_primitive() -> MagicMock:
    """Composite ORM primitive whose content nodes carry every credential class."""
    p = MagicMock()
    p.id = uuid.uuid4()
    p.organisation_id = _ORG_ID
    p.source = "local"
    p.primitive_type = "composite"
    p.name = "Sub Flow"
    p.slug = "sub-flow"
    p.description = "Composite sub-graph"
    p.author = _USER_ID.hex
    p.version = "1.0"
    p.tags = ["composite"]
    p.content_json = {"nodes": [_COMPOSITE_SECRET_NODE], "edges": []}
    p.source_url = None
    p.forked_from = None
    p.checksum = None
    p.ed25519_signature = None
    p.verified = None
    p.trust_tier = None
    p.tier = "native"
    p.download_count = 0
    p.average_rating = None
    p.review_count = 0
    p.owner_team_id = None
    p.visibility = "org"
    p.account_id = _USER_ID
    p.auto_update = True
    p.status = None
    p.manifest_pins = None
    p.trust_header = None
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


def _assert_masked_composite_body(body: dict[str, Any]) -> None:
    """The composite content nodes carry NO raw credential — sentinel + passthrough."""
    node = body["content_json"]["nodes"][0]
    assert node["env_vars"]["GITHUB_TOKEN"] == SENSITIVE_VALUE_MASK
    assert node["env_vars"]["APP_URL"] == "https://example.com"
    assert node["context_files"]["/tmp/creds.txt"] == f"token={SENSITIVE_VALUE_MASK}"
    assert node["composite_parameter_values"]["cfg"]["api_key"] == SENSITIVE_VALUE_MASK
    assert node["parameter_overrides"]["nested"]["password"] == SENSITIVE_VALUE_MASK
    # Belt-and-braces: no raw secret anywhere in the serialised body
    # (ensure_ascii=False keeps the bullet chars literal for the match).
    dump = json.dumps(body, ensure_ascii=False)
    assert _GHP_SECRET not in dump
    assert _STRIPE_SECRET not in dump
    assert "hunter2-secret" not in dump
    assert SENSITIVE_VALUE_MASK in dump


# ---------------------------------------------------------------------------
# The response-model chokepoint covers every serialisation surface
# ---------------------------------------------------------------------------


def test_response_validator_masks_composite_nodes() -> None:
    """Any LibraryPrimitiveResponse serialisation of a composite masks node creds."""
    from modulo.api.routes.library import LibraryPrimitiveResponse

    prim = _make_composite_primitive()
    resp = LibraryPrimitiveResponse.model_validate(prim)

    assert resp.primitive_type == "composite"
    node = resp.content_json["nodes"][0]
    assert node["env_vars"]["GITHUB_TOKEN"] == SENSITIVE_VALUE_MASK
    assert node["env_vars"]["APP_URL"] == "https://example.com"
    assert not resp.content_json["edges"]


def test_response_validator_never_mutates_the_source_orm_row() -> None:
    """The masker builds new dicts — the stored sentinel-free row stays raw."""
    from modulo.api.routes.library import LibraryPrimitiveResponse

    prim = _make_composite_primitive()
    LibraryPrimitiveResponse.model_validate(prim)

    assert prim.content_json["nodes"][0]["env_vars"]["GITHUB_TOKEN"] == _GHP_SECRET


def test_response_validator_leaves_non_composite_content_byte_identical() -> None:
    """Non-composite primitives pass through untouched — no accidental masking."""
    from modulo.api.routes.library import LibraryPrimitiveResponse

    p = MagicMock()
    p.id = uuid.uuid4()
    p.organisation_id = _ORG_ID
    p.source = "local"
    p.primitive_type = "workflow"
    p.name = "W"
    p.slug = "w"
    p.description = None
    p.author = "a"
    p.version = "1.0"
    p.tags = []
    p.content_json = {
        "agents": [{"name": "a", "prompt_template": f"key={_GHP_SECRET}"}],
        "graph_nodes": [dict(_COMPOSITE_SECRET_NODE)],
    }
    p.source_url = None
    p.forked_from = None
    p.checksum = None
    p.ed25519_signature = None
    p.verified = None
    p.trust_tier = None
    p.tier = "native"
    p.download_count = 0
    p.average_rating = None
    p.review_count = 0
    p.owner_team_id = None
    p.visibility = "org"
    p.account_id = _USER_ID
    p.auto_update = True
    p.status = None
    p.manifest_pins = None
    p.trust_header = None
    p.created_at = _NOW
    p.updated_at = _NOW

    resp = LibraryPrimitiveResponse.model_validate(p)

    # The library surface NEVER rewrites agent payloads: a workflow primitive's
    # content is byte-identical to storage — only a composite's top-level
    # "nodes" list is masked, and only on composite primitives.
    assert resp.content_json["agents"][0]["prompt_template"] == f"key={_GHP_SECRET}"
    assert resp.content_json["graph_nodes"][0]["env_vars"]["GITHUB_TOKEN"] == _GHP_SECRET


def test_response_validator_masks_only_the_nodes_key() -> None:
    """Masking touches ONLY content_json['nodes'] — sibling keys are preserved."""
    from modulo.api.routes.library import LibraryPrimitiveResponse

    prim = _make_composite_primitive()
    prim.content_json = {
        "nodes": [_COMPOSITE_SECRET_NODE],
        "edges": [{"source": "n1", "target": "n2", "hitl_review_config": {"x": 1}}],
        "notes": "sub graph",
    }

    resp = LibraryPrimitiveResponse.model_validate(prim)

    assert resp.content_json["notes"] == "sub graph"
    assert resp.content_json["edges"][0]["hitl_review_config"] == {"x": 1}
    assert resp.content_json["nodes"][0]["env_vars"]["GITHUB_TOKEN"] == SENSITIVE_VALUE_MASK


# ---------------------------------------------------------------------------
# Route surfaces (one per endpoint that serialises a LibraryPrimitiveResponse)
# ---------------------------------------------------------------------------


def test_read_surface_list_masks_composite(client: TestClient) -> None:
    prim = _make_composite_primitive()
    page = PageResult(items=[prim], total=1, page=1, page_size=20)
    with patch(
        "modulo.api.routes.library.list_primitives",
        new_callable=AsyncMock,
        return_value=page,
    ):
        resp = client.get("/api/v1/libraries")

    assert resp.status_code == 200, resp.text
    _assert_masked_composite_body(resp.json()["items"][0])


def test_read_surface_get_masks_composite(client: TestClient) -> None:
    prim = _make_composite_primitive()
    with (
        patch(
            "modulo.api.routes.library.get_primitive",
            new_callable=AsyncMock,
            return_value=prim,
        ),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/libraries/{prim.id}")

    assert resp.status_code == 200, resp.text
    _assert_masked_composite_body(resp.json())


def test_read_surface_create_response_masks_composite(client: TestClient) -> None:
    prim = _make_composite_primitive()
    with (
        patch(
            "modulo.api.routes.library.get_primitive_by_slug",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "modulo.api.routes.library.create_library_primitive",
            new_callable=AsyncMock,
            return_value=prim,
        ),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(
            "/api/v1/libraries",
            json={
                "primitive_type": "composite",
                "name": prim.name,
                "slug": prim.slug,
                "content_json": {"nodes": [dict(_COMPOSITE_SECRET_NODE)], "edges": []},
            },
        )

    assert resp.status_code == 201, resp.text
    _assert_masked_composite_body(resp.json())


def test_read_surface_patch_response_masks_composite(client: TestClient) -> None:
    prim = _make_composite_primitive()
    with (
        patch(
            "modulo.api.routes.library.get_library_primitive",
            new_callable=AsyncMock,
            return_value=prim,
        ),
        patch(
            "modulo.api.routes.library.update_library_primitive",
            new_callable=AsyncMock,
            return_value=prim,
        ),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.patch(
            f"/api/v1/libraries/{prim.id}",
            json={"content_json": {"nodes": [dict(_COMPOSITE_SECRET_NODE)], "edges": []}},
        )

    assert resp.status_code == 200, resp.text
    _assert_masked_composite_body(resp.json())


def test_read_surface_delete_response_masks_composite(client: TestClient) -> None:
    prim = _make_composite_primitive()
    with patch(
        "modulo.api.routes.library.soft_delete_library_primitive",
        new_callable=AsyncMock,
        return_value=prim,
    ):
        resp = client.delete(f"/api/v1/libraries/{prim.id}")

    assert resp.status_code == 200, resp.text
    _assert_masked_composite_body(resp.json())


def test_read_surface_restore_response_masks_composite(client: TestClient) -> None:
    prim = _make_composite_primitive()
    with patch(
        "modulo.api.routes.library.restore_library_primitive",
        new_callable=AsyncMock,
        return_value=prim,
    ):
        resp = client.post(f"/api/v1/libraries/{prim.id}/restore")

    assert resp.status_code == 200, resp.text
    _assert_masked_composite_body(resp.json())


def test_read_surface_adapt_response_masks_composite(client: TestClient) -> None:
    prim = _make_composite_primitive()
    with (
        patch(
            "modulo.api.routes.library.copy_to_adapt",
            new_callable=AsyncMock,
            return_value=prim,
        ),
        patch("modulo.api.routes.library.validate_owner_team_for_create", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(f"/api/v1/libraries/{prim.id}/adapt", json={})

    assert resp.status_code == 200, resp.text
    _assert_masked_composite_body(resp.json())


def test_read_surface_community_contribute_response_masks_composite(client: TestClient) -> None:
    prim = _make_composite_primitive()
    with patch(
        "modulo.api.routes.library.contribute_primitive",
        new_callable=AsyncMock,
        return_value=prim,
    ):
        resp = client.post(
            "/api/v1/libraries/community/contribute",
            json={
                "primitive_type": "composite",
                "name": prim.name,
                "slug": prim.slug,
                "content_json": {"nodes": [dict(_COMPOSITE_SECRET_NODE)], "edges": []},
            },
        )

    assert resp.status_code == 201, resp.text
    _assert_masked_composite_body(resp.json())


def test_read_surface_community_contributions_list_masks_composite(client: TestClient) -> None:
    """GET /libraries/community/contributions — list_org_contributions response."""
    prim = _make_composite_primitive()
    page = PageResult(items=[prim], total=1, page=1, page_size=20)
    with patch(
        "modulo.api.routes.library.list_org_contributions",
        new_callable=AsyncMock,
        return_value=page,
    ):
        resp = client.get("/api/v1/libraries/community/contributions")

    assert resp.status_code == 200, resp.text
    _assert_masked_composite_body(resp.json()["items"][0])


def test_read_surface_admin_publish_response_masks_composite(client: TestClient) -> None:
    prim = _make_composite_primitive()
    admin_principal = AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
        is_system_admin=True,
    )
    app.dependency_overrides[get_current_user] = lambda: admin_principal
    with patch(
        "modulo.api.routes.library.publish_contribution",
        new_callable=AsyncMock,
        return_value=prim,
    ):
        resp = client.post(f"/api/v1/libraries/admin/library/community/publish/{prim.id}")

    assert resp.status_code == 200, resp.text
    _assert_masked_composite_body(resp.json())


def test_read_surface_community_install_response_masks_composite(client: TestClient) -> None:
    prim = _make_composite_primitive()
    prim.source = "registry"
    with (
        patch(
            "modulo.api.routes.community_library.install_community_entry",
            new_callable=AsyncMock,
            return_value=prim,
        ),
        patch("modulo.api.routes.community_library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.community_library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(
            f"/api/v1/libraries/community/{uuid.uuid4()}/install",
            json={},
        )

    assert resp.status_code == 201, resp.text
    _assert_masked_composite_body(resp.json())


def test_read_surface_contributions_list_masks_composite(client: TestClient) -> None:
    """GET /api/v1/library/contribute — the fixture-contribution list surface."""
    prim = _make_composite_primitive()
    page = PageResult(items=[prim], total=1, page=1, page_size=20)
    with (
        patch(
            "modulo.api.routes.contributions.list_contributions",
            new_callable=AsyncMock,
            return_value=page,
        ),
        patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.contributions.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get("/api/v1/library/contribute")

    assert resp.status_code == 200, resp.text
    _assert_masked_composite_body(resp.json()["items"][0])


# ---------------------------------------------------------------------------
# Write side — proposed mask sentinels are refused fail-closed (422)
# ---------------------------------------------------------------------------


def test_write_create_composite_rejects_fresh_mask_sentinel(client: TestClient) -> None:
    """POST /libraries composite with a sentinel env value → 422, nothing stored."""
    sentinel_node = {
        "id": "n1",
        "env_vars": {"LIB_TOKEN": SENSITIVE_VALUE_MASK, "APP_URL": "https://example.com"},
    }
    with (
        patch(
            "modulo.api.routes.library.get_primitive_by_slug",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "modulo.api.routes.library.create_library_primitive",
            new_callable=AsyncMock,
        ) as create_mock,
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(
            "/api/v1/libraries",
            json={
                "primitive_type": "composite",
                "name": "Echo Composite",
                "slug": "echo-composite",
                "content_json": {"nodes": [sentinel_node], "edges": []},
            },
        )

    assert resp.status_code == 422, resp.text
    # The shipped issue code names the refusal — fail closed, never persist.
    assert "COMPOSITE_SUBGRAPH_MASKED_CREDENTIAL" in resp.json()["detail"]
    assert "LIB_TOKEN" in resp.json()["detail"]
    create_mock.assert_not_awaited()


def test_write_create_composite_stores_declared_values_as_is(client: TestClient) -> None:
    """An echo-free composite create is UNCHANGED — values pass through as-is."""
    prim = _make_composite_primitive()
    with (
        patch(
            "modulo.api.routes.library.get_primitive_by_slug",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "modulo.api.routes.library.create_library_primitive",
            new_callable=AsyncMock,
            return_value=prim,
        ) as create_mock,
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(
            "/api/v1/libraries",
            json={
                "primitive_type": "composite",
                "name": prim.name,
                "slug": prim.slug,
                "content_json": {"nodes": [dict(_COMPOSITE_SECRET_NODE)], "edges": []},
            },
        )

    assert resp.status_code == 201, resp.text
    write_content = create_mock.await_args.kwargs["content_json"]
    # Real declared values are stored AS-IS (parity with the pipeline write) —
    # no masking on the write side, only the sentinel refusal.
    assert write_content["nodes"][0]["env_vars"]["GITHUB_TOKEN"] == _GHP_SECRET
    assert not write_content["edges"]


def test_write_patch_composite_resolves_mask_echo_against_stored(client: TestClient) -> None:
    """PATCH round-tripping the masked GET: the echo restores the stored value."""
    stored = _make_composite_primitive()
    echo_node = dict(_COMPOSITE_SECRET_NODE)
    echo_node["env_vars"] = {"GITHUB_TOKEN": SENSITIVE_VALUE_MASK, "APP_URL": "https://example.com"}
    with (
        patch(
            "modulo.api.routes.library.get_library_primitive",
            new_callable=AsyncMock,
            return_value=stored,
        ),
        patch(
            "modulo.api.routes.library.update_library_primitive",
            new_callable=AsyncMock,
            return_value=stored,
        ) as update_mock,
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.patch(
            f"/api/v1/libraries/{stored.id}",
            json={"content_json": {"nodes": [echo_node], "edges": []}},
        )

    assert resp.status_code == 200, resp.text
    updates = update_mock.await_args.args[2]
    resolved_nodes = updates["content_json"]["nodes"]
    # The echo was resolved to the REAL stored credential before persisting.
    assert resolved_nodes[0]["env_vars"]["GITHUB_TOKEN"] == _GHP_SECRET
    assert resolved_nodes[0]["env_vars"]["APP_URL"] == "https://example.com"
    # And the RESPONSE re-masks — the caller never re-sees the raw value.
    assert resp.json()["content_json"]["nodes"][0]["env_vars"]["GITHUB_TOKEN"] == SENSITIVE_VALUE_MASK


def test_write_patch_composite_rejects_fresh_mask_sentinel(client: TestClient) -> None:
    """A PATCH with a sentinel the stored graph cannot resolve → 422, no write."""
    stored = _make_composite_primitive()
    fresh_sentinel_node = {"id": "n2", "env_vars": {"NEW_TOKEN": SENSITIVE_VALUE_MASK}}
    with (
        patch(
            "modulo.api.routes.library.get_library_primitive",
            new_callable=AsyncMock,
            return_value=stored,
        ),
        patch(
            "modulo.api.routes.library.update_library_primitive",
            new_callable=AsyncMock,
        ) as update_mock,
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.patch(
            f"/api/v1/libraries/{stored.id}",
            json={"content_json": {"nodes": [fresh_sentinel_node], "edges": []}},
        )

    assert resp.status_code == 422, resp.text
    assert "COMPOSITE_SUBGRAPH_MASKED_CREDENTIAL" in resp.json()["detail"]
    update_mock.assert_not_awaited()


def test_write_patch_composite_echoless_deletion_stays_removed(client: TestClient) -> None:
    """Full-replace semantics: a node field the caller REMOVED stays removed."""
    stored = _make_composite_primitive()
    emptied_node = dict(_COMPOSITE_SECRET_NODE)
    emptied_node["env_vars"] = {"APP_URL": "https://example.com"}
    with (
        patch(
            "modulo.api.routes.library.get_library_primitive",
            new_callable=AsyncMock,
            return_value=stored,
        ),
        patch(
            "modulo.api.routes.library.update_library_primitive",
            new_callable=AsyncMock,
            return_value=stored,
        ) as update_mock,
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.patch(
            f"/api/v1/libraries/{stored.id}",
            json={"content_json": {"nodes": [emptied_node], "edges": []}},
        )

    assert resp.status_code == 200, resp.text
    updates = update_mock.await_args.args[2]
    assert "GITHUB_TOKEN" not in updates["content_json"]["nodes"][0]["env_vars"]


def test_write_community_contribute_composite_rejects_fresh_mask_sentinel(client: TestClient) -> None:
    """The community contribute composite write follows the same FAR-1374 gate."""
    sentinel_node = {"id": "n1", "env_vars": {"LIB_TOKEN": SENSITIVE_VALUE_MASK}}
    with (
        patch(
            "modulo.api.routes.library.contribute_primitive",
            new_callable=AsyncMock,
        ) as contribute_mock,
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post(
            "/api/v1/libraries/community/contribute",
            json={
                "primitive_type": "composite",
                "name": "Echo Composite",
                "slug": "echo-composite",
                "content_json": {"nodes": [sentinel_node], "edges": []},
            },
        )

    assert resp.status_code == 422, resp.text
    assert "COMPOSITE_SUBGRAPH_MASKED_CREDENTIAL" in resp.json()["detail"]
    contribute_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Chained masked-read -> write round-trip on the LIBRARY surface (FAR-1380)
#
# The write-side tests above hand-build the echo they feed the PATCH. This one
# chains a REAL library composite read (route-level masking via the validator)
# into the REAL PATCH — the body is the GET response's content_json VERBATIM —
# so a regression on either side (masking lost on read, or the mask literal
# persisted over the stored credential on write) fails here.
# ---------------------------------------------------------------------------


def test_masked_library_read_to_patch_round_trip_preserves_stored_secret(client: TestClient) -> None:
    stored = _make_composite_primitive()

    with (
        patch(
            "modulo.api.routes.library.get_primitive",
            new_callable=AsyncMock,
            return_value=stored,
        ),
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        read_resp = client.get(f"/api/v1/libraries/{stored.id}")

    assert read_resp.status_code == 200
    read_body = read_resp.json()
    # The real read path is masked — this documents the echo flowing in.
    assert read_body["content_json"]["nodes"][0]["env_vars"]["GITHUB_TOKEN"] == SENSITIVE_VALUE_MASK
    assert read_body["content_json"]["nodes"][0]["env_vars"]["APP_URL"] == "https://example.com"

    # Write that content_json VERBATIM through the real PATCH surface.
    with (
        patch(
            "modulo.api.routes.library.get_library_primitive",
            new_callable=AsyncMock,
            return_value=stored,
        ),
        patch(
            "modulo.api.routes.library.update_library_primitive",
            new_callable=AsyncMock,
            return_value=stored,
        ) as update_mock,
        patch("modulo.api.routes.library.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.library.set_rls_user_context", new_callable=AsyncMock),
    ):
        write_resp = client.patch(
            f"/api/v1/libraries/{stored.id}",
            json={"content_json": read_body["content_json"]},
        )

    assert write_resp.status_code == 200, write_resp.text
    updates = update_mock.await_args.args[2]
    written_nodes = updates["content_json"]["nodes"]
    # The sentinel was resolved to the REAL stored credential before persistence.
    assert written_nodes[0]["env_vars"]["GITHUB_TOKEN"] == _GHP_SECRET
    assert written_nodes[0]["env_vars"]["APP_URL"] == "https://example.com"
    # Belt-and-braces: no mask literal anywhere in the persisted payload.
    assert SENSITIVE_VALUE_MASK not in json.dumps(written_nodes, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Defensive branches: malformed payloads never fail open
# ---------------------------------------------------------------------------


def test_response_validator_passes_non_list_nodes_shape_through() -> None:
    """A composite whose 'nodes' is not a list must not crash the serializer.

    The validator passes it through untouched — the same pass-through the
    composite routes apply before this control existed; structural rejection
    remains the boundary validators' job (_assert_composite_content_json).
    """
    from modulo.api.routes.library import LibraryPrimitiveResponse

    prim = _make_composite_primitive()
    prim.content_json = {"nodes": "not-a-list", "edges": []}

    resp = LibraryPrimitiveResponse.model_validate(prim)

    assert resp.content_json["nodes"] == "not-a-list"


async def test_mcp_library_detail_resource_masks_composite_content() -> None:
    import modulo.api.mcp_server as ms

    ms._ctx_org_id.set(_ORG_ID)
    mock_session: Any = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    try:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=session_cm),
            # mcp_server imports get_primitive_by_slug into its OWN namespace.
            patch(
                "modulo.api.mcp_server.get_primitive_by_slug",
                new_callable=AsyncMock,
                return_value=_make_composite_primitive(),
            ) as get_by_slug_mock,
        ):
            result = await ms.resource_library_detail("composite", "sub-flow")
    finally:
        ms._ctx_org_id.set(None)

    get_by_slug_mock.assert_awaited_once()
    assert _GHP_SECRET not in result
    assert _STRIPE_SECRET not in result
    assert "hunter2-secret" not in result
    # Non-secret env value still renders so agents keep their working context.
    assert "https://example.com" in result
    # The mask literal survives json.dumps() unicode-escaping in the summary.
    assert json.dumps(SENSITIVE_VALUE_MASK)[1:-1] in result


async def test_mcp_library_detail_resource_leaves_non_composite_content_raw() -> None:
    """A non-composite primitive resource keeps its content byte-identical."""
    import modulo.api.mcp_server as ms

    ms._ctx_org_id.set(_ORG_ID)
    mock_session: Any = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    prim = _make_composite_primitive()
    prim.primitive_type = "workflow"
    try:
        with (
            patch.object(ms, "validate_current_auth", new=AsyncMock(return_value=True)),
            patch.object(ms, "_session", return_value=session_cm),
            patch(
                "modulo.api.mcp_server.get_primitive_by_slug",
                new_callable=AsyncMock,
                return_value=prim,
            ),
        ):
            result = await ms.resource_library_detail("workflow", "sub-flow")
    finally:
        ms._ctx_org_id.set(None)

    # Non-composite content is NOT masked by the library surface.
    assert _GHP_SECRET in result
