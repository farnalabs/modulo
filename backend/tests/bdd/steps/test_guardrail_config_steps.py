"""Step definitions for the Guardrail Config-as-Code Workflow (T3) feature.

Exercises the ``/api/v1/guardrails/config`` propose -> apply -> reject -> drift
endpoints through the shared TestClient harness. The DB-backed seams
(``get_guardrail_pin``/``set_guardrail_pin`` CRUD, live-row loading, and the
row reconciliation) are replaced with controllable doubles; the workflow logic
under test (YAML validation, content hashing, per-guardrail diff, pin status
transitions, drift recomputation, permissions, and 409/422/403 error paths)
runs for real in the route handlers.
"""

import contextlib
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.eval_engine import EvalDefinition, EvalType
from modulo.core.guardrails.config import hash_config_set, load_config_set, to_eval_config
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/evals/guardrail_config.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_BLOCK_YAML = """version: 1
guardrails:
  - id: no-secrets
    name: No Secrets
    action: block
    detection:
      type: regex
      pattern: "SECRET_[A-Z0-9]{8}"
      field: body
"""

_WARN_YAML = _BLOCK_YAML.replace("action: block", "action: warn")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
        modulo_csrf_enabled=False,
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _pin_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "applied_hash": None,
        "applied_at": None,
        "serialized_snapshot": None,
        "proposed_hash": None,
        "proposed_at": None,
        "serialized_proposal": None,
        "status": "clean",
    }
    base.update(overrides)
    return base


def _client(role: str | None) -> TestClient:
    app.dependency_overrides.pop(get_current_tenant_user, None)
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides[get_settings] = _make_settings

    session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    if role is not None:
        principal = TenantPrincipal(
            username=role,
            organisation_id=_ORG_ID,
            account_id=_ACCOUNT_ID,
            org_role=role,
        )
        app.dependency_overrides[get_current_user] = lambda: principal
        app.dependency_overrides[get_current_tenant_user] = lambda: principal
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    return TestClient(app)


def _clean_overrides() -> None:
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(_get_engine, None)
    app.dependency_overrides.pop(get_current_tenant_user, None)
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_plan_context, None)


def _definitions_from_yaml(yaml_text: str) -> list[EvalDefinition]:
    """Engine DTOs for the guardrail ids in *yaml_text* (one row per id)."""
    config_set = load_config_set(yaml_text)
    return [
        EvalDefinition(
            id=uuid.uuid4(),
            org_id=_ORG_ID,
            name=item.id,
            eval_type=EvalType.GUARDRAIL,
            config=to_eval_config(item),
            failure_behaviour="warn",
        )
        for item in config_set.guardrails
    ]


def _capture(request, method: str, url: str, role: str, json: Any = None) -> None:
    client = _client(role)
    try:
        resp = client.request(method, url, json=json) if json is not None else client.request(method, url)
    finally:
        for p in getattr(request.node, "_patchers", []):
            p.stop()
        _clean_overrides()
    request.node._resp = resp
    request.node.response = resp


# ============================================================================
# Given steps
# ============================================================================


@given('I am authenticated as an admin in org "acme"')
def _bdd_auth_admin() -> None:
    """No-op — the ``when`` steps build an admin-principal TestClient."""


@given('I am authenticated as a viewer in org "acme"')
def _bdd_auth_viewer(request) -> None:
    request.node._role = "viewer"


@given("there is no applied guardrail config")
def _bdd_no_applied(request) -> None:
    request.node._pin = None
    request.node._definitions = []


@given('a guardrail "no-secrets" was previously applied')
def _bdd_guardrail_applied(request) -> None:
    config_set = load_config_set(_BLOCK_YAML)
    request.node._pin = _pin_dict(
        applied_hash=hash_config_set(config_set),
        applied_at="2026-08-16T00:00:00+00:00",
        serialized_snapshot=_BLOCK_YAML,
        status="clean",
    )
    request.node._definitions = _definitions_from_yaml(_BLOCK_YAML)


@given("a pending guardrail proposal exists")
def _bdd_pending_proposal(request) -> None:
    request.node._pin = _pin_dict(
        status="proposed",
        proposed_hash=hash_config_set(load_config_set(_BLOCK_YAML)),
        proposed_at="2026-08-16T00:00:00+00:00",
        serialized_proposal=_BLOCK_YAML,
    )
    request.node._definitions = []


@given("a guardrail config was previously applied")
def _bdd_config_applied(request) -> None:
    request.node._pin = _pin_dict(
        applied_hash=hash_config_set(load_config_set(_BLOCK_YAML)),
        applied_at="2026-08-16T00:00:00+00:00",
        serialized_snapshot=_BLOCK_YAML,
        status="clean",
    )
    request.node._definitions = _definitions_from_yaml(_BLOCK_YAML)


@given("the live guardrail rows match the applied pin")
def _bdd_rows_match(request) -> None:
    request.node._definitions = _definitions_from_yaml(_BLOCK_YAML)


@given("the live guardrail rows diverge from the applied pin")
def _bdd_rows_diverge(request) -> None:
    request.node._definitions = _definitions_from_yaml(_WARN_YAML)


# ============================================================================
# When steps
# ============================================================================


@when(parsers.parse("I propose the guardrail config:"))
def _bdd_propose_block(request, docstring: str) -> None:
    role = getattr(request.node, "_role", None) or "admin"
    _patch_route(request)
    _capture(request, "POST", "/api/v1/guardrails/config/propose", role, json={"config_yaml": docstring})


