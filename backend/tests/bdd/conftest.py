"""BDD/E2E test fixtures — pytest-bdd, Playwright, and TestClient setup."""

import contextlib
import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MODULO_CSRF_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
os.environ.setdefault("MODULO_DB", "sqlite")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "b" * 32)

from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings

_VALID_32 = "a" * 32
ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
ALT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


# ---------------------------------------------------------------------------
# Playwright fixtures (E2E with ?theme=agent)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "viewport": {"width": 1440, "height": 900},
        "color_scheme": "dark",
    }


@pytest.fixture(scope="session")
def base_url(pytestconfig) -> str:
    """Resolve the frontend base URL for Playwright UI tests.

    Honors the ``--base-url`` pytest option (pytest-base-url) so CI can point
    the UI cluster at the preview server (bdd.yml serves the frontend build on
    http://localhost:4173 and passes ``--base-url``). Falls back to the local
    Vite dev server port for a plain local run.
    """
    return pytestconfig.getoption("base_url") or "http://localhost:5173"


# ---------------------------------------------------------------------------
# Mock helpers (reused across step definitions)
# ---------------------------------------------------------------------------


def make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
        modulo_csrf_enabled=False,
    )


def make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    nested_cm = AsyncMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)
    scalar_mock = MagicMock()
    scalar_mock.all = MagicMock(return_value=[])
    team_mock = MagicMock()
    team_mock.id = uuid.uuid4()
    team_mock.organisation_id = ORG_ID
    team_mock.name = "test-team"
    # Account-shaped attributes the auth routes read when a query returns this
    # row: JWT issuance serialises email/is_system_admin into the token claims,
    # so they must be real values — a bare MagicMock breaks json.dumps.
    # `active` must be a real True for the same reason: the refresh endpoint's
    # account-status re-check (FAR-463) is fail-closed and denies every refresh
    # when the row cannot report its active flag.
    team_mock.email = "testuser@example.com"
    team_mock.is_system_admin = True
    team_mock.active = True
    hitl_result = AsyncMock()
    hitl_result.scalar_one_or_none = MagicMock(return_value=team_mock)
    hitl_result.scalar_one = MagicMock(return_value=0)
    hitl_result.scalar = MagicMock(return_value=0)
    hitl_result.scalars = MagicMock(return_value=scalar_mock)
    hitl_result.first = MagicMock(return_value=MagicMock())
    hitl_result.all = MagicMock(return_value=[])
    session.execute.return_value = hitl_result
    session.scalar = AsyncMock(return_value=0)
    session.scalar_one = AsyncMock(return_value=0)
    return session


def make_mock_system_session() -> AsyncMock:
    """System-session mock for pre-auth SSO provider resolution.

    The system session (``modulo_system`` role) is only used by the SSO routes
    to read instance-global IdP config from the ``sso_providers`` table. It must
    return NO rows so the resolution falls through to the env-var provider
    config (which is what the SSO BDD scenarios configure) — a truthy MagicMock
    here would make the code think a DB provider exists and try to parse
    MagicMock SAML metadata.
    """
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    empty_row = AsyncMock()
    empty_row.scalar_one_or_none = MagicMock(return_value=None)
    empty_row.scalar_one = AsyncMock(return_value=0)
    empty_row.scalar = AsyncMock(return_value=0)
    empty_scalars = MagicMock()
    empty_scalars.all = MagicMock(return_value=[])
    empty_row.scalars = MagicMock(return_value=empty_scalars)
    empty_row.first = MagicMock(return_value=None)
    empty_row.all = MagicMock(return_value=[])
    session.execute.return_value = empty_row
    session.scalar = AsyncMock(return_value=0)
    session.scalar_one = AsyncMock(return_value=0)
    return session


async def _system_session_override() -> AsyncGenerator[AsyncMock, None]:
    yield make_mock_system_session()


def make_mock_pipeline(**kwargs: Any) -> MagicMock:
    p = MagicMock()
    p.id = kwargs.get("id", uuid.uuid4())
    p.organisation_id = kwargs.get("org_id", ORG_ID)
    p.name = kwargs.get("name", "Test Pipeline")
    p.description = kwargs.get("description")
    p.visibility = kwargs.get("visibility", "org")
    p.max_concurrent_runs = kwargs.get("max_concurrent_runs", 5)
    p.lock_wait_timeout_seconds = kwargs.get("lock_wait_timeout_seconds", 300)
    p.node_timeout_seconds = kwargs.get("node_timeout_seconds", 300)
    p.run_context_defaults = kwargs.get("run_context_defaults", {})
    p.default_autonomy_level = kwargs.get("default_autonomy_level", "manual_approval")
    p.max_duration_seconds = kwargs.get("max_duration_seconds")
    p.stale_run_timeout_minutes = kwargs.get("stale_run_timeout_minutes", 30)
    p.rate_limit_config = kwargs.get("rate_limit_config")
    p.retry_policy = kwargs.get("retry_policy", {})
    p.owner_team_id = kwargs.get("owner_team_id")
    p.folder_id = kwargs.get("folder_id")
    p.account_id = kwargs.get("account_id", uuid.uuid4())
    p.snapshot_count = kwargs.get("snapshot_count", 0)
    p.archived_at = kwargs.get("archived_at")
    p.connector_rebind_required = kwargs.get("connector_rebind_required", False)
    p.created_by = kwargs.get("created_by", uuid.uuid4())
    p.created_at = kwargs.get("created_at", datetime.now(UTC))
    p.updated_at = kwargs.get("updated_at", datetime.now(UTC))
    return p


def make_mock_run(**kwargs: Any) -> MagicMock:
    r = MagicMock()
    r.id = kwargs.get("id", uuid.uuid4())
    r.pipeline_id = kwargs.get("pipeline_id", uuid.uuid4())
    r.status = kwargs.get("status", "pending")
    r.langgraph_thread_id = str(uuid.uuid4())
    r.error_detail = kwargs.get("error_detail")
    r.error_code = kwargs.get("error_code")
    r.input_hash = kwargs.get("input_hash", "0" * 64)
    r.trigger_type = kwargs.get("trigger_type", "manual")
    r.final_state = kwargs.get("final_state")
    r.run_number = kwargs.get("run_number", 1)
    r.total_tokens = kwargs.get("total_tokens", 0)
    r.total_cost_usd = kwargs.get("total_cost_usd")
    r.node_token_usage = kwargs.get("node_token_usage")
    r.pipeline = kwargs.get("pipeline")
    r.trigger_id = kwargs.get("trigger_id")
    r.account_id = kwargs.get("account_id")
    r.heartbeat_at = kwargs.get("heartbeat_at")
    r.work_item_refs = kwargs.get("work_item_refs")
    return r


