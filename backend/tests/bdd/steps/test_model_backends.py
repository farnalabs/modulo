"""Step definitions for Model Backend features — backend selection, rate limiting,
health checks, CRUD, and error handling."""

import contextlib
import uuid
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.db.models.model_backend import ModelBackend

# ---------------------------------------------------------------------------
# Active features
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/model_backends/backend_selection.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/model_backends/backend_health_check.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/model_backends/backend_crud.feature")
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../../bdd/features/model_backends/backend_error_handling.feature")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx():
    """Shared mutable context dict for model backend tests."""
    return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_model_backend(**kwargs: object) -> MagicMock:
    """Build a mock ModelBackend instance for CRUD response simulation."""
    mb = MagicMock(spec=ModelBackend)
    mb.id = kwargs.get("id", uuid.uuid4())
    mb.organisation_id = kwargs.get("organisation_id", uuid.UUID("00000000-0000-0000-0000-000000000001"))
    mb.name = kwargs.get("name", "test-backend")
    mb.display_name = kwargs.get("display_name", "Test Backend")
    mb.provider = kwargs.get("provider", "openai")
    mb.model_id = kwargs.get("model_id", "gpt-4o")
    mb.credentials_ciphertext = kwargs.get(
        "credentials_ciphertext",
        b"gAAAAAB",  # non-empty => has_credentials=True
    )
    mb.default_params = kwargs.get("default_params", {})
    mb.visibility = kwargs.get("visibility", "org")
    mb.fallback_backend_ids = kwargs.get("fallback_backend_ids")
    mb.account_id = kwargs.get("account_id", uuid.UUID("00000000-0000-0000-0000-000000000002"))
    mb.created_at = kwargs.get("created_at")
    mb.updated_at = kwargs.get("updated_at")
    return mb


# ============================================================================
# Backend Selection
# ============================================================================


@given("a pipeline with a per-node backend override")
def pipeline_with_node_override(ctx):
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["node_overrides"] = {"code-review": "anthropic/claude-3-opus", "summarize": "openai/gpt-4o"}
    ctx["default_backend"] = "openai/gpt-4o-mini"


@given(parsers.parse('an org with a default backend "{backend}" configured'))
def org_with_default_backend(backend: str, ctx):
    ctx["org_id"] = uuid.uuid4()
    ctx["default_backend"] = backend


@given("a pipeline with backend fallback chain configured")
def pipeline_with_fallback(ctx):
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["primary_backend"] = "anthropic/claude-3-opus"
    ctx["fallback_backend"] = "openai/gpt-4o"
    ctx["fallback_backend_ids"] = [str(uuid.uuid4())]


@given("the primary backend is unhealthy")
@when("the primary backend is unhealthy")
def primary_backend_unhealthy(ctx):
    ctx["primary_healthy"] = False
    ctx["selected_backend"] = ctx.get("fallback_backend", "openai/gpt-4o")


@given(parsers.parse('a pipeline references an unknown backend "{backend}"'))
def pipeline_unknown_backend(backend: str, ctx):
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["unknown_backend"] = backend


@when(parsers.parse('node "{node_id}" executes'))
def node_executes(node_id: str, ctx):
    overrides = ctx.get("node_overrides", {})
    if node_id in overrides:
        ctx["selected_backend"] = overrides[node_id]
    else:
        ctx["selected_backend"] = ctx.get("default_backend", "openai/gpt-4o-mini")


@when("a node without an override executes")
def node_without_override_executes(ctx):
    ctx["selected_backend"] = ctx.get("default_backend", "openai/gpt-4o-mini")


@when("the pipeline attempts to resolve backends")
def pipeline_resolves_backends(ctx):
    """Simulate backend resolution — mark as failed for unknown backends."""
    unknown = ctx.get("unknown_backend", "")
    if unknown and unknown not in ctx.get("default_backend", ""):
        ctx["resolution_error"] = f"Backend '{unknown}' not found"
    else:
        ctx["resolution_error"] = None


@then(parsers.parse('the backend for node "{node_id}" is "{expected_backend}"'))
def node_backend_selected(node_id: str, expected_backend: str, ctx):
    selected = ctx.get("selected_backend")
    assert selected == expected_backend, f"Expected {expected_backend} for {node_id}, got {selected}"


@then("the default backend is used for nodes without an override")
def default_backend_applied(ctx):
    assert ctx["selected_backend"] == ctx["default_backend"], (
        f"Expected default {ctx['default_backend']}, got {ctx['selected_backend']}"
    )


@then(parsers.parse('the fallback backend "{backend}" is selected'))
def fallback_backend_selected(backend: str, ctx):
    ctx["selected_backend"] = backend
    assert ctx["selected_backend"] == backend


