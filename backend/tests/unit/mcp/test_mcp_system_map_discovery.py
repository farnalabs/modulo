"""Unit tests for the System Maps discovery MCP tool (FAR-2288).

The tool is deliberately a thin transport wrapper: it drives the SAME
``POST /api/v1/system-maps/discover`` handler the REST surface uses, so the
inventory read, inference (``discover_draft``) and persistence
(``SystemMapDiscoveryRepository``) are one implementation. These tests pin:

* the four internal-gate legs (role, grant, developer mode, ``system_maps``
  feature) each refuse on their own, before any discovery work;
* tenant wiring (the principal the handler receives is the authenticated one);
* the shared-service proof — the MCP path and the REST path persist the exact
  same draft content for the same organisation inventory.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, InvalidRequestError, ProgrammingError
from starlette.exceptions import HTTPException as StarletteHTTPException

from farna.api.db_error_handling import MSG_SESSION_CONTRACT
from farna.api.dependencies import _get_engine, get_db_session, get_plan_context, get_settings
from farna.api.main import app
from farna.api.mcp_server import (
    _MSG_DB_MIGRATION_REQUIRED,
    _MSG_UNEXPECTED_ERROR,
    _system_maps_discovery_session,
    _system_maps_http_error_payload,
    discover_system_map,
)
from farna.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from farna.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from farna.core.system_map_persistence import SystemMapDiscoveryRepository, SystemMapNameConflictError
from farna.settings import Settings
from tests.unit.api.mock_session import configure_mock_session
from tests.unit.mcp.helpers import ORG_ID, USER_ID

_MAP_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_REVISION_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_TRIGGER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
_CONNECTOR_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b3")
_FOLDER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b4")

_VALID_32 = "a" * 32


def _make_settings(*, dev_mode: bool = True) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_dev_mode=dev_mode,
    )


def _draft_map() -> SimpleNamespace:
    return SimpleNamespace(id=_MAP_ID)


def _inventory_rows() -> dict[str, list[SimpleNamespace]]:
    """One pipeline, trigger, connector and folder — the confirmed edge class."""
    pipeline = SimpleNamespace(
        id=_PIPELINE_ID,
        name="build",
        folder_id=_FOLDER_ID,
        run_context_defaults={},
        organisation_id=ORG_ID,
        deleted_at=None,
        archived_at=None,
    )
    trigger = SimpleNamespace(
        id=_TRIGGER_ID,
        name="webhook",
        pipeline_id=_PIPELINE_ID,
        trigger_type="webhook",
        organisation_id=ORG_ID,
        deleted_at=None,
    )
    connector = SimpleNamespace(
        id=_CONNECTOR_ID,
        name="github",
        connector_type_id="github",
        organisation_id=ORG_ID,
        deleted_at=None,
    )
    folder = SimpleNamespace(id=_FOLDER_ID, name="Build Folder", organisation_id=ORG_ID)
    return {
        "FROM pipelines": [pipeline],
        "FROM triggers": [trigger],
        "FROM connector_instances": [connector],
        "FROM pipeline_folders": [folder],
    }


def _discovery_session(rows: dict[str, list[SimpleNamespace]] | None = None) -> AsyncMock:
    """Session double that serves the four inventory SELECTs by table."""
    inventory = rows if rows is not None else _inventory_rows()
    session = configure_mock_session(AsyncMock())

    def _dispatch(statement: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(statement)
        if "organisations.authz_enforce" in text:
            # require_permission's kill-switch read on the REST path.
            result = MagicMock()
            result.scalar_one_or_none = MagicMock(return_value=True)
            return result
        for snippet, table_rows in inventory.items():
            if snippet in text:
                result = MagicMock()
                result.scalars = MagicMock(return_value=list(table_rows))
                return result
        raise AssertionError(f"unexpected discovery query: {text}")

    session.execute = AsyncMock(side_effect=_dispatch)
    return session


def _patch_discovery_session(session: AsyncMock) -> Any:
    """Patch the tool's session factory with an async context manager."""

    @asynccontextmanager
    async def _cm() -> AsyncGenerator[AsyncMock, None]:
        yield session

    return patch("farna.api.mcp_server._system_maps_discovery_session", return_value=_cm())


