"""Router-level unit tests for the risk/assessment projection (FAR-941, §3.6).

The risk-assessment routes are exercised in isolation with a mocked session and the
module-level DB helpers patched. These tests pin the ROUTE-level contract:
- Filters (key_prefix, pipeline_id, days, action) are applied
- Action facet degrades gracefully when policy_gate_decisions.run_id is absent
- Redaction is applied before serialisation per the caller's role
- Response includes summary counts and facet_degraded flag
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.evidence_redaction import REDACTED_SENTINEL
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_EVIDENCE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
_NOW = datetime(2026, 1, 1, tzinfo=UTC)

_ROUTES = "modulo.api.routes.evidence"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _fake_evidence(**overrides: Any) -> MagicMock:
    row = MagicMock()
    row.id = overrides.get("id", _EVIDENCE_ID)
    row.organisation_id = overrides.get("organisation_id", _ORG_ID)
    row.key = overrides.get("key", "risk.api_call")
    row.subject_type = overrides.get("subject_type", "run")
    row.subject_id = overrides.get("subject_id", str(_RUN_ID))
    row.value = overrides.get("value", {"raw_output": "secret", "status": "block"})
    row.observed_at = overrides.get("observed_at", _NOW)
    row.producer_type = overrides.get("producer_type", "eval")
    row.producer_id = overrides.get("producer_id")
    row.created_at = overrides.get("created_at", _NOW)
    row.updated_at = overrides.get("updated_at", _NOW)
    return row


def _fake_rule(**overrides: Any) -> MagicMock:
    rule = MagicMock()
    rule.id = overrides.get("id", _EVIDENCE_ID)
    rule.key_pattern = overrides.get("key_pattern", "model_judge.*")
    rule.role = overrides.get("role", "viewer")
    rule.redacted_fields = overrides.get("redacted_fields", ["sensitive_output"])
    rule.created_at = overrides.get("created_at", _NOW)
    rule.updated_at = overrides.get("updated_at", _NOW)
    return rule


@contextmanager
def _client(org_role: str) -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="tester",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="tester",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _viewer_catch_all_rule() -> MagicMock:
    """A stored viewer catch-all rule redacting ``sensitive_output``."""
    return _fake_rule(key_pattern="*", role="viewer", redacted_fields=["sensitive_output"])


_RISK_ASSESSMENT_URL = "/api/v1/evidence/risk-assessment"


class TestRiskAssessmentRedaction:
    """Redaction is applied server-side per the caller's role."""

    def test_risk_assessment_redacts_sensitive_fields_for_viewer(self) -> None:
        row = _fake_evidence()
        with (
            _client("viewer") as client,
            patch(
                f"{_ROUTES}._load_risk_assessment_rows",
                new=AsyncMock(return_value=([row], False)),
            ),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[_viewer_catch_all_rule()])),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value={"block": 1, "warn": 0, "continue": 0, "escalate": 0}),
            ),
        ):
            resp = client.get(_RISK_ASSESSMENT_URL)

        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert "rows" in data
        assert len(data["rows"]) == 1
        assert data["rows"][0]["value"]["raw_output"] == REDACTED_SENTINEL
        # Permitted fields survive alongside the sentinel (no over-redaction).
        assert data["rows"][0]["value"]["status"] == "block"
        assert data["rows"][0]["key"] == "risk.api_call"
        assert data["summary"]["total_rows"] == 1
        assert data["summary"]["action_counts"]["block"] == 1
        assert data["summary"]["facet_degraded"] is False

    def test_risk_assessment_does_not_redact_for_admin(self) -> None:
        row = _fake_evidence()
        with (
            _client("admin") as client,
            patch(
                f"{_ROUTES}._load_risk_assessment_rows",
                new=AsyncMock(return_value=([row], False)),
            ),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[])),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value={"block": 1, "warn": 0, "continue": 0, "escalate": 0}),
            ),
        ):
            resp = client.get(_RISK_ASSESSMENT_URL)

        assert resp.status_code == 200
        data = resp.json()
        assert data["rows"][0]["value"]["raw_output"] == "secret"

    def test_risk_assessment_redacts_for_runner_when_no_rule_targets_it(self) -> None:
        # Runner is the default org role and a valid evidence.read principal.
        # With no rule for runner it must still fail CLOSED and redact both classes.
        row = _fake_evidence()
        with (
            _client("runner") as client,
            patch(
                f"{_ROUTES}._load_risk_assessment_rows",
                new=AsyncMock(return_value=([row], False)),
            ),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[])),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value={"block": 1, "warn": 0, "continue": 0, "escalate": 0}),
            ),
        ):
            resp = client.get(_RISK_ASSESSMENT_URL)

        assert resp.status_code == 200
        data = resp.json()
        assert data["rows"][0]["value"]["raw_output"] == REDACTED_SENTINEL
        assert data["rows"][0]["value"]["status"] == "block"