@when(parsers.parse('I propose the guardrail config "{yaml_text}"'))
def _bdd_propose_inline(request, yaml_text: str) -> None:
    role = getattr(request.node, "_role", None) or "admin"
    _patch_route(request)
    _capture(request, "POST", "/api/v1/guardrails/config/propose", role, json={"config_yaml": yaml_text})


@when("I apply the guardrail config")
def _bdd_apply(request) -> None:
    _patch_route(request)
    _capture(request, "POST", "/api/v1/guardrails/config/apply", "admin")


@when("I reject the guardrail config")
def _bdd_reject(request) -> None:
    _patch_route(request)
    _capture(request, "POST", "/api/v1/guardrails/config/reject", "admin")


@when("I request the guardrail config drift")
def _bdd_drift(request) -> None:
    _patch_route(request)
    _capture(request, "GET", "/api/v1/guardrails/config/drift", "admin")


def _patch_route(request) -> None:
    """Replace the DB-backed seams with doubles driven by the scenario state."""
    pin = getattr(request.node, "_pin", None)
    definitions = getattr(request.node, "_definitions", [])

    reconcile_mock = AsyncMock(return_value=[])
    pin_patch = patch(
        "modulo.api.routes.guardrail_config.get_guardrail_pin",
        new=AsyncMock(return_value=pin),
    )
    set_pin_patch = patch(
        "modulo.api.routes.guardrail_config.set_guardrail_pin",
        new=AsyncMock(return_value=None),
    )
    defs_patch = patch(
        "modulo.api.routes.guardrail_config._load_guardrail_definitions",
        new=AsyncMock(return_value=definitions),
    )
    reconcile_patch = patch(
        "modulo.api.routes.guardrail_config._reconcile_guardrail_rows",
        new=reconcile_mock,
    )
    for p in (pin_patch, set_pin_patch, defs_patch, reconcile_patch):
        p.start()
    request.node._patchers = [pin_patch, set_pin_patch, defs_patch, reconcile_patch]
    request.node._reconcile_mock = reconcile_mock


# ============================================================================
# Then steps
# ============================================================================


@then("the proposal is accepted")
def _bdd_proposal_accepted(request) -> None:
    data = request.node._resp.json()
    assert data["proposed"] is True, f"Expected proposed=True, got {data}"
    assert data["status"] == "proposed"


@then("the proposal hash is a 64-character hex digest")
def _bdd_proposal_hash(request) -> None:
    data = request.node._resp.json()
    h = data["hash"]
    assert isinstance(h, str), f"Expected 64-char hex hash, got {h!r}"
    assert len(h) == 64, f"Expected 64-char hex hash, got {h!r}"
    int(h, 16)


@then(parsers.parse('the diff lists an "{action}" for guardrail "{gid}"'))
def _bdd_diff_has(request, action: str, gid: str) -> None:
    data = request.node._resp.json()
    changes = data["diff"]
    assert any(c["action"] == action and c["id"] == gid for c in changes), f"No {action} change for {gid}: {changes}"


@then("the apply reports a clean applied state")
def _bdd_apply_clean(request) -> None:
    data = request.node._resp.json()
    assert data["applied"] is True, f"Expected applied=True, got {data}"
    assert data["status"] == "clean"


@then("the guardrail rows were reconciled")
def _bdd_rows_reconciled(request) -> None:
    reconcile_mock = getattr(request.node, "_reconcile_mock", None)
    assert reconcile_mock is not None, "reconcile mock not installed"
    assert reconcile_mock.called, "reconcile not called"


@then("the reject reports a clean state")
def _bdd_reject_clean(request) -> None:
    data = request.node._resp.json()
    assert data["rejected"] is True, f"Expected rejected=True, got {data}"
    assert data["status"] == "clean"


@given("a guardrail config with a regex pattern was applied")
def _bdd_regex_config_applied(request) -> None:
    request.node._pin = _pin_dict(
        applied_hash=hash_config_set(load_config_set(_BLOCK_YAML)),
        applied_at="2026-08-16T00:00:00+00:00",
        serialized_snapshot=_BLOCK_YAML,
        status="clean",
    )
    request.node._definitions = _definitions_from_yaml(_BLOCK_YAML)


@when("I read the guardrail config as an operator")
def _bdd_read_config_operator(request) -> None:
    _patch_route(request)
    _capture(request, "GET", "/api/v1/guardrails/config", "operator")


@when("I read the elevated guardrail config as an admin")
def _bdd_read_elevated_admin(request) -> None:
    _patch_route(request)
    _capture(request, "GET", "/api/v1/guardrails/config/elevated", "admin")


@when("I read the elevated guardrail config as an operator")
def _bdd_read_elevated_operator(request) -> None:
    _patch_route(request)
    _capture(request, "GET", "/api/v1/guardrails/config/elevated", "operator")


@then("the response config masks the regex pattern")
def _bdd_config_masks_pattern(request) -> None:
    yaml_text = request.node._resp.json()["config_yaml"]
    assert "AKIA" not in yaml_text, "masked read must not leak the real regex pattern"
    assert "SECRET_[A-Z0-9]{8}" not in yaml_text, "masked read must not leak the real regex pattern"
    assert "********" in yaml_text, "masked read must show the redaction mask"


@then("the response config shows the real regex pattern")
def _bdd_config_shows_pattern(request) -> None:
    yaml_text = request.node._resp.json()["config_yaml"]
    assert "SECRET_[A-Z0-9]{8}" in yaml_text, "elevated read must show the real regex pattern"


@then(parsers.parse('the drift response reports "{status}"'))
def _bdd_drift_status(request, status: str) -> None:
    data = request.node._resp.json()
    assert data["status"] == status, f"Expected drift {status!r}, got {data}"