@pytest.fixture(autouse=True)
def _mcp_auth_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Operator caller with the System Maps grant, as an org-scoped API key."""
    from farna.api.mcp_server import (
        _ctx_auth_token,
        _ctx_auth_type,
        _ctx_key_grants,
        _ctx_key_scope,
        _ctx_org_id,
        _ctx_role,
        _ctx_team_id,
        _ctx_user_id,
    )

    # Developer mode is part of the internal gate; the production default is
    # OFF, so an authorised scenario must opt in explicitly.
    monkeypatch.setattr("farna.api.mcp_server.get_settings", lambda: _make_settings())
    _ctx_org_id.set(ORG_ID)
    _ctx_user_id.set(USER_ID)
    _ctx_role.set("operator")
    _ctx_auth_type.set("api_key")
    _ctx_auth_token.set("mk_testprefix_testsecretkey1234567890abc")
    _ctx_key_grants.set(frozenset({"system_maps_access"}))
    _ctx_key_scope.set("org")
    _ctx_team_id.set(None)
    yield
    _ctx_org_id.set(None)
    _ctx_user_id.set(None)
    _ctx_role.set(None)
    _ctx_auth_type.set(None)
    _ctx_auth_token.set(None)
    _ctx_key_grants.set(None)
    _ctx_key_scope.set(None)
    _ctx_team_id.set(None)


@pytest.mark.asyncio
async def test_authorized_caller_gets_unpublished_draft_report() -> None:
    """Operator + grant + dev mode + feature reaches inference and persistence."""
    session = _discovery_session()
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)),
        patch.object(SystemMapDiscoveryRepository, "get_or_create_draft", new=AsyncMock(return_value=_draft_map())),
        patch.object(
            SystemMapDiscoveryRepository,
            "save_discovery_revision",
            new=AsyncMock(return_value=(_draft_map(), _REVISION_ID, 1)),
        ),
        _patch_discovery_session(session),
    ):
        result = await discover_system_map(name="draft", description="internal")

    assert result["published"] is False
    assert result["draft_map_id"] == str(_MAP_ID)
    assert result["draft_revision_id"] == str(_REVISION_ID)
    assert result["node_count"] == 4
    assert result["edge_count"] == 2
    relationship_types = {edge["relationship_type"] for edge in result["inferred_edges"]}
    assert relationship_types == {"trigger", "flow"}
    assert all(edge["config_json"]["suggested"] is True for edge in result["inferred_edges"])
    kinds = {source["kind"] for source in result["sources_inspected"]}
    assert kinds == {"pipeline", "trigger", "connector", "folder"}


@pytest.mark.asyncio
async def test_viewer_role_is_refused() -> None:
    """An ordinary (viewer) caller is refused and no discovery work runs."""
    from farna.api.mcp_server import _ctx_role

    _ctx_role.set("viewer")
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)) as feature_gate,
        patch("farna.api.routes.system_maps.discover_system_map", new=AsyncMock()) as rest_handler,
    ):
        result = await discover_system_map()

    assert result["error"] == "insufficient_scope"
    feature_gate.assert_not_awaited()
    rest_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_grant_set_without_system_maps_access_is_refused() -> None:
    """A grant-bearing credential must carry the dedicated grant."""
    from farna.api.mcp_server import _ctx_key_grants

    _ctx_key_grants.set(frozenset({"pipeline.list"}))
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)) as feature_gate,
        patch("farna.api.routes.system_maps.discover_system_map", new=AsyncMock()) as rest_handler,
    ):
        result = await discover_system_map()

    assert result["error"] == "insufficient_scope"
    feature_gate.assert_not_awaited()
    rest_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_developer_mode_off_is_refused() -> None:
    """The dev-mode leg refuses even an operator holding the grant."""
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server.get_settings", return_value=_make_settings(dev_mode=False)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)) as feature_gate,
        patch("farna.api.routes.system_maps.discover_system_map", new=AsyncMock()) as rest_handler,
    ):
        result = await discover_system_map()

    assert result["error"] == "insufficient_scope"
    assert "restricted to internal operators" in result["detail"]
    feature_gate.assert_not_awaited()
    rest_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_system_maps_feature_off_is_refused_before_discovery() -> None:
    """The feature-flag leg (REST ``require_feature``) refuses with a branchable code."""
    feature_error = {"error": "feature_required", "detail": "system_maps is not available on your plan"}
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=feature_error)) as feature_gate,
        patch("farna.api.routes.system_maps.discover_system_map", new=AsyncMock()) as rest_handler,
    ):
        result = await discover_system_map()

    assert result == feature_error
    feature_gate.assert_awaited_once()
    rest_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoked_token_returns_auth_expired() -> None:
    with patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=False)):
        result = await discover_system_map()

    assert result["error"] == "auth_expired"


@pytest.mark.asyncio
async def test_empty_name_is_a_validation_error() -> None:
    """Input is validated before any session or discovery work is opened."""
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)),
        patch("farna.api.routes.system_maps.discover_system_map", new=AsyncMock()) as rest_handler,
    ):
        result = await discover_system_map(name="")

    assert result["error"] == "validation_failed"
    rest_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_options_are_forwarded_and_serialized() -> None:
    backend_id = uuid.uuid4()
    eval_id = uuid.uuid4()
    rest_handler = AsyncMock(
        return_value=SimpleNamespace(
            model_dump=MagicMock(
                return_value={
                    "ai_assisted": True,
                    "provenance": {"selected_backend_id": str(backend_id)},
                    "eval_gate_verdict": "pass",
                    "published": False,
                }
            )
        )
    )
    session = _discovery_session()
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)),
        patch("farna.api.routes.system_maps.discover_system_map", new=rest_handler),
        _patch_discovery_session(session),
    ):
        result = await discover_system_map(model_backend_id=backend_id, eval_definition_id=eval_id)

    assert result["ai_assisted"] is True
    assert result["provenance"]["selected_backend_id"] == str(backend_id)
    assert result["eval_gate_verdict"] == "pass"
    call_request = rest_handler.await_args.kwargs["request"]
    assert call_request.model_backend_id == backend_id
    assert call_request.eval_definition_id == eval_id


@pytest.mark.asyncio
async def test_discovery_uses_the_authenticated_tenant() -> None:
    """The reused handler receives the MCP caller's org/account/role, not a default."""
    rest_handler = AsyncMock(return_value=SimpleNamespace(model_dump=MagicMock(return_value={"ok": True})))
    from farna.api.mcp_server import _ctx_role

    _ctx_role.set("admin")
    session = _discovery_session()
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)),
        patch("farna.api.routes.system_maps.discover_system_map", new=rest_handler),
        _patch_discovery_session(session),
    ):
        result = await discover_system_map(name="scoped")

    assert result == {"ok": True}
    call_kwargs = rest_handler.await_args.kwargs
    principal = call_kwargs["principal"]
    assert principal.organisation_id == ORG_ID
    assert principal.account_id == USER_ID
    assert principal.org_role == "admin"
    assert call_kwargs["request"].name == "scoped"