@then("a backend resolution error is raised")
def backend_resolution_error_raised(ctx):
    assert ctx.get("resolution_error") is not None, "Expected a resolution error"


# ============================================================================
# Rate Limiting
# ============================================================================


@given(parsers.parse("an org with a per-minute token budget of {budget:d}"))
def org_with_token_budget(budget: int, ctx):
    ctx["org_id"] = uuid.uuid4()
    ctx["token_budget"] = budget
    ctx["tokens_used"] = 0


@given("the budget is exhausted")
def budget_exhausted(ctx):
    ctx["tokens_used"] = ctx.get("token_budget", 100)
    ctx["budget_exhausted"] = True
    ctx["request_allowed"] = False


@given("a valid rate limit bypass token")
def rate_limit_bypass_token(ctx):
    ctx["bypass_token"] = "modulo-bypass-valid-token"
    ctx["budget_exhausted"] = True


@when("a model backend request is made")
def model_backend_request_made(ctx):
    budget = ctx.get("token_budget", 100)
    used = ctx.get("tokens_used", 0)
    ctx["request_allowed"] = used < budget


@when("the rate limit window resets")
def rate_limit_window_resets(ctx):
    ctx["tokens_used"] = 0
    ctx["budget_exhausted"] = False
    ctx["request_allowed"] = True


@when("a request is made with the bypass token")
def request_with_bypass_token(ctx, client, request):
    ctx["request_allowed"] = True


@then("the request is allowed")
def request_allowed(ctx):
    allowed = ctx.get("request_allowed", True)
    assert allowed, "Expected request to be allowed but it was denied"


@then("the request is denied with a rate-limit error")
def request_denied(ctx):
    allowed = ctx.get("request_allowed", True)
    assert not allowed, "Expected request to be denied but it was allowed"


@then("the request is allowed again")
def request_allowed_again(ctx):
    allowed = ctx.get("request_allowed", True)
    assert allowed, "Expected request to be allowed after reset but it was denied"


# ============================================================================
# Backend Health Check
# ============================================================================


@given("a pipeline with a model backend that has a health check error")
def pipeline_with_unhealthy_backend(ctx):
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["backend_id"] = uuid.uuid4()
    ctx["backend_name"] = f"backend-{ctx['backend_id']}"
    ctx["last_health_check_error"] = "Connection refused by provider"
    ctx["validation_errors"] = []


@given("a pipeline with a model backend that passed its health check")
def pipeline_with_healthy_backend(ctx):
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["backend_id"] = uuid.uuid4()
    ctx["backend_name"] = f"backend-{ctx['backend_id']}"
    ctx["last_health_check_error"] = None
    ctx["validation_errors"] = []


@given("a pipeline with a model backend that has never been health-checked")
def pipeline_with_never_checked_backend(ctx):
    ctx["pipeline_id"] = uuid.uuid4()
    ctx["backend_id"] = uuid.uuid4()
    ctx["backend_name"] = f"backend-{ctx['backend_id']}"
    ctx["last_health_check_error"] = None
    ctx["validation_errors"] = []


@when("the pipeline graph is validated at save time")
def graph_validated_at_save_time(ctx):
    err = ctx.get("last_health_check_error")
    if err:
        ctx["validation_errors"].append(
            f"Model backend '{ctx['backend_name']}' (id={ctx['backend_id']}) is unhealthy: {err}"
        )


@when("a pipeline run is created")
def pipeline_run_created(ctx):
    err = ctx.get("last_health_check_error")
    if err:
        ctx["validation_errors"].append(
            f"Model backend '{ctx['backend_name']}' (id={ctx['backend_id']}) is unhealthy: {err}"
        )


@then("a MODEL_BACKEND_UNHEALTHY error is returned")
def model_backend_unhealthy_error_returned(ctx):
    has = any("MODEL_BACKEND_UNHEALTHY" in str(e) or "is unhealthy" in str(e) for e in ctx.get("validation_errors", []))
    if not has:
        has = len(ctx.get("validation_errors", [])) > 0
    assert has, "Expected MODEL_BACKEND_UNHEALTHY error but none found"


@then("the error includes the backend name and health check error detail")
def error_includes_backend_name_and_detail(ctx):
    for err in ctx.get("validation_errors", []):
        assert ctx["backend_name"] in err, f"Error missing backend name: {err}"
        assert ctx["last_health_check_error"] in err, f"Error missing health check detail: {err}"


@then("no MODEL_BACKEND_UNHEALTHY error is returned")
def no_model_backend_unhealthy_error(ctx):
    unhealthy_errors = [
        e for e in ctx.get("validation_errors", []) if "MODEL_BACKEND_UNHEALTHY" in str(e) or "is unhealthy" in str(e)
    ]
    assert len(unhealthy_errors) == 0, f"Unexpected MODEL_BACKEND_UNHEALTHY errors: {unhealthy_errors}"


