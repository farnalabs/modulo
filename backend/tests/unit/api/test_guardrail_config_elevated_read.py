"""Unit tests for the FAR-309 PR A elevated guardrail-config read.

The standard ``GET /api/v1/guardrails/config`` (``eval.list``, viewer-visible)
must NOT leak the deny-rule internals â€” regex patterns, JSON schemas, and
redaction field paths are masked. The elevated ``GET
/api/v1/guardrails/config/elevated`` requires ``guardrail.manage`` (admin) and
returns the FULL unmasked config. Non-admins are denied 403 on the elevated
endpoint.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.guardrails.config import GuardrailPin
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_CONFIG_YAML = """
version: 1
guardrails:
  - id: no-aws-keys
    name: Block AWS keys
    action: block
    detection:
      type: regex
      pattern: 'AKIA[0-9A-Z]{16}'
      field: body
    redaction:
      - path: body
        mode: transform
  - id: valid-payload
    name: Require valid payload
    action: observe
    detection:
      type: json_schema
      schema:
        type: object
        properties:
          body:
            type: string
"""


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _mock_session() -> AsyncMock:
    from tests.unit.api.mock_session import configure_mock_session

    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
    return session


def _pin() -> GuardrailPin:
    return GuardrailPin(
        org_id=_ORG_ID,
        applied_hash="applied-hash",
        applied_at="2026-01-01T00:00:00+00:00",
        serialized_snapshot=_CONFIG_YAML,
        status="clean",
    )


def _proposed_pin() -> GuardrailPin:
    """A pin with a PENDING proposal â€” the proposal is what the operator is
    reviewing, so the standard read must mask it and the elevated read must
    return it unmasked."""
    return GuardrailPin(
        org_id=_ORG_ID,
        applied_hash="applied-hash",
        applied_at="2026-01-01T00:00:00+00:00",
        serialized_snapshot="version: 1\nguardrails: []\n",
        serialized_proposal=_CONFIG_YAML,
        status="proposed",
    )


@pytest.fixture
def admin_client() -> TestClient:
    return _make_client(org_role="admin")


@pytest.fixture
def operator_client() -> TestClient:
    return _make_client(org_role="operator")


@pytest.fixture
def runner_client() -> TestClient:
    return _make_client(org_role="runner")


@pytest.fixture
def viewer_client() -> TestClient:
    return _make_client(org_role="viewer")


def _make_client(*, org_role: str) -> TestClient:
    mock_session = _mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username=f"{org_role}@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_overrides():
    yield
    app.dependency_overrides.clear()


@contextmanager
def _patch_route_deps(pin: GuardrailPin | None = None) -> Generator[None, None, None]:
    """Patch the guardrail_config route's pin/definition reads and RLS setup."""
    with ExitStack() as stack:
        stack.enter_context(patch("modulo.api.routes.guardrail_config.set_rls_org"))
        stack.enter_context(
            patch(
                "modulo.api.routes.guardrail_config._load_pin",
                return_value=pin if pin is not None else _pin(),
            )
        )
        stack.enter_context(patch("modulo.api.routes.guardrail_config._load_guardrail_definitions", return_value=[]))
        stack.enter_context(patch("modulo.api.routes.guardrail_config.check_guardrail_drift", return_value=False))
        yield


def test_standard_read_masks_sensitive_values(runner_client: TestClient) -> None:
    """The standard read (``eval.list``, runner-visible â€” the LOWEST role that
    can reach it) exposes the guardrail topology but masks the deny-rule
    internals â€” the real regex pattern and JSON schema must never appear in the
    returned YAML."""
    with _patch_route_deps():
        resp = runner_client.get("/api/v1/guardrails/config")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "clean"
    yaml_text = body["config_yaml"]
    # Topology preserved.
    assert "no-aws-keys" in yaml_text
    assert "valid-payload" in yaml_text
    assert "block" in yaml_text
    # Sensitive internals masked.
    assert "AKIA[0-9A-Z]{16}" not in yaml_text
    assert '"properties"' not in yaml_text
    assert "********" in yaml_text


def test_standard_read_denied_for_viewer(viewer_client: TestClient) -> None:
    """Viewers cannot reach even the standard read (``eval.list`` requires
    runner) â€” the guardrail config is never exposed to the lowest role."""
    with _patch_route_deps():
        resp = viewer_client.get("/api/v1/guardrails/config")

    assert resp.status_code == 403


def test_standard_read_masks_pending_proposal(runner_client: TestClient) -> None:
    """A PENDING proposal is what a non-admin operator is reviewing â€” the
    standard read must mask its sensitive internals just like the applied
    snapshot (the pattern/schema must never appear)."""
    with _patch_route_deps(pin=_proposed_pin()):
        resp = runner_client.get("/api/v1/guardrails/config")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "proposed"
    yaml_text = body["config_yaml"]
    assert "no-aws-keys" in yaml_text
    assert "AKIA[0-9A-Z]{16}" not in yaml_text
    assert '"properties"' not in yaml_text
    assert "********" in yaml_text


def test_elevated_read_returns_full_unmasked_proposal_for_admin(admin_client: TestClient) -> None:
    """The elevated endpoint returns the PENDING PROPOSAL unmasked â€” the
    operator reviewing/approving it needs the actual rule bodies."""
    with _patch_route_deps(pin=_proposed_pin()):
        resp = admin_client.get("/api/v1/guardrails/config/elevated")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "proposed"
    yaml_text = body["config_yaml"]
    assert "AKIA[0-9A-Z]{16}" in yaml_text
    assert "properties" in yaml_text
    assert "********" not in yaml_text


def test_standard_read_masks_for_admins_too(admin_client: TestClient) -> None:
    """Even an admin hitting the STANDARD read gets the masked view â€” the
    unmasked config is only available via the elevated endpoint."""
    with _patch_route_deps():
        resp = admin_client.get("/api/v1/guardrails/config")

    assert resp.status_code == 200
    assert "AKIA[0-9A-Z]{16}" not in resp.json()["config_yaml"]


def test_elevated_read_returns_full_unmasked_config_for_admin(admin_client: TestClient) -> None:
    """An admin (``guardrail.manage``) gets the FULL config from the elevated
    endpoint â€” the actual regex pattern, JSON schema, and redaction paths."""
    with _patch_route_deps():
        resp = admin_client.get("/api/v1/guardrails/config/elevated")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "clean"
    yaml_text = body["config_yaml"]
    assert "AKIA[0-9A-Z]{16}" in yaml_text
    assert "properties" in yaml_text
    assert "body" in yaml_text
    assert "********" not in yaml_text


def test_elevated_read_denied_for_operator(operator_client: TestClient) -> None:
    """The elevated endpoint is admin-only (``guardrail.manage``): an operator
    is denied 403 before any config is served."""
    with _patch_route_deps():
        resp = operator_client.get("/api/v1/guardrails/config/elevated")

    assert resp.status_code == 403
    assert "guardrail.manage" in resp.json()["detail"]


def test_elevated_read_denied_for_viewer(viewer_client: TestClient) -> None:
    with _patch_route_deps():
        resp = viewer_client.get("/api/v1/guardrails/config/elevated")

    assert resp.status_code == 403
    assert "guardrail.manage" in resp.json()["detail"]