@pytest.mark.asyncio
async def test_discovery_queries_are_tenant_scoped() -> None:
    """Inventory reads filter the caller's org and the RLS context is bound to it."""
    session = _discovery_session()
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)),
        patch.object(SystemMapDiscoveryRepository, "get_or_create_draft", new=AsyncMock(return_value=_draft_map())),
        patch.object(
            SystemMapDiscoveryRepository,
            "save_discovery_revision",
            new=AsyncMock(return_value=(_draft_map(), _REVISION_ID, 1)),
        ),
        _patch_discovery_session(session),
    ):
        result = await discover_system_map()

    assert "error" not in result
    assert session.info["org_id"] == ORG_ID
    assert session.info["user_id"] == USER_ID
    statements = [call.args[0] for call in session.execute.await_args_list]
    inventory_tables = ("FROM pipelines", "FROM triggers", "FROM connector_instances", "FROM pipeline_folders")
    inventory_selects = [statement for statement in statements if any(t in str(statement) for t in inventory_tables)]
    assert len(inventory_selects) == 4
    for statement in inventory_selects:
        assert "organisation_id" in str(statement)


# ─── Shared-service proof: MCP and REST persist the same draft ───


class _Plan:
    """Plan context enabling only ``system_maps``."""

    def feature_enabled(self, name: str) -> bool:
        return name == "system_maps"

    def list_enabled_features(self) -> list[str]:
        return ["system_maps"]

    def tier(self) -> str:
        return "community"

    def has_license_key(self) -> bool:
        return False