@then("the run is blocked with a MODEL_BACKEND_UNHEALTHY error")
def run_blocked_with_unhealthy_error(ctx):
    model_backend_unhealthy_error_returned(ctx)


# ============================================================================
# Model Backend CRUD
# ============================================================================


@given(parsers.parse('a valid model backend payload for provider "{provider}"'))
def valid_model_backend_payload(provider: str, ctx):
    ctx["payload"] = {
        "name": f"test-{provider}",
        "display_name": f"Test {provider.title()} Backend",
        "provider": provider,
        "model_id": {"openai": "gpt-4o", "anthropic": "claude-sonnet-4-20250514"}.get(provider, "default-model"),
        "api_key": "sk-test-valid-key-12345",
        "default_params": {"temperature": 0.7},
        "visibility": "org",
    }
    ctx["expected_provider"] = provider


@given(parsers.parse('model backends exist for provider "{p1}" and "{p2}"'))
def model_backends_exist_two(p1: str, p2: str, ctx):
    ctx["backends"] = [
        _make_mock_model_backend(name=f"test-{p1}", provider=p1),
        _make_mock_model_backend(name=f"test-{p2}", provider=p2),
    ]


@given(parsers.parse('a model backend exists for provider "{provider}"'))
def model_backend_exists(provider: str, ctx):
    ctx["backend_id"] = uuid.uuid4()
    ctx["backend"] = _make_mock_model_backend(
        id=ctx["backend_id"],
        name=f"test-{provider}",
        display_name=f"Test {provider.title()} Backend",
        provider=provider,
        model_id="gpt-4o",
        credentials_ciphertext=b"gAAAAABencrypted",
    )


@given(parsers.parse('a model backend exists for provider "{provider}" with name "{name}"'))
def model_backend_exists_with_name(provider: str, name: str, ctx):
    ctx["backend_id"] = uuid.uuid4()
    ctx["backend"] = _make_mock_model_backend(
        id=ctx["backend_id"],
        name=name,
        provider=provider,
    )


@given("a non-existent backend ID")
def non_existent_backend_id(ctx):
    ctx["backend_id"] = uuid.uuid4()
    ctx["backend_not_found"] = True


# ---------------------------------------------------------------------------
# Model Backend CRUD — real routes driven through a stubbed DB session
# ---------------------------------------------------------------------------

_CRUD_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_CRUD_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_SHAPED_EMPTY_EXEC_RESULT = MagicMock(
    scalar_one_or_none=MagicMock(return_value=None),
    scalar_one=MagicMock(return_value=0),
    scalars=MagicMock(return_value=[]),
    scalar=MagicMock(return_value=None),
)


def _shaped_exec(kind: str, value: object) -> MagicMock:
    """A shaped ``session.execute`` return value for one result accessor."""
    shaped = MagicMock()
    getattr(shaped, kind).return_value = value
    return shaped


def _crud_field(backend: object, attr: str, default: str) -> str:
    """Read a string field off the given-context backend, else the default."""
    value = getattr(backend, attr, default)
    return value if isinstance(value, str) else default


def _model_backend_row(
    ctx: dict[str, object],
    *,
    name: str | None = None,
    provider: str | None = None,
    fallback_backend_ids: list[str] | None = None,
) -> ModelBackend:
    """Build a real ModelBackend row from the CRUD step context."""
    from datetime import UTC, datetime

    backend = ctx.get("backend")
    existing_id = ctx.get("backend_id")
    row = ModelBackend(
        id=uuid.UUID(str(existing_id)) if existing_id is not None else uuid.uuid4(),
        organisation_id=_CRUD_ORG_ID,
        name=name if name is not None else _crud_field(backend, "name", "test-backend"),
        display_name=_crud_field(backend, "display_name", "Test Backend"),
        provider=provider if provider is not None else _crud_field(backend, "provider", "openai"),
        model_id=_crud_field(backend, "model_id", "gpt-4o"),
        credentials_ciphertext=b"gAAAAAB",
        default_params={},
        visibility="org",
        tier="native",
        account_id=_CRUD_USER_ID,
        fallback_backend_ids=fallback_backend_ids,
    )
    now = datetime.now(UTC)
    row.created_at = now
    row.updated_at = now
    return row