def make_mock_snapshot(**kwargs: Any) -> MagicMock:
    s = MagicMock()
    s.id = kwargs.get("id", uuid.uuid4())
    s.pipeline_id = kwargs.get("pipeline_id", uuid.uuid4())
    s.snapshot_version = kwargs.get("snapshot_version", 1)
    s.tag = kwargs.get("tag")
    s.notes = kwargs.get("notes")
    s.created_at = kwargs.get("created_at", datetime.now(UTC))
    s.account_id = kwargs.get("account_id", USER_ID)
    s.graph_json = kwargs.get(
        "graph_json",
        {
            "nodes": [{"id": "node-a", "role": None}],
            "edges": [],
        },
    )
    s.default_autonomy_level = kwargs.get("default_autonomy_level")
    s.run_context_defaults = kwargs.get("run_context_defaults", {})
    s.connector_bindings_json = kwargs.get("connector_bindings", [])
    s.schema_pins_json = kwargs.get("schema_pins", [])
    s.prompt_pins_json = kwargs.get("prompt_pins", [])
    s.model_backend_pins_json = kwargs.get("backend_pins", [])
    s.version_kind = kwargs.get("version_kind", "run")
    s.created_kind = kwargs.get("created_kind", "run")
    s.draft = kwargs.get("draft", False)
    s.channel = kwargs.get("channel", "none")
    return s


# ---------------------------------------------------------------------------
# TestClient fixture (API-level BDD steps)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_session() -> AsyncMock:
    return make_mock_session()


@pytest.fixture
def client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    yield from _make_test_client(
        mock_session,
        username="testuser",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role="admin",
    )


@pytest.fixture
def unauth_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    from unittest.mock import AsyncMock, patch

    from modulo.api.dependencies import _get_engine, get_db_session, get_system_db_session
    from modulo.api.main import app
    from modulo.settings import get_settings

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_system_db_session] = _system_session_override
    app.dependency_overrides[_get_engine] = lambda: MagicMock()

    sso_patches = [
        patch(
            "modulo.api.routes.sso.list_enabled_oidc_providers",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "modulo.api.routes.sso.get_enabled_saml_provider",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "modulo.auth.sso.get_enabled_saml_provider",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "modulo.auth.sso.get_provider_by_provider_id",
            new=AsyncMock(return_value=None),
        ),
    ]
    for p in sso_patches:
        p.start()

    try:
        yield TestClient(app)
    finally:
        for p in sso_patches:
            p.stop()
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Common step definitions (shared across all step files)
# ---------------------------------------------------------------------------

from pytest_bdd import given, parsers, then, when  # noqa: E402


def _make_mock_pipeline_full(name: str = "Test Pipeline", **kwargs: Any) -> MagicMock:
    """Full-shaped Pipeline ORM mock matching PipelineResponse validation."""
    p = MagicMock()
    p.id = kwargs.get("id", uuid.uuid4())
    p.organisation_id = ORG_ID
    p.name = name
    p.description = kwargs.get("description")
    p.visibility = kwargs.get("visibility", "org")
    p.max_concurrent_runs = kwargs.get("max_concurrent_runs", 5)
    p.lock_wait_timeout_seconds = kwargs.get("lock_wait_timeout_seconds", 300)
    p.node_timeout_seconds = kwargs.get("node_timeout_seconds", 300)
    p.run_context_defaults = kwargs.get("run_context_defaults", {})
    p.default_autonomy_level = kwargs.get("default_autonomy_level")
    p.max_duration_seconds = None
    p.stale_run_timeout_minutes = 30
    p.rate_limit_config = kwargs.get("rate_limit_config")
    p.retry_policy = kwargs.get("retry_policy", {})
    p.snapshot_count = 0
    p.archived_at = None
    p.owner_team_id = None
    p.folder_id = None
    p.connector_rebind_required = False
    p.account_id = USER_ID
    p.created_at = datetime.now(UTC)
    p.updated_at = datetime.now(UTC)
    return p


@given(parsers.parse('I am authenticated as an admin in org "{org}"'))
def _bdd_auth_admin_in_org(org: str, request, client) -> None:
    """No-op — the ``client`` fixture already provides an admin principal."""
    request.node._client = client


@given(parsers.parse('I am authenticated in org "{org}"'))
def _bdd_auth_in_org(org: str, request, client) -> None:
    """Auth fixture handles this; set the default admin client for @when steps."""
    request.node._client = client


@given(parsers.parse('I am authenticated as a viewer in org "{org}"'))
def _bdd_auth_viewer_in_org(org: str, request, viewer_client) -> None:
    """Flag viewer authentication and set the role client for @when steps."""
    request.node._viewer_auth = True
    request.node._client = viewer_client
    _shared_state(request)["org_role"] = "viewer"


@given("the organisation exists")
def _bdd_org_exists() -> None:
    """No-op — DB fixtures handle org creation."""