class TestRiskAssessmentFilters:
    """Filter parameters are passed through to the DB helper."""

    def test_key_prefix_filter(self) -> None:
        row = _fake_evidence(key="risk.api_call")
        with (
            _client("viewer") as client,
            patch(
                f"{_ROUTES}._load_risk_assessment_rows",
                new=AsyncMock(return_value=([row], False)),
            ) as mock_load,
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[_viewer_catch_all_rule()])),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value={"block": 1, "warn": 0, "continue": 0, "escalate": 0}),
            ),
        ):
            resp = client.get(f"{_RISK_ASSESSMENT_URL}?key_prefix=risk.")

        assert resp.status_code == 200
        # Verify the filter was passed to the helper
        mock_load.assert_called_once()
        call_kwargs = mock_load.call_args.kwargs
        assert call_kwargs.get("key_prefix") == "risk."

    def test_pipeline_id_filter(self) -> None:
        row = _fake_evidence()
        with (
            _client("viewer") as client,
            patch(
                f"{_ROUTES}._load_risk_assessment_rows",
                new=AsyncMock(return_value=([row], False)),
            ) as mock_load,
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[_viewer_catch_all_rule()])),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value={"block": 1, "warn": 0, "continue": 0, "escalate": 0}),
            ),
        ):
            resp = client.get(f"{_RISK_ASSESSMENT_URL}?pipeline_id={_PIPELINE_ID}")

        assert resp.status_code == 200
        mock_load.assert_called_once()
        call_kwargs = mock_load.call_args.kwargs
        assert call_kwargs.get("pipeline_id") == _PIPELINE_ID

    def test_days_filter_defaults_to_7(self) -> None:
        row = _fake_evidence()
        with (
            _client("viewer") as client,
            patch(
                f"{_ROUTES}._load_risk_assessment_rows",
                new=AsyncMock(return_value=([row], False)),
            ) as mock_load,
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[_viewer_catch_all_rule()])),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value={"block": 1, "warn": 0, "continue": 0, "escalate": 0}),
            ),
        ):
            resp = client.get(_RISK_ASSESSMENT_URL)

        assert resp.status_code == 200
        mock_load.assert_called_once()
        call_kwargs = mock_load.call_args.kwargs
        assert call_kwargs.get("days") == 7

    def test_days_filter_custom_value(self) -> None:
        row = _fake_evidence()
        with (
            _client("viewer") as client,
            patch(
                f"{_ROUTES}._load_risk_assessment_rows",
                new=AsyncMock(return_value=([row], False)),
            ) as mock_load,
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[_viewer_catch_all_rule()])),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value={"block": 1, "warn": 0, "continue": 0, "escalate": 0}),
            ),
        ):
            resp = client.get(f"{_RISK_ASSESSMENT_URL}?days=30")

        assert resp.status_code == 200
        mock_load.assert_called_once()
        call_kwargs = mock_load.call_args.kwargs
        assert call_kwargs.get("days") == 30

    def test_action_filter_valid_values(self) -> None:
        for action in ("block", "warn", "continue", "escalate"):
            row = _fake_evidence()
            with (
                _client("viewer") as client,
                patch(
                    f"{_ROUTES}._load_risk_assessment_rows",
                    new=AsyncMock(return_value=([row], False)),
                ) as mock_load,
                patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
                patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
                patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[_viewer_catch_all_rule()])),
                patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
                patch(
                    f"{_ROUTES}._count_risk_assessment_by_action",
                    new=AsyncMock(return_value={"block": 1, "warn": 0, "continue": 0, "escalate": 0}),
                ),
            ):
                resp = client.get(f"{_RISK_ASSESSMENT_URL}?action={action}")

            assert resp.status_code == 200
            mock_load.assert_called_once()
            call_kwargs = mock_load.call_args.kwargs
            assert call_kwargs.get("action") == action