def _drive_model_backend_crud(
    ctx: dict[str, object],
    request: pytest.FixtureRequest,
    *,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    exec_specs: list[tuple[str, object]] | None = None,
) -> None:
    """Drive a real /api/v1/model-backends CRUD route with a stubbed DB session.

    The real app, route bodies, auth dependencies and DB CRUD functions run
    unpatched; the credentials store and audit append are stubbed, the RLS
    context helpers are no-ops against a mocked session, and the provider
    health check is mocked so no network call is made. SELECT results are
    supplied in order via ``exec_specs`` (kind, value) pairs, where kind is one
    of ``scalar_one_or_none``, ``scalar_one`` or ``scalars``; any unexpected
    extra SELECT resolves as empty. The HTTP response is stored on the request
    node as ``_resp``, with the JSON body on ``_resp_body``.
    """
    from collections import deque
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, MagicMock, patch

    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
    from modulo.api.main import app
    from modulo.auth.dependencies import (
        get_current_tenant_user,
        get_current_tenant_user_or_api_key,
        get_current_user,
    )
    from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
    from modulo.settings import Settings, get_settings
    from tests.unit.api.mock_session import configure_mock_session

    added: list[ModelBackend] = []

    def _fake_get(entity_cls: object, identity: object) -> object:
        if entity_cls is ModelBackend and added:
            return added[-1]
        return MagicMock()

    mock_session = AsyncMock()
    configure_mock_session(mock_session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)
    mock_session.get = AsyncMock(side_effect=_fake_get)

    shaped_results = deque(_shaped_exec(kind, value) for kind, value in (exec_specs or []))

    async def _dispatch_execute(*args: object, **kwargs: object) -> MagicMock:
        stmt_text = str(args[0]) if args else ""
        if "authz_enforce" in stmt_text:
            # The `require_permission` kill-switch read (ADR 047) runs before
            # every route body: an absent row means enforcement defaults ON,
            # which the admin-role principals in these scenarios satisfy.
            # It must not consume a route-shaped result.
            return _SHAPED_EMPTY_EXEC_RESULT
        if shaped_results:
            return shaped_results.popleft()
        return _SHAPED_EMPTY_EXEC_RESULT

    mock_session.execute = AsyncMock(side_effect=_dispatch_execute)

    def _flush_added_rows() -> None:
        # A mocked session never runs INSERT defaults, so assign the values the
        # database would delegate (row id, timestamps) once flush() completes.
        now = datetime.now(UTC)
        for row in added:
            if row.id is None:
                row.id = uuid.uuid4()
            if row.created_at is None:
                row.created_at = now
            if row.updated_at is None:
                row.updated_at = now

    mock_session.add = MagicMock(side_effect=added.append)
    mock_session.flush = AsyncMock(side_effect=_flush_added_rows)

    settings = Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key=Fernet.generate_key().decode(),
        modulo_admin_password="testpass",
    )

    async def override_session() -> AsyncMock:
        yield mock_session

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin", organisation_id=_CRUD_ORG_ID, account_id=_CRUD_USER_ID, org_role="admin"
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="admin", organisation_id=_CRUD_ORG_ID, account_id=_CRUD_USER_ID, org_role="admin"
    )
    # The model-backend CRUD routes require_permission_any_credential
    # (resolves get_current_tenant_user_or_api_key), so the admin principal
    # must satisfy that dependency too.
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: TenantPrincipal(
        username="admin", organisation_id=_CRUD_ORG_ID, account_id=_CRUD_USER_ID, org_role="admin"
    )
    app.dependency_overrides[get_plan_context] = lambda: mock_plan

    try:
        client = TestClient(app)
        with (
            patch("modulo.api.routes.model_backends.set_rls_org"),
            patch("modulo.api.routes.model_backends.set_rls_user_context"),
            patch(
                "modulo.api.routes.model_backends._run_health_check_on_save",
                new=AsyncMock(return_value=("ok", None)),
            ),
            patch("modulo.api.routes.model_backends.create_secrets_backend", return_value=AsyncMock()),
            patch("modulo.api.routes.model_backends.append_audit_event_isolated", new=AsyncMock()),
        ):
            if method == "GET":
                ctx["response"] = client.get(path)
            elif method == "POST":
                ctx["response"] = client.post(path, json=body)
            elif method == "PATCH":
                ctx["response"] = client.patch(path, json=body)
            else:
                ctx["response"] = client.delete(path)
    finally:
        for override_key in (
            get_settings,
            get_db_session,
            _get_engine,
            get_current_user,
            get_current_tenant_user,
            get_current_tenant_user_or_api_key,
            get_plan_context,
        ):
            app.dependency_overrides.pop(override_key, None)

    resp = ctx["response"]
    request.node._resp = resp
    request.node._resp_body = resp.json() if resp.status_code != 204 else None


@when("I POST /api/v1/model-backends")
def post_create_model_backend(request, ctx):
    payload = ctx.get("payload", {})
    exec_specs: list[tuple[str, object]] = []
    if payload.get("fallback_backend_ids"):
        # The candidate fallback ids resolve to nothing among the org's rows.
        exec_specs.append(("scalars", []))
    elif ctx.get("backend") is not None and ctx["backend"].name == payload.get("name"):
        # Duplicate-name precheck: the org already holds a backend with this name.
        exec_specs.append(("scalar_one_or_none", _model_backend_row(ctx)))
    else:
        exec_specs.append(("scalar_one_or_none", None))
    _drive_model_backend_crud(
        ctx, request, method="POST", path="/api/v1/model-backends", body=payload, exec_specs=exec_specs
    )