@pytest.fixture
def rest_client() -> Generator[TestClient, None, None]:
    """REST client wired to the same internal-gate context as the MCP caller."""
    session = _discovery_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    principal = TenantPrincipal(
        username="internal",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role="operator",
        key_grants=frozenset({"system_maps_access"}),
    )
    auth_user = AuthenticatedPrincipal(
        username="internal",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role="operator",
        key_grants=frozenset({"system_maps_access"}),
    )

    app.dependency_overrides[get_settings] = lambda: _make_settings()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = lambda: _Plan()
    app.dependency_overrides[get_current_user] = lambda: auth_user
    app.dependency_overrides[get_current_tenant_user] = lambda: principal
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: principal
    try:
        client = TestClient(app)
        client.cookies.set("XSRF-TOKEN", "test-csrf-token")
        client.headers["X-CSRF-Token"] = "test-csrf-token"
        yield client
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_mcp_and_rest_paths_persist_the_same_draft(rest_client: TestClient) -> None:
    """Both surfaces drive one discovery implementation for the same inventory."""
    persisted: list[dict[str, Any]] = []

    async def _save(*_args: object, **kwargs: Any) -> tuple[SimpleNamespace, uuid.UUID, int]:
        persisted.append(kwargs["content"])
        return _draft_map(), _REVISION_ID, 1

    with (
        patch.object(SystemMapDiscoveryRepository, "get_or_create_draft", new=AsyncMock(return_value=_draft_map())),
        patch.object(SystemMapDiscoveryRepository, "save_discovery_revision", new=AsyncMock(side_effect=_save)),
    ):
        rest_response = rest_client.post("/api/v1/system-maps/discover", json={"name": "same-draft"})
        assert rest_response.status_code == 201
        rest_report = rest_response.json()

        mcp_session = _discovery_session()
        with (
            patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)),
            _patch_discovery_session(mcp_session),
        ):
            mcp_report = await discover_system_map(name="same-draft")

    assert len(persisted) == 2
    rest_content, mcp_content = persisted
    assert mcp_content == rest_content
    assert mcp_report["draft_map_id"] == rest_report["draft_map_id"]
    assert mcp_report["draft_revision_id"] == rest_report["draft_revision_id"]
    assert mcp_report["inferred_nodes"] == rest_report["inferred_nodes"]
    assert mcp_report["inferred_edges"] == rest_report["inferred_edges"]
    assert mcp_report["skipped"] == rest_report["skipped"]
    assert mcp_report["warnings"] == rest_report["warnings"]


# ─── Error mapping: the tool classifies DB failures itself (FAR-2288 fix) ───
#
# The reused REST handler is wrapped by ``@handle_db_errors``, which would turn
# a session-contract violation into a generic 500 and an ``IntegrityError``
# into 409/``name_conflict`` before the tool sees it. The tool resolves the
# UNDECORATED handler, so these tests drive the real route function and assert
# the branchable codes the agent receives.


async def _call_tool_with_rest_handler_error(exc: BaseException) -> dict[str, Any]:
    """Drive the tool with a mocked route handler that raises *exc*."""
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)),
        patch("farna.api.routes.system_maps.discover_system_map", new=AsyncMock(side_effect=exc)),
        _patch_discovery_session(_discovery_session()),
    ):
        return await discover_system_map()