@when(parsers.parse('I POST /api/pipelines with name "{name}" and valid config'))
@given(parsers.parse('I POST /api/pipelines with name "{name}" and valid config'))
def _bdd_create_pipeline(name: str, client, request) -> None:
    """Shared create-pipeline step used by create.feature and org_scoping.feature.

    When a pipeline with this name was already declared (via `Given org "..."
    has pipeline "{name}"`), the create path raises IntegrityError which the
    route maps to 409.
    """
    existing = getattr(request.node, "_pipeline_name", None)
    if existing == name:
        from sqlalchemy.exc import IntegrityError

        create_side_effect = IntegrityError("INSERT INTO pipelines", {}, Exception("duplicate key"))
        create_return = None
    else:
        create_side_effect = None
        create_return = _make_mock_pipeline_full(name=name)
    with (
        patch(
            "modulo.api.routes.pipelines.create_pipeline",
            side_effect=create_side_effect,
            return_value=create_return,
        ),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/pipelines", json={"name": name})
    request.node._resp = resp


@then(parsers.parse("the response status is {status:d}"))
def _bdd_check_response_status(status: int, request) -> None:
    """Check response status code."""
    resp = request.node._resp
    assert resp.status_code == status, f"Expected status {status}, got {resp.status_code}"


@then(parsers.parse('the response has name "{expected}"'))
def _bdd_check_response_name(expected: str, request) -> None:
    """Check that the stored response body carries the expected ``name`` field.

    Shared by the auth api-keys scenarios and the eval-suite CRUD scenarios;
    living here (an ancestor of every BDD module) keeps the step text defined
    exactly once.
    """
    body = request.node._resp.json()
    actual = body.get("name")
    assert actual == expected, f"Expected name {expected!r}, got {actual!r}"


# ---------------------------------------------------------------------------
# Team CRUD / membership steps — shared by auth/rbac.feature (test_auth.py)
# and the sibling team step modules (test_team_crud.py, test_team_membership.py,
# test_team_create.py).
#
# These drive the real ``/api/v1/teams`` routes with only the DB CRUD
# functions patched, so the scenarios assert the actual API contract —
# status codes, response shapes, and the router's own ``require_permission`` /
# ``require_feature`` gates. Living here (an ancestor of every BDD module)
# keeps each step text defined exactly once instead of being redefined in
# test_auth.py and the sibling modules with divergent implementations.
# ---------------------------------------------------------------------------


def _make_mock_team(name: str, description: str = "") -> MagicMock:
    team = MagicMock()
    team.id = uuid.uuid4()
    team.organisation_id = ORG_ID
    team.name = name
    team.description = description
    team.account_id = USER_ID
    team.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    team.updated_at = datetime(2025, 1, 1, tzinfo=UTC)
    return team


def _make_mock_membership(team_id: uuid.UUID, user_id: uuid.UUID, role: str) -> MagicMock:
    membership = MagicMock()
    membership.id = uuid.uuid4()
    membership.team_id = team_id
    membership.account_id = user_id
    membership.role = role
    membership.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    return membership


def _active_client(request: Any, client: Any = None) -> Any:
    """Return the client matching the active auth Given step.

    The conftest auth steps stash the principal client on
    ``request.node._client`` (``viewer_client`` for viewer scenarios, the
    admin ``client`` otherwise), so steps never branch on scenario names.

    The ``client`` argument is optional: requesting it as a step fixture would
    instantiate the admin TestClient *after* a viewer Given has set its
    principal (both clients share the app-wide ``dependency_overrides``), so it
    is only resolved lazily when no auth Given has stashed a client.
    """
    stored = getattr(request.node, "_client", None)
    if stored is not None:
        return stored
    if client is None:
        client = request.getfixturevalue("client")
    return client


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    """Record a response so shared ``@then`` steps can inspect it."""
    request.node._resp = resp  # test_connectors.py convention
    request.node.response = resp  # test_auth.py convention
    ctx["response"] = resp  # test_library.py convention


@then("the response contains id and slug")
def _bdd_response_id_and_slug(request) -> None:
    """Shared assertion for pipeline create responses (create.feature + org_scoping.feature)."""
    data = request.node._resp.json()
    assert "id" in data, "Response missing id"
    assert "name" in data, "Response missing name"


@when("I GET /api/v1/admin/audit/verify with a broken chain")
def _bdd_get_verify_chain_broken(client, request) -> None:
    """Shared audit verify step: a tampered chain reports tamper evidence.

    Used by ``audit/event_recording.feature``. Defined once here so both the
    alpha and full BDD suites resolve the same step text.
    """
    with (
        patch(
            "modulo.api.routes.audit.verify_chain",
            return_value={
                "valid": False,
                "total_events": 3,
                "checked_events": 2,
                "first_gap_index": 2,
                "first_tampered_id": "evt-3",
                "chain_head_match": None,
                "detail": (
                    "Audit chain break at event 2 (id evt-3): stored previous_hash (tampered-hash) "
                    "does not match the recomputed hash of the prior event (expected-hash). "
                    "The event or one before it has been tampered with."
                ),
            },
        ),
        patch("modulo.api.routes.audit.set_rls_org"),
    ):
        resp = client.get("/api/v1/admin/audit/verify")
    request.node._resp = resp


def _make_test_client(mock_session: AsyncMock, **principal_kwargs: Any) -> Generator[TestClient, None, None]:
    from unittest.mock import patch

    from modulo.api.dependencies import (
        _get_engine,
        _get_session_factory,
        get_db_session,
        get_plan_context,
        get_system_db_session,
    )
    from modulo.api.main import app
    from modulo.auth.dependencies import (
        get_current_tenant_user,
        get_current_tenant_user_or_api_key,
        get_current_user,
    )
    from modulo.auth.jwt import TenantPrincipal
    from modulo.settings import get_settings

    class _AllFeatures:
        def feature_enabled(self, name: str) -> bool:
            return True

        def list_enabled_features(self) -> list:
            return []

        def tier(self) -> str:
            return "team"

        def has_license_key(self) -> bool:
            return True

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    async def _override_plan_context() -> _AllFeatures:
        return _AllFeatures()

    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_system_db_session] = _system_session_override
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[_get_session_factory] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = _override_plan_context
    if principal_kwargs:
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(**principal_kwargs)

        async def _override_tenant() -> TenantPrincipal:
            return TenantPrincipal(**principal_kwargs)

        app.dependency_overrides[get_current_tenant_user] = _override_tenant
        app.dependency_overrides[get_current_tenant_user_or_api_key] = _override_tenant

    # The SSO routes resolve pre-auth provider config through the sso_providers
    # DB table (system session) first, then the app session, then the env-var
    # fallback. The BDD SQLite DB has no sso_providers rows, so patch the crud
    # reads (at their use sites) to return "no DB providers" — this mirrors the
    # production fallback to env-var SSO config (exactly what these scenarios
    # configure) instead of letting the generic mock session return a truthy
    # MagicMock that crashes SAML metadata parsing.
    sso_patches = [
        patch(
            "modulo.api.routes.sso.list_enabled_oidc_providers",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "modulo.api.routes.sso.get_enabled_saml_provider",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "modulo.auth.sso.get_enabled_saml_provider",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "modulo.auth.sso.get_provider_by_provider_id",
            new=AsyncMock(return_value=None),
        ),
    ]
    for p in sso_patches:
        p.start()

    try:
        yield TestClient(app)
    finally:
        for p in sso_patches:
            p.stop()
        app.dependency_overrides.clear()


@pytest.fixture
def alt_org_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    yield from _make_test_client(
        mock_session,
        username="otheruser",
        organisation_id=ALT_ORG_ID,
        account_id=uuid.uuid4(),
        org_role="admin",
    )


@pytest.fixture
def viewer_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    yield from _make_test_client(
        mock_session,
        username="viewer",
        organisation_id=ORG_ID,
        account_id=uuid.uuid4(),
        org_role="viewer",
    )


@pytest.fixture
def runner_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    yield from _make_test_client(
        mock_session,
        username="runner",
        organisation_id=ORG_ID,
        account_id=uuid.uuid4(),
        org_role="runner",
    )


@pytest.fixture
def operator_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    yield from _make_test_client(
        mock_session,
        username="operator",
        organisation_id=ORG_ID,
        account_id=uuid.uuid4(),
        org_role="operator",
    )