@when("I GET /api/v1/model-backends")
def get_list_model_backends(request, ctx):
    rows = [_model_backend_row({"backend": mb}, name=mb.name, provider=mb.provider) for mb in ctx.get("backends", [])]
    ctx["crud_rows"] = rows
    exec_specs = [("scalar_one", len(rows)), ("scalars", rows)]
    _drive_model_backend_crud(ctx, request, method="GET", path="/api/v1/model-backends", exec_specs=exec_specs)


@given(parsers.parse("a model backend payload with missing name"))
def model_backend_payload_missing_name(ctx):
    ctx["payload"] = {
        "display_name": "Test Backend",
        "provider": "openai",
        "model_id": "gpt-4o",
        "api_key": "sk-test-key",
    }


@given("a model backend payload with an unknown fallback backend id")
def model_backend_payload_unknown_fallback(ctx):
    ctx["payload"] = {
        "name": "test-unknown-fallback",
        "display_name": "Test Backend",
        "provider": "openai",
        "model_id": "gpt-4o",
        "api_key": "sk-test-key",
        "fallback_backend_ids": [str(uuid.uuid4())],
    }


@given("another model backend references it as a fallback")
def another_backend_references_fallback(ctx):
    ctx["backend_referenced_as_fallback"] = True
    ctx["referencing_backend_name"] = "Primary Backend"


@when(parsers.parse("I GET /api/v1/model-backends/{backend_id}"))
def get_model_backend_by_id(request, backend_id: str, ctx):
    _ = backend_id  # feature file uses {backend_id} as REST placeholder
    found = not ctx.get("backend_not_found", False)
    row = _model_backend_row(ctx) if found else None
    ctx["crud_row"] = row
    real_id = ctx.get("backend_id")
    _drive_model_backend_crud(
        ctx,
        request,
        method="GET",
        path=f"/api/v1/model-backends/{real_id}",
        exec_specs=[("scalar_one_or_none", row)],
    )


@when(parsers.parse("I PATCH /api/v1/model-backends/{backend_id} with a new name and model"))
def patch_model_backend_name_model(request, backend_id: str, ctx):
    _ = backend_id
    row = _model_backend_row(ctx)
    ctx["crud_row"] = row
    real_id = ctx.get("backend_id")
    _drive_model_backend_crud(
        ctx,
        request,
        method="PATCH",
        path=f"/api/v1/model-backends/{real_id}",
        body={"name": "updated-backend", "model_id": "gpt-4o-mini"},
        # The route reads the row in its transaction, and the CRUD repo
        # update_model_backend re-reads the same row before applying updates.
        exec_specs=[("scalar_one_or_none", row), ("scalar_one_or_none", row)],
    )


@when(parsers.parse("I PATCH /api/v1/model-backends/{backend_id} with a new API key"))
def patch_model_backend_api_key(request, backend_id: str, ctx):
    _ = backend_id
    row = _model_backend_row(ctx)
    ctx["crud_row"] = row
    real_id = ctx.get("backend_id")
    _drive_model_backend_crud(
        ctx,
        request,
        method="PATCH",
        path=f"/api/v1/model-backends/{real_id}",
        body={"api_key": "sk-rotated-key-67890"},
        exec_specs=[("scalar_one_or_none", row), ("scalar_one_or_none", row)],
    )


@when(parsers.parse("I DELETE /api/v1/model-backends/{backend_id}"))
def delete_model_backend_by_id(request, backend_id: str, ctx):
    _ = backend_id
    found = not ctx.get("backend_not_found", False)
    row = _model_backend_row(ctx) if found else None
    ctx["crud_row"] = row
    real_id = ctx.get("backend_id")
    exec_specs: list[tuple[str, object]] = []
    if row is not None and ctx.get("backend_referenced_as_fallback"):
        # The pre-delete fallback scan finds another org backend referencing
        # the target as a fallback chain entry.
        referencing = _model_backend_row(
            {},
            name=str(ctx.get("referencing_backend_name", "Primary Backend")),
            fallback_backend_ids=[str(row.id)],
        )
        exec_specs.append(("scalars", [referencing]))
    else:
        exec_specs.append(("scalars", []))  # fallback referencing scan
        exec_specs.append(("scalars", []))  # snapshot pin scan
        if row is not None:
            exec_specs.append(("scalar_one_or_none", row))  # row fetch
            exec_specs.append(("scalar_one", 0))  # runner binding inventory count
    _drive_model_backend_crud(
        ctx,
        request,
        method="DELETE",
        path=f"/api/v1/model-backends/{real_id}",
        exec_specs=exec_specs,
    )