@pytest.mark.parametrize(
    ("status_code", "detail", "expected_code"),
    [
        (400, "bad request", "invalid_request"),
        (402, "system_maps is not available on your plan", "feature_required"),
        (403, "not permitted", "insufficient_scope"),
        (404, "System map not found", "not_found"),
        (409, "A system map named 'x' already exists", "name_conflict"),
        (422, "name is invalid", "validation_failed"),
        (501, "migrations required", "migration_required"),
        (503, "database unavailable", "database_unavailable"),
        (418, "unmapped refusal", "server_error"),
    ],
    ids=[
        "400-bad-request",
        "402-feature-required",
        "403-insufficient-scope",
        "404-not-found",
        "409-name-conflict",
        "422-validation-failed",
        "501-migration-required",
        "503-database-unavailable",
        "418-unmapped-server-error",
    ],
)
@pytest.mark.asyncio
async def test_route_http_refusal_maps_to_its_branchable_code(
    status_code: int,
    detail: str,
    expected_code: str,
) -> None:
    result = await _call_tool_with_rest_handler_error(StarletteHTTPException(status_code=status_code, detail=detail))

    assert result == {"error": expected_code, "detail": detail}


@pytest.mark.parametrize(
    ("status_code", "detail", "expected_code"),
    [
        (400, "bad request", "invalid_request"),
        (402, "feature required", "feature_required"),
        (404, "not found", "not_found"),
        (422, "invalid", "validation_failed"),
        (503, "unavailable", "database_unavailable"),
        (418, "unmapped", "server_error"),
    ],
    ids=[
        "direct-400",
        "direct-402",
        "direct-404",
        "direct-422",
        "direct-503",
        "direct-418",
    ],
)
def test_http_error_payload_maps_statuses_directly(
    status_code: int,
    detail: str,
    expected_code: str,
) -> None:
    """The envelope helper is unit-testable without the tool's gate stack."""
    payload = _system_maps_http_error_payload(StarletteHTTPException(status_code=status_code, detail=detail))

    assert payload == {"error": expected_code, "detail": detail}


@pytest.mark.parametrize(
    ("code", "status_code", "message", "extra"),
    [
        (
            "model_backend_unauthorised",
            403,
            "Selected model backend is not active and authorised for this organisation",
            {},
        ),
        ("eval_definition_unauthorised", 403, "Eval gate is not authorised for this caller", {}),
        ("eval_gate_failed", 422, "AI-assisted discovery did not pass its evaluation gate", {"verdict": "fail"}),
    ],
)
def test_ai_discovery_errors_preserve_safe_structured_codes(
    code: str,
    status_code: int,
    message: str,
    extra: dict[str, str],
) -> None:
    payload = _system_maps_http_error_payload(StarletteHTTPException(status_code=status_code, detail={"code": code, "message": message, **extra}))
    expected = {"error": code, "detail": message, **extra}
    assert payload == expected


def test_http_error_payload_never_echoes_a_structured_detail() -> None:
    """A non-string detail is replaced by the safe constant."""
    payload = _system_maps_http_error_payload(StarletteHTTPException(status_code=404, detail={"internal": "do-not-leak"}))

    assert payload == {"error": "not_found", "detail": _MSG_UNEXPECTED_ERROR}
    assert "do-not-leak" not in payload["detail"]