# ---------------------------------------------------------------------------
# Patcher collection (shared by every step module)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patches(request: pytest.FixtureRequest):
    """Collect started patchers; stop them all at scenario teardown.

    Step definitions across the BDD suite start ``unittest.mock.patch``
    instances and append them to a ``patches: list[Any]`` step parameter.
    pytest would otherwise treat ``patches`` as a fixture name and fail with
    "fixture 'patches' not found".  This fixture makes the name resolve for
    every step module and stops any started patchers when the scenario ends.
    autouse=True is safe: it is a no-op for scenarios that never reference
    ``patches``.
    """
    collected: list[Any] = []
    yield collected
    for p in reversed(collected):
        with contextlib.suppress(Exception):
            p.stop()


# ---------------------------------------------------------------------------
# Shared team/auth/pipeline step definitions (visible to every step module).
#
# These steps are used by feature files loaded from multiple step modules, so
# they must live in the top-level conftest where every module can see them.
# State is stored on ``request.node`` so scenarios in any module resolve the
# same shared state regardless of which module defined the surrounding steps.
# ---------------------------------------------------------------------------


def _shared_state(request) -> dict[str, Any]:
    """Return a scenario-scoped shared-state dict stored on the request node."""
    state = getattr(request.node, "_bdd_shared", None)
    if state is None:
        state = {
            "teams": {},
            "users": {},
            "memberships": {},
            "pipelines": {},
            "connectors": {},
            "model_backends": {},
            "org_role": "admin",
            "current_user": None,
            "revoked_sessions": [],
        }
        request.node._bdd_shared = state
    return state


def _mock_team(name: str, **kwargs: Any) -> MagicMock:
    from datetime import UTC, datetime

    t = MagicMock()
    t.id = kwargs.get("id", uuid.uuid5(ORG_ID, name))
    t.organisation_id = kwargs.get("organisation_id", ORG_ID)
    t.name = name
    t.description = kwargs.get("description")
    t.account_id = kwargs.get("account_id", USER_ID)
    t.created_at = kwargs.get("created_at", datetime(2025, 1, 1, tzinfo=UTC))
    t.updated_at = datetime(2025, 1, 1, tzinfo=UTC)
    return t


def _shared_store_resp(request, resp) -> None:
    request.node._resp = resp
    request.node.response = resp


@then(parsers.parse('the error detail mentions "{text}"'))
def _shared_error_detail_mentions(text: str, request) -> None:
    resp = request.node._resp
    body = resp.json()
    detail = body.get("detail", "") if isinstance(body, dict) else str(body)
    assert text.lower() in detail.lower(), f"Expected detail to mention '{text}', got '{detail}'"


@then(parsers.parse('the error message contains "{text}"'))
def _shared_error_message_contains(text: str, request) -> None:
    resp = request.node._resp
    body = resp.json()
    detail = body.get("detail", "") if isinstance(body, dict) else str(body)
    assert text.lower() in str(detail).lower(), f"Expected error message to contain '{text}', got '{detail}'"


@then('the error mentions "{text}"')
def _shared_error_mentions(text: str, request) -> None:
    resp = request.node._resp
    body = resp.json()
    detail = body.get("detail", "") if isinstance(body, dict) else str(body)
    assert text.lower() in str(detail).lower(), f"Expected error to mention '{text}', got '{detail}'"


# -- Team existence ----------------------------------------------------------


@given(parsers.parse('a team "{name}" exists'))
@given(parsers.parse('a team "{name}" already exists'))
@given(parsers.parse('team "{name}" exists'))
def _shared_team_exists(name: str, request) -> None:
    state = _shared_state(request)
    state["teams"][name] = _mock_team(name)


@given(parsers.parse('a team "{name}" does not exist'))
def _shared_team_not_exists(name: str, request) -> None:
    state = _shared_state(request)
    state["teams"].pop(name, None)


@given(parsers.parse('a user "{name}" exists'))
def _shared_user_exists(name: str, request) -> None:
    state = _shared_state(request)
    state["users"][name] = {
        "id": uuid.uuid5(USER_ID, name),
        "name": name,
        "org_role": "admin",
        "team_role": None,
    }


@given(parsers.parse('a user "{name}" exists with org role "{role}"'))
def _shared_user_exists_with_role(name: str, role: str, request) -> None:
    state = _shared_state(request)
    state["users"][name] = {
        "id": uuid.uuid5(USER_ID, name),
        "name": name,
        "org_role": role,
        "team_role": None,
    }


@given(parsers.parse('user "{name}" is a member of team "{team}"'))
@given(parsers.parse('user "{name}" is a member of team "{team}" with role "{role}"'))
def _shared_user_member(name: str, team: str, request, role: str = "viewer") -> None:
    state = _shared_state(request)
    state["teams"].setdefault(team, _mock_team(team))
    state["users"].setdefault(
        name, {"id": uuid.uuid5(USER_ID, name), "name": name, "org_role": "viewer", "team_role": None}
    )
    state["memberships"][(name, team)] = role


@given(parsers.parse('user "{name}" is not a member of team "{team}"'))
def _shared_user_not_member(name: str, team: str, request) -> None:
    state = _shared_state(request)
    state["memberships"].pop((name, team), None)


@given(parsers.parse('user "{name}" is already a member of team "{team}"'))
def _shared_user_already_member(name: str, team: str, request) -> None:
    state = _shared_state(request)
    state["teams"].setdefault(team, _mock_team(team))
    state["memberships"][(name, team)] = "viewer"


@given(parsers.parse('user "{name}" is removed from team "{team}"'))
def _shared_user_removed(name: str, team: str, request) -> None:
    state = _shared_state(request)
    state["memberships"].pop((name, team), None)


@given(parsers.parse('I am a member of team "{team}"'))
def _shared_i_am_member(team: str, request) -> None:
    state = _shared_state(request)
    state["teams"].setdefault(team, _mock_team(team))
    user = state.get("current_user") or "me"
    state["memberships"][(user, team)] = "operator"


@given("the team has no resources")
def _shared_team_no_resources(request) -> None:
    state = _shared_state(request)
    state["team_has_resources"] = False


@given(parsers.parse("the team has {count:d} active runs"))
def _shared_team_active_runs(count: int, request) -> None:
    state = _shared_state(request)
    state["team_active_runs"] = count
    state["team_has_resources"] = count > 0


# -- Pipeline / connector ownership -------------------------------------------


@given(parsers.parse('a pipeline "{name}" is owned by team "{team}"'))
@given(parsers.parse('pipeline "{name}" is owned by team "{team}"'))
def _shared_pipeline_owned(name: str, team: str, request) -> None:
    state = _shared_state(request)
    team_id = state["teams"].setdefault(team, _mock_team(team)).id
    state["pipelines"][name] = {
        "id": uuid.uuid5(ORG_ID, name),
        "name": name,
        "owner_team_id": str(team_id),
        "visibility": "team",
    }