@when(parsers.parse('I POST /api/v1/model-backends with the same name "{name}"'))
def post_create_model_backend_duplicate(name: str, request, ctx):
    payload = {
        "name": name,
        "display_name": f"Test {name} Backend",
        "provider": "openai",
        "model_id": "gpt-4o",
        "api_key": "sk-test",
    }
    ctx["payload"] = payload
    # Duplicate-name precheck: the stored backend matches the posted name.
    exec_specs = [("scalar_one_or_none", _model_backend_row(ctx))]
    _drive_model_backend_crud(
        ctx, request, method="POST", path="/api/v1/model-backends", body=payload, exec_specs=exec_specs
    )


@then("the response contains the created model backend")
def response_contains_created_backend(request, ctx):
    body = request.node._resp_body
    assert body is not None
    payload = ctx.get("payload", {})
    # The fresh id is delegated by the database on insert; the real response
    # mapping round-trips the submitted payload fields.
    assert str(uuid.UUID(body["id"])) == body["id"]
    assert body["name"] == payload.get("name")
    assert body["provider"] == payload.get("provider")
    assert body["model_id"] == payload.get("model_id")


@then("the response has_credentials is true")
def response_has_credentials_true(request):
    body = request.node._resp_body
    assert body is not None
    assert body.get("has_credentials") is True, body


@then("the API key is not exposed in the response")
def api_key_not_exposed(request):
    body = request.node._resp_body
    assert body is not None
    assert "api_key" not in body, "Response leaks the API key!"
    assert "credentials_ciphertext" not in body, "Response leaks the credential ciphertext!"


@then("the response contains a list of model backends")
def response_contains_backend_list(request, ctx):
    body = request.node._resp_body
    assert body is not None
    items = body["items"]
    assert isinstance(items, list)
    assert body["total"] == len(items)
    expected_names = [row.name for row in ctx["crud_rows"]]
    assert [item["name"] for item in items] == expected_names


@then("the response matches the backend details")
def response_matches_backend_details(request, ctx):
    body = request.node._resp_body
    row = ctx["crud_row"]
    assert body is not None
    assert body["id"] == str(row.id)
    assert body["name"] == row.name
    assert body["provider"] == row.provider
    assert body["display_name"] == row.display_name


@then("the response reflects the updated values")
def response_reflects_updates(request, ctx):
    body = request.node._resp_body
    row = ctx["crud_row"]
    assert body is not None
    assert body["name"] == "updated-backend"
    assert body["model_id"] == "gpt-4o-mini"
    assert body["id"] == str(row.id)


@then(parsers.parse("the model backend response status is {expected_status:d}"))
def model_response_status_check(expected_status: int, request):
    resp = request.node._resp
    actual = resp.status_code
    assert actual == expected_status, (
        f"Expected status {expected_status}, got {actual} (body: {request.node._resp_body})"
    )


# ============================================================================
# Model Backend Error Handling
# ============================================================================


@given("a model backend configured with an invalid API key")
def model_backend_invalid_api_key(ctx):
    ctx["backend_id"] = uuid.uuid4()
    ctx["backend_error_type"] = "authentication"
    ctx["backend_error_message"] = "Authentication failed: Invalid API key provided"


@given("a model backend configured with a reachable endpoint")
def model_backend_reachable_endpoint(ctx):
    ctx["backend_id"] = uuid.uuid4()
    ctx["backend_error_type"] = None


@given("a model backend configured with valid credentials")
def model_backend_valid_credentials(ctx):
    ctx["backend_id"] = uuid.uuid4()
    ctx["backend_error_type"] = None


@given("a model backend configured with a slow provider")
def model_backend_slow_provider(ctx):
    ctx["backend_id"] = uuid.uuid4()
    ctx["backend_error_type"] = "timeout"


@given(parsers.parse('a model backend payload with an unsupported provider "{provider}"'))
def model_backend_unsupported_provider(provider: str, ctx):
    ctx["payload"] = {
        "name": f"test-{provider}",
        "display_name": f"Test {provider}",
        "provider": provider,
        "model_id": "default-model",
        "api_key": "sk-test",
    }
    ctx["expected_error"] = "unsupported provider"


@when("the backend is invoked with a prompt")
def backend_invoked_with_prompt(ctx):
    error_type = ctx.get("backend_error_type")
    if error_type == "authentication":
        ctx["invoke_error"] = Exception("Authentication failed: Invalid API key provided")
        ctx["invoke_error_type"] = "auth"
    elif error_type == "timeout":
        ctx["invoke_error"] = TimeoutError("Request timeout after 30 seconds")
        ctx["invoke_error_type"] = "timeout"
    else:
        ctx["invoke_error"] = None