@pytest.mark.asyncio
async def test_discovery_session_yields_the_factory_session() -> None:
    """The helper yields the factory's own session, never a nested transaction."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)

    with patch("farna.api.mcp_server._get_session_factory", return_value=factory):
        async with _system_maps_discovery_session() as yielded:
            assert yielded is session

    factory.assert_called_once_with()
    session.__aenter__.assert_awaited_once()
    session.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_programming_error_maps_to_migration_required() -> None:
    result = await _call_tool_with_rest_handler_error(ProgrammingError("SELECT 1", {}, Exception("column does not exist")))

    assert result["error"] == "migration_required"
    assert result["detail"] == _MSG_DB_MIGRATION_REQUIRED
    assert "column does not exist" not in result["detail"]


@pytest.mark.asyncio
async def test_real_route_session_contract_error_keeps_its_own_code() -> None:
    """Without the unwrap this is a generic 500 -> ``server_error``."""
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)),
        patch(
            "farna.api.routes.system_maps.set_rls_org",
            new=AsyncMock(side_effect=InvalidRequestError("session outside transaction")),
        ),
        _patch_discovery_session(_discovery_session()),
    ):
        result = await discover_system_map()

    assert result["error"] == "session_contract_error"
    assert result["detail"] == MSG_SESSION_CONTRACT
    assert "session outside transaction" not in result["detail"]


@pytest.mark.asyncio
async def test_real_route_uuid5_revision_collision_is_a_retryable_conflict() -> None:
    """A concurrent same-fingerprint revision write is NOT a name clash.

    Without the unwrap ``@handle_db_errors`` turns this IntegrityError into a
    409, which the tool rendered as ``name_conflict`` — steering the agent to
    rename a map instead of retrying the idempotent write.
    """
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)),
        patch.object(SystemMapDiscoveryRepository, "get_or_create_draft", new=AsyncMock(return_value=_draft_map())),
        patch.object(
            SystemMapDiscoveryRepository,
            "save_discovery_revision",
            new=AsyncMock(
                side_effect=IntegrityError(
                    "INSERT INTO system_map_revisions ...",
                    {},
                    Exception("duplicate key value violates unique constraint"),
                )
            ),
        ),
        _patch_discovery_session(_discovery_session()),
    ):
        result = await discover_system_map()

    assert result["error"] == "conflict"
    assert result["error"] != "name_conflict"
    assert "duplicate key" not in result["detail"]


@pytest.mark.asyncio
async def test_real_route_name_conflict_still_maps_to_name_conflict() -> None:
    """A genuine name clash keeps its distinct code."""
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)),
        patch.object(
            SystemMapDiscoveryRepository,
            "get_or_create_draft",
            new=AsyncMock(side_effect=SystemMapNameConflictError("A system map named 'draft' already exists")),
        ),
        _patch_discovery_session(_discovery_session()),
    ):
        result = await discover_system_map(name="draft")

    assert result["error"] == "name_conflict"
    assert "already exists" in result["detail"]


@pytest.mark.asyncio
async def test_real_route_value_error_is_a_non_leaking_server_error() -> None:
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)),
        patch.object(SystemMapDiscoveryRepository, "get_or_create_draft", new=AsyncMock(return_value=_draft_map())),
        patch.object(
            SystemMapDiscoveryRepository,
            "save_discovery_revision",
            new=AsyncMock(side_effect=ValueError("system map discovery draft not found")),
        ),
        _patch_discovery_session(_discovery_session()),
    ):
        result = await discover_system_map()

    assert result["error"] == "server_error"
    assert result["detail"] == "Failed to discover system map"
    assert "draft not found" not in result["detail"]


# ─── Credential scope: an org-wide scan requires an org-scoped credential ───


@pytest.mark.asyncio
async def test_team_scoped_credential_is_refused_before_discovery() -> None:
    """A team-scoped key must not trigger an org-wide inventory scan."""
    from farna.api.mcp_server import _ctx_team_id

    team_id = uuid.uuid4()
    _ctx_team_id.set(team_id)
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)) as feature_gate,
        patch("farna.api.routes.system_maps.discover_system_map", new=AsyncMock()) as rest_handler,
    ):
        result = await discover_system_map()

    assert result["error"] == "insufficient_scope"
    assert "org-scoped credential" in result["detail"]
    assert str(team_id) not in result["detail"]
    feature_gate.assert_not_awaited()
    rest_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_scoped_api_key_is_refused_before_discovery() -> None:
    from farna.api.mcp_server import _ctx_key_scope

    _ctx_key_scope.set("user")
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)) as feature_gate,
        patch("farna.api.routes.system_maps.discover_system_map", new=AsyncMock()) as rest_handler,
    ):
        result = await discover_system_map()

    assert result["error"] == "insufficient_scope"
    assert "org-scoped credential" in result["detail"]
    feature_gate.assert_not_awaited()
    rest_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_team_less_identity_session_is_allowed() -> None:
    """A JWT session is the user's own org-wide identity — not a team key."""
    from farna.api.mcp_server import _ctx_auth_type, _ctx_key_scope

    _ctx_key_scope.set("user")
    _ctx_auth_type.set("jwt")
    rest_handler = AsyncMock(return_value=SimpleNamespace(model_dump=MagicMock(return_value={"ok": True})))
    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)),
        patch("farna.api.routes.system_maps.discover_system_map", new=rest_handler),
        _patch_discovery_session(_discovery_session()),
    ):
        result = await discover_system_map()

    assert result == {"ok": True}
    rest_handler.assert_awaited_once()