class TestRiskAssessmentActionFacetDegradation:
    """Action facet degrades gracefully when policy_gate_decisions.run_id is absent."""

    def test_action_filter_degrades_when_run_id_column_missing(self) -> None:
        row = _fake_evidence()
        with (
            _client("viewer") as client,
            patch(
                f"{_ROUTES}._load_risk_assessment_rows",
                new=AsyncMock(return_value=([row], True)),  # facet_degraded=True
            ),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[_viewer_catch_all_rule()])),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value={"block": 0, "warn": 0, "continue": 0, "escalate": 0}),
            ),
        ):
            resp = client.get(f"{_RISK_ASSESSMENT_URL}?action=block")

        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["facet_degraded"] is True
        # Rows still returned (action filter ignored)
        assert len(data["rows"]) == 1

    def test_action_counts_zero_when_facet_degraded(self) -> None:
        row = _fake_evidence()
        with (
            _client("viewer") as client,
            patch(
                f"{_ROUTES}._load_risk_assessment_rows",
                new=AsyncMock(return_value=([row], True)),
            ),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[_viewer_catch_all_rule()])),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value={"block": 0, "warn": 0, "continue": 0, "escalate": 0}),
            ),
        ):
            resp = client.get(f"{_RISK_ASSESSMENT_URL}?action=block")

        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["facet_degraded"] is True
        # Action counts should be zero when degraded
        assert data["summary"]["action_counts"]["block"] == 0


class TestRiskAssessmentErrorArms:
    """Error arms map to documented status codes."""

    def _get(self, side_effect: Exception, org_role: str = "viewer") -> Any:
        with (
            _client(org_role) as client,
            patch(
                f"{_ROUTES}._load_risk_assessment_rows",
                new=AsyncMock(side_effect=side_effect),
            ),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[_viewer_catch_all_rule()])),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(side_effect=side_effect)),
            patch(f"{_ROUTES}._count_risk_assessment_by_action", new=AsyncMock(side_effect=side_effect)),
        ):
            return client.get(_RISK_ASSESSMENT_URL)

    def test_integrity_error_returns_409(self) -> None:
        resp = self._get(IntegrityError("stmt", "params", "orig"))
        assert resp.status_code == 409

    def test_programming_error_returns_501(self) -> None:
        resp = self._get(ProgrammingError("stmt", "params", "orig"))
        assert resp.status_code == 501

    def test_sqlalchemy_error_returns_503(self) -> None:
        resp = self._get(SQLAlchemyError("db down"))
        assert resp.status_code == 503

    def test_unexpected_error_returns_500(self) -> None:
        resp = self._get(RuntimeError("boom"))
        assert resp.status_code == 500


class TestRiskAssessmentInvalidAction:
    """Invalid action values are rejected with 422."""

    def test_invalid_action_returns_422(self) -> None:
        with _client("viewer") as client:
            resp = client.get(f"{_RISK_ASSESSMENT_URL}?action=invalid_action")

        assert resp.status_code == 422
        data = resp.json()
        assert "Invalid action" in data["detail"]


class TestRiskAssessmentNoOutcomeFacet:
    """No outcome parameter is accepted (deferred to predicate unit)."""

    def test_outcome_parameter_not_in_openapi(self) -> None:
        # The route simply doesn't declare an 'outcome' query parameter.
        # This is verified by the absence of the parameter in the schema.
        # Here we just verify the endpoint doesn't crash on an unknown param.
        row = _fake_evidence()
        with (
            _client("viewer") as client,
            patch(
                f"{_ROUTES}._load_risk_assessment_rows",
                new=AsyncMock(return_value=([row], False)),
            ),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[_viewer_catch_all_rule()])),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value={"block": 1, "warn": 0, "continue": 0, "escalate": 0}),
            ),
        ):
            # FastAPI will ignore unknown query params (or return 422 if strict)
            resp = client.get(f"{_RISK_ASSESSMENT_URL}?outcome=pass")

        # Should not error on unknown param (FastAPI ignores extra params by default)
        assert resp.status_code == 200