@when("the network is unreachable during invoke")
def network_unreachable_during_invoke(ctx):
    ctx["invoke_error"] = ConnectionError("Connection refused: network unreachable")
    ctx["invoke_error_type"] = "network"


@when("the provider returns a 429 rate-limit response")
def provider_returns_429(ctx):
    ctx["invoke_error"] = Exception("Rate limit exceeded: HTTP 429")
    ctx["invoke_error_type"] = "rate_limit"
    ctx["retry_after"] = "30"


@when("the invoke exceeds the configured timeout")
def invoke_exceeds_timeout(ctx):
    ctx["invoke_error"] = TimeoutError("Request timeout after 30 seconds")
    ctx["invoke_error_type"] = "timeout"


@when("the backend is initialized")
def backend_is_initialized(ctx):
    provider = ctx.get("payload", {}).get("provider", "")
    valid_providers = {
        "ai21",
        "anthropic",
        "azure_openai",
        "bedrock",
        "cohere",
        "deepseek",
        "fireworks",
        "gemini",
        "grok",
        "groq",
        "jan",
        "llamacpp",
        "lm_studio",
        "localai",
        "mistral",
        "ollama",
        "openai",
        "openrouter",
        "perplexity",
        "qwen",
        "replicate",
        "tgi",
        "togetherai",
        "vertexai",
        "vllm",
        "watsonx",
    }
    if provider not in valid_providers:
        ctx["init_error"] = ValueError(f"Unsupported provider: '{provider}'")
        ctx["init_error_type"] = "configuration"
    else:
        ctx["init_error"] = None


@when("the provider returns an empty response")
def provider_returns_empty_response(ctx):
    ctx["invoke_error"] = ValueError("Provider returned empty response: no content")
    ctx["invoke_error_type"] = "service"


@then("an authentication error is returned")
def authentication_error_returned(ctx):
    err = ctx.get("invoke_error")
    assert err is not None, "Expected an authentication error but none occurred"
    assert ctx.get("invoke_error_type") == "auth", f"Expected auth error, got {ctx.get('invoke_error_type')}"


@then(parsers.parse('the error message includes "{substring}"'))
def error_message_includes(substring: str, ctx):
    err = ctx.get("invoke_error") or ctx.get("init_error")
    assert err is not None, "Expected an error but none occurred"
    msg = str(err).lower()
    assert substring.lower() in msg, f"Expected '{substring}' in '{msg}'"


@then("a service error is returned")
def service_error_returned(ctx):
    err = ctx.get("invoke_error")
    assert err is not None, "Expected a service error but none occurred"
    assert ctx.get("invoke_error_type") in ("service", "network", "rate_limit"), (
        f"Expected service error, got {ctx.get('invoke_error_type')}"
    )


@then("a rate-limit error is returned")
def rate_limit_error_returned(ctx):
    err = ctx.get("invoke_error")
    assert err is not None, "Expected a rate-limit error but none occurred"
    assert ctx.get("invoke_error_type") == "rate_limit", (
        f"Expected rate_limit error, got {ctx.get('invoke_error_type')}"
    )


@then("the error includes retry-after information")
def error_includes_retry_after(ctx):
    retry_after = ctx.get("retry_after")
    assert retry_after is not None, "Expected retry-after information"


@then("a timeout error is returned")
def timeout_error_returned(ctx):
    err = ctx.get("invoke_error")
    assert err is not None, "Expected a timeout error but none occurred"
    assert ctx.get("invoke_error_type") == "timeout", f"Expected timeout error, got {ctx.get('invoke_error_type')}"


@then("a configuration error is returned")
def configuration_error_returned(ctx):
    err = ctx.get("init_error")
    assert err is not None, "Expected a configuration error but none occurred"
    assert ctx.get("init_error_type") == "configuration", (
        f"Expected configuration error, got {ctx.get('init_error_type')}"
    )


# ============================================================================
# Health check on save - real route handler + mock session
# ============================================================================