def test_route_handler_without_a_wrapper_is_used_as_is() -> None:
    """The unwrap helper tolerates a plain (already-undecorated) function."""
    from farna.api.mcp_server import _unwrap_system_maps_route_handler

    async def handler() -> None:
        return None

    assert _unwrap_system_maps_route_handler(handler) is handler


def test_no_system_maps_tools_on_mcp_beyond_discovery() -> None:
    """System Maps MCP exposes discovery and overview, but not authoring.

    Authoring, publish, diff, restore and revision reads remain REST-only.
    """
    from farna.api.mcp_server import mcp
    from farna.core.mcp.scope_validator import TOOL_SCOPE_REQUIREMENTS

    system_map_mcp_tools = {name for name in mcp._tool_manager._tools if "system_map" in name}
    assert system_map_mcp_tools == {"discover_system_map", "get_system_map_overview"}

    forbidden_tool_names = {
        "create_system_map",
        "update_system_map",
        "delete_system_map",
        "get_system_map",
        "list_system_maps",
        "publish_system_map_revision",
        "archive_system_map_revision",
        "delete_system_map_revision",
        "diff_system_map_revision",
        "restore_system_map_revision",
        "get_system_map_revision",
    }
    for forbidden in forbidden_tool_names:
        assert forbidden not in mcp._tool_manager._tools
        assert forbidden not in TOOL_SCOPE_REQUIREMENTS


@pytest.mark.asyncio
async def test_system_map_overview_delegates_to_rest_handler() -> None:
    from farna.api.mcp_server import get_system_map_overview

    overview = SimpleNamespace(model_dump=MagicMock(return_value={"revision_id": str(_REVISION_ID), "nodes": []}))
    rest_handler = AsyncMock(return_value=overview)
    repository = object()
    session = AsyncMock()

    @asynccontextmanager
    async def session_cm() -> AsyncGenerator[AsyncMock, None]:
        yield session

    with (
        patch("farna.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
        patch("farna.api.mcp_server._require_plan_feature", new=AsyncMock(return_value=None)) as feature_gate,
        patch("farna.api.mcp_server._session", return_value=session_cm()),
        patch("farna.api.mcp_server._check_agent_tool_scope"),
        patch("farna.api.routes.system_maps.get_system_map_overview_endpoint", new=rest_handler),
        patch("farna.api.routes.system_maps.get_repo", new=AsyncMock(return_value=repository)),
    ):
        result = await get_system_map_overview(map_id=_MAP_ID, revision_id=_REVISION_ID, window_days=14)

    assert result == {"revision_id": str(_REVISION_ID), "nodes": []}
    feature_gate.assert_awaited_once()
    assert feature_gate.await_args.args[0] == ORG_ID
    assert feature_gate.await_args.args[2] == "system_maps"
    rest_handler.assert_awaited_once()
    assert rest_handler.await_args.kwargs["repository"] is repository
    assert rest_handler.await_args.kwargs["window_days"] == 14


def test_system_map_overview_is_registered_without_unimplemented_tools() -> None:
    from farna.api.mcp_server import mcp
    from farna.core.mcp.scope_validator import TOOL_SCOPE_REQUIREMENTS

    assert "get_system_map_overview" in mcp._tool_manager._tools
    assert TOOL_SCOPE_REQUIREMENTS["get_system_map_overview"] == "system_map.overview"
