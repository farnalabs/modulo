"""Route-level tests for the guardrail config-as-code workflow (FAR-574).

Complements ``test_guardrail_config_elevated_read.py`` (masked vs elevated
reads) by covering the propose → apply/reject workflow, the drift endpoint and
its pin status transitions, the reconcile-collision 409, and the route error
convention paths.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.guardrails.config import GuardrailPin
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_BASE = "/api/v1/guardrails/config"

_VALID_YAML = """
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
"""


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _clean_pin() -> GuardrailPin:
    return GuardrailPin(
        org_id=_ORG_ID,
        applied_hash="applied-hash",
        applied_at="2026-01-01T00:00:00+00:00",
        serialized_snapshot="version: 1\nguardrails: []\n",
        status="clean",
    )


def _proposed_pin() -> GuardrailPin:
    return GuardrailPin(
        org_id=_ORG_ID,
        applied_hash="applied-hash",
        applied_at="2026-01-01T00:00:00+00:00",
        serialized_snapshot="version: 1\nguardrails: []\n",
        serialized_proposal=_VALID_YAML,
        proposed_hash="proposed-hash",
        proposed_at="2026-01-02T00:00:00+00:00",
        status="proposed",
    )


def _make_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    nested_cm = AsyncMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested_cm)
    return session


@contextmanager
def _patched(
    pin: GuardrailPin | None = None,
    pipelines: list[MagicMock] | None = None,
) -> Generator[dict[str, MagicMock], None, None]:
    """Patch the guardrail_config route's DB-facing collaborators.

    Yields a dict of the mocks that tests may inspect (keyed by collaborator
    name): ``get_guardrail_pin`` returns the given pin's stored dict (None
    when no pin), ``set_guardrail_pin`` captures pin persistence, and the
    pipeline/row readers are stubbed to the given pipelines.
    """
    mocks: dict[str, MagicMock] = {}
    with ExitStack() as stack:
        for name, target, kwargs in [
            ("set_rls_org", "set_rls_org", {"new_callable": AsyncMock}),
            ("set_rls_user_context", "set_rls_user_context", {"new_callable": AsyncMock}),
            (
                "get_guardrail_pin",
                "get_guardrail_pin",
                {
                    "new_callable": AsyncMock,
                    "return_value": pin.to_json() if pin is not None else None,
                },
            ),
            ("set_guardrail_pin", "set_guardrail_pin", {"new_callable": AsyncMock}),
            (
                "_load_guardrail_definitions",
                "_load_guardrail_definitions",
                {"new_callable": AsyncMock, "return_value": []},
            ),
            ("check_guardrail_drift", "check_guardrail_drift", {"return_value": False}),
            ("append_audit_event", "append_audit_event", {"new_callable": AsyncMock}),
            (
                "load_pipeline_guardrail_rows",
                "load_pipeline_guardrail_rows",
                {"new_callable": AsyncMock, "return_value": []},
            ),
        ]:
            mocks[name] = stack.enter_context(patch(f"modulo.api.routes.guardrail_config.{target}", **kwargs))
        yield mocks


@pytest.fixture
def mock_session() -> AsyncMock:
    return _make_session()


def _install_overrides(org_role: str, session: AsyncMock) -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=f"{org_role}@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username=f"{org_role}@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )


@pytest.fixture
def admin_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    _install_overrides("admin", mock_session)
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def operator_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    _install_overrides("operator", mock_session)
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /propose
# ---------------------------------------------------------------------------


def test_propose_stores_proposal_and_returns_diff(admin_client: TestClient) -> None:
    with _patched(pin=_clean_pin()):
        resp = admin_client.post(f"{_BASE}/propose", json={"config_yaml": _VALID_YAML})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["proposed"] is True
    assert body["status"] == "proposed"
    assert body["hash"]
    assert isinstance(body["diff"], list)
    add_entries = [c for c in body["diff"] if c["action"] == "add"]
    assert len(add_entries) == 1
    assert add_entries[0]["id"] == "no-aws-keys"


def test_propose_invalid_yaml_returns_422(admin_client: TestClient) -> None:
    with _patched(pin=None):
        resp = admin_client.post(f"{_BASE}/propose", json={"config_yaml": "not: [valid: yaml"})

    assert resp.status_code == 422


def test_propose_rejects_empty_yaml_via_validation(admin_client: TestClient) -> None:
    with _patched(pin=None):
        resp = admin_client.post(f"{_BASE}/propose", json={"config_yaml": ""})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /apply
# ---------------------------------------------------------------------------


def _pipeline_result(pipelines: list[MagicMock]) -> MagicMock:
    """Session.execute result returning the given pipeline rows for the reconcile."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = pipelines
    return result