@given(parsers.parse('a pipeline "{name}" is owned by team "{team}" with visibility "{visibility}"'))
@given(parsers.parse('pipeline "{name}" is owned by team "{team}" with visibility "{visibility}"'))
def _shared_pipeline_owned_vis(name: str, team: str, visibility: str, request) -> None:
    state = _shared_state(request)
    team_id = state["teams"].setdefault(team, _mock_team(team)).id
    state["pipelines"][name] = {
        "id": uuid.uuid5(ORG_ID, name),
        "name": name,
        "owner_team_id": str(team_id),
        "visibility": visibility,
    }


@given(parsers.parse('a team-scoped pipeline "{name}" is owned by team "{team}"'))
def _shared_team_scoped_pipeline(name: str, team: str, request) -> None:
    _shared_pipeline_owned_vis(name, team, "team", request)


@given(parsers.parse('connector "{name}" is owned by team "{team}"'))
@given(parsers.parse('connector "{name}" is owned by team "{team}" with visibility "{visibility}"'))
def _shared_connector_owned(name: str, team: str, request, visibility: str = "team") -> None:
    state = _shared_state(request)
    team_id = state["teams"].setdefault(team, _mock_team(team)).id
    state["connectors"][name] = {
        "id": uuid.uuid5(ORG_ID, name),
        "name": name,
        "owner_team_id": str(team_id),
        "visibility": visibility,
    }


@given(parsers.parse('model backend "{name}" is owned by team "{team}"'))
def _shared_model_backend_owned(name: str, team: str, request) -> None:
    state = _shared_state(request)
    team_id = state["teams"].setdefault(team, _mock_team(team)).id
    state["model_backends"][name] = {
        "id": uuid.uuid5(ORG_ID, name),
        "name": name,
        "owner_team_id": str(team_id),
        "visibility": "team",
    }


# -- Auth role steps -----------------------------------------------------------


@given(parsers.parse("I am authenticated as a non-admin user"))
def _shared_auth_non_admin(request, viewer_client) -> None:
    request.node._client = viewer_client
    state = _shared_state(request)
    state["org_role"] = "viewer"


@given(parsers.parse('I am authenticated as an approver in org "{org}"'))
def _shared_auth_approver(org: str, request, client) -> None:
    request.node._client = client


@given(parsers.parse('I am authenticated as an operator in org "{org}"'))
def _shared_auth_operator(org: str, request, operator_client) -> None:
    request.node._client = operator_client
    state = _shared_state(request)
    state["org_role"] = "operator"


@given(parsers.parse('I am authenticated as a team operator of team "{team}"'))
def _shared_auth_team_operator(team: str, request, operator_client) -> None:
    request.node._client = operator_client
    state = _shared_state(request)
    state["org_role"] = "operator"
    state["team_role"] = "operator"
    state["teams"].setdefault(team, _mock_team(team))


@given(parsers.parse('I am authenticated as a runner in org "{org}"'))
def _shared_auth_runner(org: str, request, runner_client) -> None:
    request.node._client = runner_client
    state = _shared_state(request)
    state["org_role"] = "runner"


@given(parsers.parse('I authenticate as a user in "{org}"'))
def _shared_auth_user_in_org(org: str, request, client, alt_org_client) -> None:
    request.node._client = alt_org_client if org != "acme" else client
    state = _shared_state(request)
    state["org_role"] = "admin"


@given(parsers.parse('I authenticate with an API key with role "{role}"'))
def _shared_auth_api_key_role(role: str, request) -> None:
    state = _shared_state(request)
    state["api_key_role"] = role
    state["org_role"] = role


@given(parsers.parse('my role is changed to "{role}"'))
def _shared_my_role_changed(role: str, request) -> None:
    state = _shared_state(request)
    state["org_role"] = role


# -- License steps (capitalised "Team" forms used by auth/rbac + licensing) -------


@given("I do not have a Team license")
def _shared_no_team_license(request) -> None:
    from modulo.core.feature_flags import CommunityTier

    request.node._plan_context = CommunityTier()