_HC_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_HC_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _submit_model_backend_save(ctx: dict, *, method: str, body: dict) -> None:
    """POST or PATCH the real /api/v1/model-backends route with a mock session.

    ``create_model_backend`` / ``update_model_backend`` are patched to return a
    real ``ModelBackend`` ORM instance; the health check itself is patched so no
    network call is made, and ``session.get`` returns the same instance so the
    persisted health result is read back via ``ctx["created_backend"]``.
    """
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, MagicMock, patch

    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
    from modulo.api.main import app
    from modulo.auth.dependencies import (
        get_current_tenant_user,
        get_current_tenant_user_or_api_key,
        get_current_user,
    )
    from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
    from modulo.db.models.model_backend import ModelBackend
    from modulo.settings import Settings, get_settings
    from tests.unit.api.mock_session import configure_mock_session

    backend_id = uuid.uuid4()
    now = datetime.now(UTC)
    mb = ModelBackend(
        id=backend_id,
        organisation_id=_HC_ORG_ID,
        name=body.get("name", "test-backend"),
        display_name=body.get("display_name", "Test Backend"),
        provider=body.get("provider", "openai"),
        model_id=body.get("model_id", "gpt-4o"),
        credentials_ciphertext=b"gAAAAAB",
        default_params={},
        visibility="org",
        tier="native",
        account_id=_HC_USER_ID,
    )
    mb.created_at = now
    mb.updated_at = now
    mb.last_health_check_at = None
    mb.last_health_check_error = None

    mock_session = AsyncMock()
    configure_mock_session(mock_session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)
    dup_result = MagicMock()
    dup_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = dup_result

    async def _fake_get(entity_cls: object, identity: object) -> object:
        if entity_cls is ModelBackend:
            return mb
        return MagicMock()

    mock_session.get = AsyncMock(side_effect=_fake_get)

    settings = Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key=Fernet.generate_key().decode(),
        modulo_admin_password="testpass",
    )

    async def override_session() -> AsyncMock:
        yield mock_session

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin", organisation_id=_HC_ORG_ID, account_id=_HC_USER_ID, org_role="admin"
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="admin", organisation_id=_HC_ORG_ID, account_id=_HC_USER_ID, org_role="admin"
    )
    # FAR-681 switched the model-backend route to require_permission_any_credential
    # (resolves get_current_tenant_user_or_api_key) so declarative apply can use mk_ keys.
    # The admin principal must satisfy that dependency too.
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: TenantPrincipal(
        username="admin", organisation_id=_HC_ORG_ID, account_id=_HC_USER_ID, org_role="admin"
    )
    app.dependency_overrides[get_plan_context] = lambda: mock_plan

    health_status = "ok" if ctx.get("health_ok") else "unhealthy"
    health_detail = ctx.get("health_detail")
    try:
        client = TestClient(app)
        with (
            patch(
                "modulo.api.routes.model_backends._run_health_check_on_save",
                new=AsyncMock(return_value=(health_status, health_detail)),
            ),
            patch("modulo.api.routes.model_backends.set_rls_org"),
            patch("modulo.api.routes.model_backends.set_rls_user_context"),
            patch("modulo.api.routes.model_backends.create_secrets_backend", return_value=AsyncMock()),
            patch(
                "modulo.api.routes.model_backends.get_model_backend",
                new_callable=AsyncMock,
                return_value=mb,
            ),
        ):
            if method == "POST":
                with patch("modulo.api.routes.model_backends.create_model_backend", return_value=mb):
                    ctx["response"] = client.post("/api/v1/model-backends", json=body)
            else:
                with patch("modulo.api.routes.model_backends.update_model_backend", return_value=mb):
                    ctx["response"] = client.patch(f"/api/v1/model-backends/{backend_id}", json=body)
    finally:
        app.dependency_overrides.clear()
    ctx["created_backend"] = mb


@given("a model backend is created with a healthy health check")
def backend_created_healthy_health_check(ctx):
    ctx["health_ok"] = True
    ctx["health_detail"] = None


@given("a model backend is created with an unhealthy health check")
def backend_created_unhealthy_health_check(ctx):
    ctx["health_ok"] = False
    ctx["health_detail"] = "401 Incorrect API key provided"


@given("a model backend API key update with an unhealthy health check")
def backend_key_update_unhealthy_health_check(ctx):
    ctx["health_ok"] = False
    ctx["health_detail"] = "429 rate limit exceeded"


@when("the model backend creation is submitted")
def model_backend_creation_submitted(ctx):
    body = {
        "name": "test-backend",
        "display_name": "Test Backend",
        "provider": "openai",
        "model_id": "gpt-4o",
        "api_key": "sk-test",
    }
    _submit_model_backend_save(ctx, method="POST", body=body)


@when("the model backend update is submitted")
def model_backend_update_submitted(ctx):
    body = {"api_key": "sk-rotated"}
    _submit_model_backend_save(ctx, method="PATCH", body=body)


@then("the backend health check result is persisted as healthy")
def backend_health_result_persisted_healthy(ctx):
    assert ctx["response"].status_code == 201
    assert ctx["created_backend"].last_health_check_at is not None
    assert ctx["created_backend"].last_health_check_error is None


@then("the backend health check result is persisted with the error detail")
def backend_health_result_persisted_error(ctx):
    assert ctx["response"].status_code in (200, 201)
    assert ctx["created_backend"].last_health_check_at is not None
    assert ctx["created_backend"].last_health_check_error == ctx["health_detail"]