def test_apply_moves_pin_to_clean(admin_client: TestClient, mock_session: AsyncMock) -> None:
    pipeline = MagicMock()
    pipeline.id = uuid.uuid4()
    mock_session.execute = AsyncMock(return_value=_pipeline_result([pipeline]))
    with _patched(pin=_proposed_pin()):
        resp = admin_client.post(f"{_BASE}/apply")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["status"] == "clean"
    assert body["hash"] == "proposed-hash"
    # The reconcile created one org-level guardrail row for the pipeline.
    assert mock_session.add.call_count == 1


def test_apply_without_proposal_returns_409(admin_client: TestClient) -> None:
    with _patched(pin=_clean_pin()):
        resp = admin_client.post(f"{_BASE}/apply")

    assert resp.status_code == 409
    assert "No guardrail config proposal" in resp.json()["detail"]


def test_apply_without_pin_returns_409(admin_client: TestClient) -> None:
    with _patched(pin=None):
        resp = admin_client.post(f"{_BASE}/apply")

    assert resp.status_code == 409


def test_apply_denied_for_operator(operator_client: TestClient) -> None:
    with _patched(pin=_proposed_pin()):
        resp = operator_client.post(f"{_BASE}/apply")

    assert resp.status_code == 403
    assert "Only admins" in resp.json()["detail"]


def test_apply_collision_with_node_bound_row_returns_409(admin_client: TestClient, mock_session: AsyncMock) -> None:
    pipeline = MagicMock()
    pipeline.id = uuid.uuid4()
    colliding_row = MagicMock()
    colliding_row.name = "no-aws-keys"
    colliding_row.node_id = uuid.uuid4()
    mock_session.execute = AsyncMock(return_value=_pipeline_result([pipeline]))
    with (
        _patched(pin=_proposed_pin()),
        patch(
            "modulo.api.routes.guardrail_config.load_pipeline_guardrail_rows",
            new_callable=AsyncMock,
            return_value=[colliding_row],
        ),
    ):
        resp = admin_client.post(f"{_BASE}/apply")

    assert resp.status_code == 409
    assert "no-aws-keys" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /reject
# ---------------------------------------------------------------------------


def test_reject_discards_proposal(admin_client: TestClient) -> None:
    with _patched(pin=_proposed_pin()):
        resp = admin_client.post(f"{_BASE}/reject")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"rejected": True, "status": "clean"}


def test_reject_without_proposal_returns_409(admin_client: TestClient) -> None:
    with _patched(pin=_clean_pin()):
        resp = admin_client.post(f"{_BASE}/reject")

    assert resp.status_code == 409
    assert "No guardrail config proposal to reject" in resp.json()["detail"]


def test_reject_denied_for_operator(operator_client: TestClient) -> None:
    with _patched(pin=_proposed_pin()):
        resp = operator_client.post(f"{_BASE}/reject")

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /drift
# ---------------------------------------------------------------------------


def test_drift_clean_pin_stays_clean(admin_client: TestClient) -> None:
    with _patched(pin=_clean_pin()):
        resp = admin_client.get(f"{_BASE}/drift")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "clean"
    assert body["applied_hash"] == "applied-hash"
    assert body["current_hash"]


def test_drift_transition_clean_to_drift_persists_pin(admin_client: TestClient) -> None:
    with (
        _patched(pin=_clean_pin()) as mocks,
        patch("modulo.api.routes.guardrail_config.check_guardrail_drift", return_value=True),
    ):
        resp = admin_client.get(f"{_BASE}/drift")

    assert resp.status_code == 200
    assert resp.json()["status"] == "drift"
    mocks["set_guardrail_pin"].assert_awaited_once()
    mocks["append_audit_event"].assert_awaited_once()