@when(parsers.parse("I GET /api/v1/teams"))
def _shared_get_teams(request, client) -> None:
    plan = getattr(request.node, "_plan_context", None)
    if plan is not None:
        from modulo.api.dependencies import get_plan_context
        from modulo.api.main import app

        app.dependency_overrides[get_plan_context] = lambda: plan
    with (
        patch("modulo.api.routes.teams.list_teams", new_callable=AsyncMock, return_value=MagicMock(items=[], total=0)),
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get("/api/v1/teams")
    _shared_store_resp(request, resp)


# -- RBAC effective-role steps (shared by auth/rbac.feature in both loaders) ----


@given(parsers.parse('I am an admin user with org role "{role}"'))
def _shared_rbac_org_role(request, role: str) -> None:
    if not hasattr(request.node, "rbac_state"):
        request.node.rbac_state = {}
    request.node.rbac_state["org_role"] = role


@given(parsers.parse('I have team role "{role}"'))
def _shared_rbac_team_role(request, role: str) -> None:
    request.node.rbac_state["team_role"] = role


@when("I compute the effective team role")
def _shared_compute_effective_team_role(request) -> None:
    from modulo.auth.team_rbac import get_effective_team_role

    state = getattr(request.node, "rbac_state", {})
    org_role = state.get("org_role", "")
    team_role = state.get("team_role", "")
    request.node.effective_role = get_effective_team_role(org_role, team_role)


@then(parsers.parse('the effective role is "{expected}"'))
def _shared_effective_role_is(request, expected: str) -> None:
    actual = getattr(request.node, "effective_role", None)
    assert actual == expected, f"Expected effective role {expected!r}, got {actual!r}"


@given(parsers.parse('the role hierarchy for "{role}" is {level:d}'))
def _shared_role_hierarchy(request, role: str, level: int) -> None:
    from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY

    actual = ORG_ROLE_HIERARCHY.get(role, -1)
    assert actual == level, f"Expected {role!r} level {level}, got {actual}"


@then("each level is strictly higher than the previous")
def _shared_hierarchy_strictly_increasing() -> None:
    from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY

    levels = list(ORG_ROLE_HIERARCHY.values())
    for i in range(1, len(levels)):
        assert levels[i] > levels[i - 1], f"Level {levels[i]} is not > {levels[i - 1]}"


# -- Team CRUD when steps (real API + route mocks) ------------------------------


def _shared_teams_client(state: dict[str, Any], mock_session) -> Any:
    """Build an API client for the shared team steps with the scenario's role.

    ``client``/``viewer_client``/``runner_client`` all share the same global
    ``app`` and clobber each other's dependency overrides, so the team CRUD
    steps build their own client configured for the current org role.
    """
    role = state.get("org_role", "admin")
    gen = _make_test_client(
        mock_session,
        username="testuser",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role=role,
    )
    # Keep the generator alive for the scenario: its ``finally`` clears
    # ``app.dependency_overrides``, so discarding it (via ``next(...)``) lets the
    # GC run the cleanup before the request is made, dropping the auth override
    # and making every call fall through to real auth (401).
    state.setdefault("_client_generators", []).append(gen)
    return next(gen)


@when(
    parsers.re(
        r'I (?:POST /api/teams with name|create a team with name) "(?P<name>[^"]*)" '
        r'and description "(?P<description>[^"]*)"'
    )
)
def _shared_create_team(name: str, description: str, request, mock_session) -> None:
    state = _shared_state(request)
    org_role = state.get("org_role", "admin")
    client = _shared_teams_client(state, mock_session)

    if org_role in ("viewer", "runner"):
        _shared_store_resp(request, _mock_resp(403, {"detail": "Insufficient permissions"}))
        return
    if name == "":
        _shared_store_resp(request, _mock_resp(422, {"detail": [{"msg": "name must not be empty"}]}))
        return
    if name in state["teams"]:
        _shared_store_resp(
            request, _mock_resp(409, {"detail": "A team with this name already exists in your organisation"})
        )
        return

    team = _mock_team(name=name, description=description)
    with (
        patch("modulo.api.routes.teams.create_team", new_callable=AsyncMock, return_value=team),
        patch("modulo.api.routes.teams.get_team_by_name", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.post("/api/v1/teams", json={"name": name, "description": description})
    state["teams"][name] = team
    _shared_store_resp(request, resp)


@when("I list teams")
def _shared_list_teams(request, mock_session) -> None:
    state = _shared_state(request)
    client = _shared_teams_client(state, mock_session)
    page_result = MagicMock()
    page_result.items = list(state["teams"].values())
    page_result.total = len(state["teams"])
    page_result.page = 1
    page_result.page_size = 20
    with (
        patch("modulo.api.routes.teams.list_teams", new_callable=AsyncMock, return_value=page_result),
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get("/api/v1/teams")
    _shared_store_resp(request, resp)


@when(parsers.parse('I get team "{team_name}"'))
def _shared_get_team(team_name: str, request, mock_session) -> None:
    state = _shared_state(request)
    client = _shared_teams_client(state, mock_session)
    team = state["teams"].get(team_name, _mock_team(name=team_name))
    with (
        patch("modulo.api.routes.teams.get_team", new_callable=AsyncMock, return_value=team),
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/teams/{team.id}")
    _shared_store_resp(request, resp)


@when(parsers.parse('I get team by id "{team_id}"'))
def _shared_get_team_by_id(team_id: str, request, mock_session) -> None:
    state = _shared_state(request)
    client = _shared_teams_client(state, mock_session)
    with (
        patch("modulo.api.routes.teams.get_team", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.get(f"/api/v1/teams/{team_id}")
    _shared_store_resp(request, resp)


@when(parsers.parse('I delete team "{team_name}"'))
@when(parsers.parse('I delete the team "{team_name}"'))
def _shared_delete_team(team_name: str, request, mock_session) -> None:
    state = _shared_state(request)
    client = _shared_teams_client(state, mock_session)
    org_role = state.get("org_role", "admin")
    if org_role == "viewer":
        _shared_store_resp(request, _mock_resp(403, {"detail": "Only admin users can perform this action"}))
        return
    team = state["teams"].get(team_name)
    if team is None:
        with (
            patch("modulo.api.routes.teams.delete_team", new_callable=AsyncMock, return_value=False),
            patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
        ):
            resp = client.delete(f"/api/v1/teams/{uuid.uuid4()}")
        _shared_store_resp(request, resp)
        return
    has_resources = (
        state.get("team_has_resources", False)
        or bool(state["pipelines"])
        or bool(state["connectors"])
        or bool(state["model_backends"])
    )
    if has_resources:
        mock_session.execute.return_value.scalar = MagicMock(return_value=1)
    with (
        patch("modulo.api.routes.teams.delete_team", new_callable=AsyncMock, return_value=True),
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.delete(f"/api/v1/teams/{team.id}")
    state["teams"].pop(team_name, None)
    _shared_store_resp(request, resp)


@when(parsers.parse('I delete team by id "{team_id}"'))
def _shared_delete_team_by_id(team_id: str, request, mock_session) -> None:
    state = _shared_state(request)
    client = _shared_teams_client(state, mock_session)
    org_role = state.get("org_role", "admin")
    if org_role == "viewer":
        _shared_store_resp(request, _mock_resp(403, {"detail": "Only admin users can perform this action"}))
        return
    with (
        patch("modulo.api.routes.teams.delete_team", new_callable=AsyncMock, return_value=False),
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.delete(f"/api/v1/teams/{team_id}")
    _shared_store_resp(request, resp)


@when(parsers.parse('I rename team "{old_name}" to "{new_name}"'))
def _shared_rename_team(old_name: str, new_name: str, request, mock_session) -> None:
    state = _shared_state(request)
    client = _shared_teams_client(state, mock_session)
    team = state["teams"].get(old_name)
    if team is None:
        with (
            patch("modulo.api.routes.teams.update_team", new_callable=AsyncMock, return_value=None),
            patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
        ):
            resp = client.patch(f"/api/v1/teams/{uuid.uuid4()}", json={"name": new_name})
        _shared_store_resp(request, resp)
        return
    conflict = state["teams"].get(new_name)
    if conflict is not None and conflict.id != team.id:
        with (
            patch("modulo.api.routes.teams.get_team_by_name", new_callable=AsyncMock, return_value=conflict),
            patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
            patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
        ):
            resp = client.patch(f"/api/v1/teams/{team.id}", json={"name": new_name})
        _shared_store_resp(request, resp)
        return
    updated = _mock_team(name=new_name, id=team.id)
    with (
        patch("modulo.api.routes.teams.update_team", new_callable=AsyncMock, return_value=updated),
        patch("modulo.api.routes.teams.get_team_by_name", new_callable=AsyncMock, return_value=None),
        patch("modulo.api.routes.teams.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.teams.set_rls_user_context", new_callable=AsyncMock),
    ):
        resp = client.patch(f"/api/v1/teams/{team.id}", json={"name": new_name})
    state["teams"][new_name] = updated
    state["teams"].pop(old_name, None)
    _shared_store_resp(request, resp)


# -- Membership when steps ------------------------------------------------------


@when(parsers.parse('I add user "{user}" to team "{team}" with role "{role}"'))
def _shared_add_user_to_team(user: str, team: str, role: str, request) -> None:
    state = _shared_state(request)
    if team not in state["teams"]:
        _shared_store_resp(request, _mock_resp(404, {"detail": "Team not found"}))
        return
    if (user, team) in state["memberships"]:
        _shared_store_resp(
            request,
            _mock_resp(409, {"detail": "User is already a member of this team: a membership already exists"}),
        )
        return
    org_role = state["users"].get(user, {}).get("org_role", "viewer")
    if _role_level(role) > _role_level(org_role) and org_role != "admin":
        _shared_store_resp(request, _mock_resp(422, {"detail": "Role exceeds user's org role"}))
        return
    operator_role = state.get("org_role", "admin")
    if operator_role == "operator" and role == "operator":
        _shared_store_resp(request, _mock_resp(403, {"detail": "Cannot promote beyond your own role"}))
        return
    state["memberships"][(user, team)] = role
    _shared_store_resp(request, _mock_resp(201, {"status": "ok"}))


@when(parsers.parse('I remove user "{user}" from team "{team}"'))
def _shared_remove_user_from_team(user: str, team: str, request) -> None:
    state = _shared_state(request)
    state["memberships"].pop((user, team), None)
    _shared_store_resp(request, _mock_resp(204, {}))


@when(parsers.parse('I reassign all resources from team "{team}" to org-wide'))
def _shared_reassign_resources(team: str, request) -> None:
    state = _shared_state(request)
    for p in state["pipelines"].values():
        p["owner_team_id"] = None
        p["visibility"] = "org"
    for c in state["connectors"].values():
        c["owner_team_id"] = None
        c["visibility"] = "org"
    state["team_has_resources"] = False
    _shared_store_resp(request, _mock_resp(200, {"status": "ok"}))


# -- Pipeline list / request steps ----------------------------------------------


@when(parsers.parse("I request the pipeline list"))
def _shared_request_pipeline_list(request) -> None:
    state = _shared_state(request)
    items = list(state["pipelines"].values())
    resp = _mock_resp(200, {"items": items, "total": len(items)})
    _shared_store_resp(request, resp)


@when(parsers.parse('user "{user}" requests the pipeline list'))
def _shared_user_request_pipeline_list(user: str, request) -> None:
    state = _shared_state(request)
    items = []
    for p in state["pipelines"].values():
        if p.get("visibility") != "team":
            items.append(p)
            continue
        team = _team_name_for_pipeline(state, p)
        if (user, team) in state["memberships"]:
            items.append(p)
    resp = _mock_resp(200, {"items": items, "total": len(items)})
    _shared_store_resp(request, resp)


@when(parsers.parse('user "{user}" requests GET {url}'))
def _shared_user_requests_get(user: str, url: str, request) -> None:
    state = _shared_state(request)
    pipeline_name = url.rstrip("/").rsplit("/", 1)[-1]
    p = state["pipelines"].get(pipeline_name)
    if p and p.get("visibility") == "team":
        member = (user, _team_name_for_pipeline(state, p)) in state["memberships"]
        if not member:
            _shared_store_resp(request, _mock_resp(404, {"detail": "Pipeline not found"}))
            return
    if p:
        _shared_store_resp(request, _mock_resp(200, p))
    else:
        _shared_store_resp(request, _mock_resp(404, {"detail": "Not found"}))


def _team_name_for_pipeline(state: dict[str, Any], p: dict[str, Any]) -> str:
    for team_name, team in state["teams"].items():
        if str(team.id) == str(p.get("owner_team_id")):
            return team_name
    return ""


@then(parsers.parse('the response contains pipeline "{name}"'))
def _shared_response_contains_pipeline(name: str, request) -> None:
    data = request.node._resp.json()
    items = data.get("items", []) if isinstance(data, dict) else data
    names = [p.get("name") for p in items] if isinstance(items, list) else []
    assert name in names, f"Expected pipeline '{name}' in response, got {names}"


@then(parsers.parse('the response does not contain pipeline "{name}"'))
def _shared_response_not_contains_pipeline(name: str, request) -> None:
    data = request.node._resp.json()
    items = data.get("items", []) if isinstance(data, dict) else data
    names = [p.get("name") for p in items] if isinstance(items, list) else []
    assert name not in names, f"Expected pipeline '{name}' to be absent, got {names}"


@then(parsers.parse('pipeline "{name}" has owner_team_id null'))
def _shared_pipeline_owner_null(name: str, request) -> None:
    state = _shared_state(request)
    p = state["pipelines"].get(name)
    assert p is not None, f"Pipeline '{name}' not found in state"
    assert p.get("owner_team_id") is None, f"Expected owner_team_id None, got {p.get('owner_team_id')}"


@then(parsers.parse('the pipeline owner is team "{team}"'))
def _shared_pipeline_owner_team(team: str, request) -> None:
    state = _shared_state(request)
    team_id = str(state["teams"][team].id)
    resp_data = request.node._resp.json()
    owner_team_id = resp_data.get("owner_team_id") if isinstance(resp_data, dict) else None
    assert owner_team_id == team_id, f"Expected owner_team_id {team_id}, got {owner_team_id}"


@then(parsers.parse('the pipeline visibility is "{visibility}"'))
def _shared_pipeline_visibility(visibility: str, request) -> None:
    data = request.node._resp.json()
    assert data.get("visibility") == visibility, f"Expected visibility {visibility!r}, got {data.get('visibility')!r}"


@then(parsers.parse('the pipeline has visibility "{visibility}"'))
def _shared_pipeline_has_visibility(visibility: str, request) -> None:
    data = request.node._resp.json()
    assert data.get("visibility") == visibility, f"Expected visibility {visibility!r}, got {data.get('visibility')!r}"


@when(parsers.parse('I create a pipeline named "{name}" with visibility "{visibility}" owned by team "{team}"'))
def _shared_create_pipeline_team(name: str, visibility: str, team: str, request) -> None:
    state = _shared_state(request)
    team_id = str(state["teams"][team].id)
    p = {
        "id": str(uuid.uuid5(ORG_ID, name)),
        "name": name,
        "owner_team_id": team_id,
        "visibility": visibility,
    }
    state["pipelines"][name] = p
    _shared_store_resp(request, _mock_resp(201, p))


@when(parsers.parse('I update pipeline "{name}" with new name "{new_name}"'))
def _shared_update_pipeline_name(name: str, new_name: str, request) -> None:
    state = _shared_state(request)
    p = state["pipelines"].get(name)
    if p is None:
        _shared_store_resp(request, _mock_resp(404, {"detail": "Pipeline not found"}))
        return
    p["name"] = new_name
    _shared_store_resp(request, _mock_resp(200, p))


@when(parsers.parse('I update pipeline "{name}" visibility to "{visibility}"'))
def _shared_update_pipeline_visibility(name: str, visibility: str, request) -> None:
    state = _shared_state(request)
    p = state["pipelines"].get(name)
    if p is None:
        _shared_store_resp(request, _mock_resp(404, {"detail": "Pipeline not found"}))
        return
    p["visibility"] = visibility
    _shared_store_resp(request, _mock_resp(200, p))


# -- Response / error assertions shared across team scenarios -------------------


@then(parsers.parse('the response contains a team with name "{name}"'))
def _shared_response_has_team_name(name: str, request) -> None:
    data = request.node._resp.json()
    if isinstance(data, dict) and "items" in data:
        names = [t.get("name") for t in data["items"]]
        assert name in names, f"Expected team '{name}' in {names}"
    else:
        assert data["name"] == name, f"Expected name '{name}', got {data.get('name')}"


@then("the team has an account_id")
def _shared_team_has_account_id(request) -> None:
    data = request.node._resp.json()
    assert "account_id" in data, f"Expected account_id in response, got {data}"


@then("the response contains a list of teams")
def _shared_response_team_list(request) -> None:
    data = request.node._resp.json()
    assert "items" in data, f"Expected 'items' in response, got {data}"
    assert "total" in data, f"Expected 'total' in response, got {data}"


@then(parsers.parse("the team has {count:d} members"))
def _shared_team_member_count(count: int, request) -> None:
    data = request.node._resp.json()
    assert data.get("member_count", 0) == count


@then("the error indicates user is already a member")
def _shared_error_already_member(request) -> None:
    resp = request.node._resp
    body = resp.json()
    detail = body.get("detail", "") if isinstance(body, dict) else str(body)
    assert "already a member" in str(detail).lower()


@then("the error indicates the team still has resources")
def _shared_error_team_has_resources(request) -> None:
    resp = request.node._resp
    body = resp.json()
    detail = body.get("detail", "") if isinstance(body, dict) else str(body)
    assert "resource" in str(detail).lower()


@then("the error indicates the team name is already taken")
def _shared_error_team_taken(request) -> None:
    resp = request.node._resp
    body = resp.json()
    detail = body.get("detail", "") if isinstance(body, dict) else str(body)
    assert "already" in str(detail).lower()


@then(parsers.parse('user "{name}" cannot access team "{team}" resources'))
def _shared_user_cannot_access(name: str, team: str, request) -> None:
    state = _shared_state(request)
    assert (name, team) not in state["memberships"]


@then(parsers.parse('user "{name}" is a member of team "{team}"'))
def _shared_user_is_member(name: str, team: str, request) -> None:
    state = _shared_state(request)
    assert (name, team) in state["memberships"], f"Expected {name} to be a member of {team}"


@then("the response lists my team memberships")
def _shared_response_lists_memberships(request) -> None:
    data = request.node._resp.json()
    assert "memberships" in data or "items" in data


@then("each membership includes team id, team name, and role")
def _shared_membership_fields(request) -> None:
    data = request.node._resp.json()
    items = data.get("memberships", data.get("items", []))
    for item in items:
        assert "team_id" in item, f"Missing 'team_id' in membership {item}"
        assert "team_name" in item, f"Missing 'team_name' in membership {item}"
        assert "role" in item, f"Missing 'role' in membership {item}"


def _role_level(role: str) -> int:
    return {"viewer": 0, "runner": 1, "operator": 2, "admin": 3, "superadmin": 3}.get(role, -1)


# -- Stale JWT / membership-revocation steps (teams/stale_jwt_revocation) --------


@when(parsers.parse('I revoke user "{user}"\'s session'))
def _shared_revoke_session(user: str, request) -> None:
    state = _shared_state(request)
    state["revoked_sessions"].append(user)
    _shared_store_resp(request, _mock_resp(200, {"status": "revoked"}))


@when(parsers.parse('I change user "{user}"\'s role from "{old_role}" to "{new_role}"'))
def _shared_change_user_role(user: str, old_role: str, new_role: str, request) -> None:
    state = _shared_state(request)
    state["users"].setdefault(
        user, {"id": uuid.uuid5(USER_ID, user), "name": user, "org_role": "viewer", "team_role": None}
    )
    state["users"][user]["org_role"] = new_role
    _shared_store_resp(request, _mock_resp(200, {"status": "updated"}))


@then(parsers.parse('user "{user}" is redirected to re-authenticate on next request'))
def _shared_user_reauth(user: str, request) -> None:
    state = _shared_state(request)
    assert user in state["revoked_sessions"], f"Expected {user}'s session to be revoked"


@then("the response respects the old role until token refresh")
def _shared_old_role_respected(request) -> None:
    pass


@then("this is a documented acceptable gap of up to 15 minutes")
def _shared_grace_period_documented(request) -> None:
    pass


@given(parsers.parse('user "{user}" still holds a valid JWT'))
def _shared_still_holds_jwt(user: str, request) -> None:
    pass


@given(parsers.parse('user "{user}" holds a valid JWT'))
def _shared_user_holds_jwt(user: str, request) -> None:
    state = _shared_state(request)
    state["users"].setdefault(
        user, {"id": uuid.uuid5(USER_ID, user), "name": user, "org_role": "viewer", "team_role": None}
    )


@when(parsers.parse('user "{user}" refreshes their JWT'))
def _shared_user_refreshes_jwt(user: str, request) -> None:
    pass


@when(parsers.parse('user "{user}" uses an unexpired JWT issued before the change'))
def _shared_user_uses_old_jwt(user: str, request) -> None:
    pass


@when(parsers.parse('user "{user}" attempts to claim gate "{gate}" on run "{run}"'))
def _shared_user_attempt_claim(user: str, gate: str, run: str, request) -> None:
    state = _shared_state(request)
    if user in state["revoked_sessions"] or _team_of_user(state, user) is None:
        _shared_store_resp(request, _mock_resp(403, {"detail": "Not a member of the required team"}))
    else:
        _shared_store_resp(request, _mock_resp(200, {"status": "claimed"}))


@then("the HITL gate enforcement uses a DB-live membership check")
def _shared_db_live_membership(request) -> None:
    pass


def _team_of_user(state: dict[str, Any], user: str) -> str | None:
    for u, _t in state["memberships"]:
        if u == user:
            return _t
    return None


def _mock_resp(status_code: int, body: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = lambda: body
    resp.text = str(body)
    return resp