def test_drift_transition_drift_to_clean_restores_pin(admin_client: TestClient) -> None:
    drifted_pin = _clean_pin()
    drifted_pin.status = "drift"
    with _patched(pin=drifted_pin):
        resp = admin_client.get(f"{_BASE}/drift")

    assert resp.status_code == 200
    assert resp.json()["status"] == "clean"


def test_drift_proposed_pin_stays_proposed(admin_client: TestClient) -> None:
    with (
        _patched(pin=_proposed_pin()),
        patch("modulo.api.routes.guardrail_config.check_guardrail_drift", return_value=True),
    ):
        resp = admin_client.get(f"{_BASE}/drift")

    assert resp.status_code == 200
    assert resp.json()["status"] == "proposed"


def test_drift_without_pin_reports_clean(admin_client: TestClient) -> None:
    with _patched(pin=None):
        resp = admin_client.get(f"{_BASE}/drift")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "clean"
    assert body["applied_hash"] is None


def test_drift_config_error_fails_closed_422(admin_client: TestClient) -> None:
    from modulo.core.guardrails.config import GuardrailConfigError

    with (
        _patched(pin=_clean_pin()),
        patch(
            "modulo.api.routes.guardrail_config.check_guardrail_drift",
            side_effect=GuardrailConfigError("bad legacy name"),
        ),
    ):
        resp = admin_client.get(f"{_BASE}/drift")

    assert resp.status_code == 422
    assert "bad legacy name" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /config — the pin-less and error branches
# ---------------------------------------------------------------------------


def test_config_without_pin_reports_clean_empty_config(admin_client: TestClient) -> None:
    with _patched(pin=None):
        resp = admin_client.get(_BASE)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "clean"
    assert body["hash"] is None
    assert body["applied_at"] is None


def test_config_config_error_fails_closed_422(admin_client: TestClient) -> None:
    from modulo.core.guardrails.config import GuardrailConfigError

    with (
        _patched(pin=_clean_pin()),
        patch(
            "modulo.api.routes.guardrail_config.check_guardrail_drift",
            side_effect=GuardrailConfigError("bad legacy name"),
        ),
    ):
        resp = admin_client.get(_BASE)

    assert resp.status_code == 422


def test_elevated_config_error_fails_closed_422(admin_client: TestClient) -> None:
    from modulo.core.guardrails.config import GuardrailConfigError

    with (
        _patched(pin=_clean_pin()),
        patch(
            "modulo.api.routes.guardrail_config.check_guardrail_drift",
            side_effect=GuardrailConfigError("bad legacy name"),
        ),
    ):
        resp = admin_client.get(f"{_BASE}/elevated")

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Helper units: corrupt-pin degradation paths
# ---------------------------------------------------------------------------


def test_applied_config_set_degrades_on_invalid_snapshot() -> None:
    from modulo.api.routes.guardrail_config import _applied_config_set

    broken = _clean_pin()
    broken.serialized_snapshot = "not: [valid: yaml"
    config_set = _applied_config_set(broken)
    assert not config_set.guardrails


def test_effective_config_set_uses_proposal_while_pending() -> None:
    from modulo.api.routes.guardrail_config import _effective_config_set

    config_set = _effective_config_set(_proposed_pin())
    assert len(config_set.guardrails) == 1
    assert config_set.guardrails[0].id == "no-aws-keys"


def test_effective_config_set_empty_without_serialized_state() -> None:
    from modulo.api.routes.guardrail_config import _effective_config_set

    empty_pin = GuardrailPin(org_id=_ORG_ID, status="proposed")
    assert not _effective_config_set(empty_pin).guardrails
    assert not _effective_config_set(None).guardrails


def test_diff_summary_counts_by_action() -> None:
    from modulo.api.routes.guardrail_config import _diff_summary
    from modulo.core.guardrails.config import ConfigChange

    changes = [
        ConfigChange(action="add", id="a", name="A"),
        ConfigChange(action="add", id="b", name="B"),
        ConfigChange(action="remove", id="c", name="C"),
    ]
    summary = _diff_summary(changes)
    assert summary == {"add": 2, "update": 0, "remove": 1}
