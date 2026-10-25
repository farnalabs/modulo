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

import pytest
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
            patch(f"{_ROUTES}._load_risk_assessment_row_actions", new=AsyncMock(return_value=({}, False))),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value=({"block": 1, "warn": 0, "continue": 0, "escalate": 0}, False)),
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
            patch(f"{_ROUTES}._load_risk_assessment_row_actions", new=AsyncMock(return_value=({}, False))),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value=({"block": 1, "warn": 0, "continue": 0, "escalate": 0}, False)),
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
            patch(f"{_ROUTES}._load_risk_assessment_row_actions", new=AsyncMock(return_value=({}, False))),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value=({"block": 1, "warn": 0, "continue": 0, "escalate": 0}, False)),
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
            patch(f"{_ROUTES}._load_risk_assessment_row_actions", new=AsyncMock(return_value=({}, False))),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value=({"block": 1, "warn": 0, "continue": 0, "escalate": 0}, False)),
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
            patch(f"{_ROUTES}._load_risk_assessment_row_actions", new=AsyncMock(return_value=({}, False))),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value=({"block": 1, "warn": 0, "continue": 0, "escalate": 0}, False)),
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
            patch(f"{_ROUTES}._load_risk_assessment_row_actions", new=AsyncMock(return_value=({}, False))),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value=({"block": 1, "warn": 0, "continue": 0, "escalate": 0}, False)),
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
            patch(f"{_ROUTES}._load_risk_assessment_row_actions", new=AsyncMock(return_value=({}, False))),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value=({"block": 1, "warn": 0, "continue": 0, "escalate": 0}, False)),
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
                patch(f"{_ROUTES}._load_risk_assessment_row_actions", new=AsyncMock(return_value=({}, False))),
                patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
                patch(
                    f"{_ROUTES}._count_risk_assessment_by_action",
                    new=AsyncMock(return_value=({"block": 1, "warn": 0, "continue": 0, "escalate": 0}, False)),
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
            patch(f"{_ROUTES}._load_risk_assessment_row_actions", new=AsyncMock(return_value=({}, False))),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value=({"block": 0, "warn": 0, "continue": 0, "escalate": 0}, False)),
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
            patch(f"{_ROUTES}._load_risk_assessment_row_actions", new=AsyncMock(return_value=({}, False))),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value=({"block": 0, "warn": 0, "continue": 0, "escalate": 0}, False)),
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
            patch(f"{_ROUTES}._load_risk_assessment_row_actions", new=AsyncMock(return_value=({}, False))),
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
            patch(f"{_ROUTES}._load_risk_assessment_row_actions", new=AsyncMock(return_value=({}, False))),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value=({"block": 1, "warn": 0, "continue": 0, "escalate": 0}, False)),
            ),
        ):
            # FastAPI will ignore unknown query params (or return 422 if strict)
            resp = client.get(f"{_RISK_ASSESSMENT_URL}?outcome=pass")

        # Should not error on unknown param (FastAPI ignores extra params by default)
        assert resp.status_code == 200


def _scalar_result(value: int | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _rows_result(rows: list[Any]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


def _decisions_result(decisions: list[tuple[Any, Any, str]]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = decisions
    return result


class TestRiskAssessmentDegradationRealPath:
    """The real ``ProgrammingError`` arms — no patching of the loaders.

    These drive the helper functions with an execute that actually raises, so
    removing the guard fails the suite (the mocked-loader tests above cannot
    prove the catch exists).
    """

    async def test_load_rows_degrades_and_falls_back_to_unfiltered(self) -> None:
        from modulo.api.routes import evidence as mod

        row = _fake_evidence()
        session = _make_mock_session()
        missing_column = ProgrammingError("stmt", "params", "no column")
        missing_column.orig = MagicMock(sqlstate="42703")
        session.execute = AsyncMock(side_effect=[missing_column, _rows_result([row])])

        rows, degraded = await mod._load_risk_assessment_rows(session, _ORG_ID, action="block", limit=10)

        assert rows == [row]
        assert degraded is True
        assert session.execute.await_count == 2

    async def test_load_rows_re_raises_unrelated_programming_error(self) -> None:
        from modulo.api.routes import evidence as mod

        session = _make_mock_session()
        unrelated = ProgrammingError("stmt", "params", "syntax error")
        unrelated.orig = MagicMock(sqlstate="42601")
        session.execute = AsyncMock(side_effect=unrelated)

        with pytest.raises(ProgrammingError):
            await mod._load_risk_assessment_rows(session, _ORG_ID, action="block", limit=10)

    async def test_load_rows_with_action_uses_the_filtered_query(self) -> None:
        from modulo.api.routes import evidence as mod

        row = _fake_evidence()
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_rows_result([row]))

        rows, degraded = await mod._load_risk_assessment_rows(session, _ORG_ID, action="block")

        assert rows == [row]
        assert degraded is False
        assert session.execute.await_count == 1

    async def test_load_rows_without_action_never_degrades(self) -> None:
        from modulo.api.routes import evidence as mod

        row = _fake_evidence()
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_rows_result([row]))

        rows, degraded = await mod._load_risk_assessment_rows(session, _ORG_ID)

        assert rows == [row]
        assert degraded is False
        assert session.execute.await_count == 1

    async def test_count_degrades_to_unfiltered_count(self) -> None:
        from modulo.api.routes import evidence as mod

        session = _make_mock_session()
        missing_column = ProgrammingError("stmt", "params", "no column")
        missing_column.orig = MagicMock(sqlstate="42703")
        session.execute = AsyncMock(side_effect=[missing_column, _scalar_result(4)])

        total = await mod._count_risk_assessment_rows(session, _ORG_ID, action="block")

        assert total == 4

    async def test_count_with_action_uses_filtered_query(self) -> None:
        from modulo.api.routes import evidence as mod

        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_scalar_result(2))

        total = await mod._count_risk_assessment_rows(session, _ORG_ID, action="warn")

        assert total == 2
        assert session.execute.await_count == 1

    async def test_count_by_action_degrades_all_to_zero(self) -> None:
        from modulo.api.routes import evidence as mod

        session = _make_mock_session()
        missing_column = ProgrammingError("stmt", "params", "no column")
        missing_column.orig = MagicMock(sqlstate="42703")
        session.execute = AsyncMock(side_effect=missing_column)

        counts, degraded = await mod._count_risk_assessment_by_action(session, _ORG_ID)

        assert counts == {"block": 0, "escalate": 0, "warn": 0, "continue": 0}
        assert degraded is True
        # The first failing action short-circuits the rest.
        assert session.execute.await_count == 1

    async def test_count_by_action_reraises_unrelated_programming_error(self) -> None:
        from modulo.api.routes import evidence as mod

        session = _make_mock_session()
        unrelated = ProgrammingError("stmt", "params", "syntax error")
        unrelated.orig = MagicMock(sqlstate="42601")
        session.execute = AsyncMock(side_effect=unrelated)

        with pytest.raises(ProgrammingError):
            await mod._count_risk_assessment_by_action(session, _ORG_ID)

    async def test_count_by_action_success(self) -> None:
        from modulo.api.routes import evidence as mod

        session = _make_mock_session()
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result(3),
                _scalar_result(1),
                _scalar_result(5),
                _scalar_result(2),
            ]
        )

        counts, degraded = await mod._count_risk_assessment_by_action(session, _ORG_ID)

        assert counts == {"block": 3, "escalate": 1, "warn": 5, "continue": 2}
        assert degraded is False


class TestRiskAssessmentScopeClause:
    """The shared WHERE builder covers every filter combination."""

    async def test_rows_apply_key_prefix_and_pipeline_filters(self) -> None:
        from modulo.api.routes import evidence as mod

        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_rows_result([]))

        rows, degraded = await mod._load_risk_assessment_rows(
            session,
            _ORG_ID,
            key_prefix="risk.",
            pipeline_id=_PIPELINE_ID,
            days=3,
            limit=5,
        )

        assert rows == []
        assert degraded is False
        sql = str(session.execute.await_args.args[0])
        # The pipeline filter is the split_part subquery, not an ORM join.
        assert "split_part" in sql
        assert "LIKE" in sql

    async def test_count_applies_key_prefix_and_pipeline_filters(self) -> None:
        from modulo.api.routes import evidence as mod

        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_scalar_result(7))

        total = await mod._count_risk_assessment_rows(
            session, _ORG_ID, key_prefix="risk.", pipeline_id=_PIPELINE_ID, days=3
        )

        assert total == 7
        sql = str(session.execute.await_args.args[0])
        assert "split_part" in sql
        assert "LIKE" in sql

    async def test_by_action_applies_key_prefix_and_pipeline_filters(self) -> None:
        from modulo.api.routes import evidence as mod

        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_scalar_result(0))

        await mod._count_risk_assessment_by_action(
            session, _ORG_ID, key_prefix="risk.", pipeline_id=_PIPELINE_ID, days=3
        )

        sql = str(session.execute.await_args.args[0])
        assert "split_part" in sql
        assert "LIKE" in sql


class TestRiskAssessmentRowActions:
    """Per-row gate action resolution from the decision record."""

    async def test_resolves_run_and_node_actions_with_severity(self) -> None:
        from modulo.api.routes import evidence as mod

        node_id = uuid.uuid4()
        run_row = _fake_evidence(id=uuid.uuid4(), subject_type="run", subject_id=str(_RUN_ID))
        node_row = _fake_evidence(
            id=uuid.uuid4(),
            subject_type="node_execution",
            subject_id=f"{_RUN_ID}:{node_id}",
        )
        session = _make_mock_session()
        session.execute = AsyncMock(
            return_value=_decisions_result(
                [
                    (_RUN_ID, None, "continue"),
                    (_RUN_ID, None, "warn"),  # run-level worst is warn
                    (_RUN_ID, node_id, "block"),
                    (_RUN_ID, node_id, "warn"),
                    (None, None, "block"),  # unattributed decision is ignored
                    (_RUN_ID, None, "bogus"),  # invalid vocabulary is ignored
                ]
            )
        )

        resolved, degraded = await mod._load_risk_assessment_row_actions(session, _ORG_ID, [run_row, node_row])

        assert degraded is False
        assert resolved[run_row.id] == "warn"
        assert resolved[node_row.id] == "block"

    async def test_no_rows_issues_no_query(self) -> None:
        from modulo.api.routes import evidence as mod

        session = _make_mock_session()
        session.execute = AsyncMock()

        resolved, degraded = await mod._load_risk_assessment_row_actions(session, _ORG_ID, [])

        assert resolved == {}
        assert degraded is False
        session.execute.assert_not_awaited()

    async def test_degrades_when_run_id_column_missing(self) -> None:
        from modulo.api.routes import evidence as mod

        row = _fake_evidence()
        session = _make_mock_session()
        missing_column = ProgrammingError("stmt", "params", "no column")
        missing_column.orig = MagicMock(sqlstate="42703")
        session.execute = AsyncMock(side_effect=missing_column)

        resolved, degraded = await mod._load_risk_assessment_row_actions(session, _ORG_ID, [row])

        assert resolved == {}
        assert degraded is True

    async def test_reraises_unrelated_programming_error(self) -> None:
        from modulo.api.routes import evidence as mod

        row = _fake_evidence()
        session = _make_mock_session()
        unrelated = ProgrammingError("stmt", "params", "syntax error")
        unrelated.orig = MagicMock(sqlstate="42601")
        session.execute = AsyncMock(side_effect=unrelated)

        with pytest.raises(ProgrammingError):
            await mod._load_risk_assessment_row_actions(session, _ORG_ID, [row])

    async def test_malformed_subjects_are_skipped(self) -> None:
        from modulo.api.routes import evidence as mod

        rows = [
            _fake_evidence(id=uuid.uuid4(), subject_type="run", subject_id="not-a-uuid"),
            _fake_evidence(id=uuid.uuid4(), subject_type="node_execution", subject_id="no-colon"),
            _fake_evidence(id=uuid.uuid4(), subject_type="other", subject_id=str(_RUN_ID)),
            _fake_evidence(id=uuid.uuid4(), subject_type="node_execution", subject_id="bad:node"),
        ]
        session = _make_mock_session()
        session.execute = AsyncMock()

        resolved, degraded = await mod._load_risk_assessment_row_actions(session, _ORG_ID, rows)

        assert resolved == {}
        assert degraded is False
        session.execute.assert_not_awaited()

    async def test_mixed_valid_and_malformed_rows_resolve_the_valid_ones(self) -> None:
        from modulo.api.routes import evidence as mod

        valid = _fake_evidence(id=uuid.uuid4(), subject_type="run", subject_id=str(_RUN_ID))
        unmatched = _fake_evidence(id=uuid.uuid4(), subject_type="run", subject_id=str(uuid.uuid4()))
        malformed = _fake_evidence(id=uuid.uuid4(), subject_type="run", subject_id="not-a-uuid")
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_decisions_result([(_RUN_ID, None, "warn")]))

        resolved, degraded = await mod._load_risk_assessment_row_actions(
            session, _ORG_ID, [valid, unmatched, malformed]
        )

        assert degraded is False
        # A run with no decision is absent (renders as unknown), never fabricated.
        assert resolved == {valid.id: "warn"}

    def test_most_severe_ordering(self) -> None:
        from modulo.api.routes import evidence as mod

        assert mod._most_severe(None, "continue") == "continue"
        assert mod._most_severe("continue", "escalate") == "escalate"
        assert mod._most_severe("block", "escalate") == "block"
        assert mod._most_severe("warn", "continue") == "warn"


class TestRiskAssessmentRowActionInResponse:
    """The response carries the resolved action per row, not a frontend guess."""

    def test_row_action_is_serialised(self) -> None:
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
            patch(
                f"{_ROUTES}._load_risk_assessment_row_actions",
                new=AsyncMock(return_value=({_EVIDENCE_ID: "block"}, False)),
            ),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value=({"block": 1, "warn": 0, "continue": 0, "escalate": 0}, False)),
            ),
        ):
            resp = client.get(_RISK_ASSESSMENT_URL)

        assert resp.status_code == 200
        assert resp.json()["rows"][0]["action"] == "block"

    def test_degradation_from_counts_is_surfaced(self) -> None:
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
            patch(
                f"{_ROUTES}._load_risk_assessment_row_actions",
                new=AsyncMock(return_value=({}, False)),
            ),
            patch(f"{_ROUTES}._count_risk_assessment_rows", new=AsyncMock(return_value=1)),
            patch(
                f"{_ROUTES}._count_risk_assessment_by_action",
                new=AsyncMock(return_value=({"block": 0, "warn": 0, "continue": 0, "escalate": 0}, True)),
            ),
        ):
            resp = client.get(_RISK_ASSESSMENT_URL)

        assert resp.status_code == 200
        assert resp.json()["summary"]["facet_degraded"] is True


class TestRiskAssessmentSupportingEvidenceHelpers:
    """Cover the shared evidence and redaction paths used by this projection."""

    async def test_load_evidence_rows_supports_run_node_and_exact_key_filters(self) -> None:
        from modulo.api.routes import evidence as mod

        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_rows_result([]))

        await mod._load_evidence_rows(session, _ORG_ID, _RUN_ID, None, None)
        run_stmt = str(session.execute.await_args.args[0])
        assert "subject_type" in run_stmt
        assert "subject_id" in run_stmt

        await mod._load_evidence_rows(session, _ORG_ID, _RUN_ID, "node-1", "risk.api_call")
        node_stmt = str(session.execute.await_args.args[0])
        assert "subject_type" in node_stmt
        assert "evidence.key =" in node_stmt

    async def test_load_single_row_and_rules_return_scalar_and_list(self) -> None:
        from modulo.api.routes import evidence as mod

        row = _fake_evidence()
        rule = _fake_rule()
        session = _make_mock_session()
        session.execute = AsyncMock(side_effect=[_scalar_result(row), _rows_result([rule])])

        assert await mod._load_evidence_row(session, _ORG_ID, _EVIDENCE_ID) is row
        assert await mod._load_redaction_rules(session, _ORG_ID) == [rule]

    async def test_upsert_redaction_rule_creates_and_restores_existing_rule(self) -> None:
        from modulo.api.routes import evidence as mod

        new_rule = _fake_rule()
        session = _make_mock_session()
        session.execute = AsyncMock(side_effect=[_scalar_result(None), _scalar_result(new_rule)])

        created = await mod._upsert_redaction_rule(session, _ORG_ID, "risk.*", "viewer", ["sensitive_output"])
        assert created is not None
        session.add.assert_called_once()
        session.flush.assert_awaited()

        existing = _fake_rule(redacted_fields=[], role="viewer")
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=_scalar_result(existing))
        restored = await mod._upsert_redaction_rule(session, _ORG_ID, "risk.*", "viewer", ["sensitive_output"])
        assert restored is existing
        assert existing.redacted_fields == ["sensitive_output"]
        assert existing.deleted_at is None
        session.add.assert_not_called()

    def test_redaction_rule_validator_rejects_unknown_and_deduplicates(self) -> None:
        from pydantic import ValidationError

        from modulo.api.routes.evidence import RedactionRuleUpsert

        with pytest.raises(ValidationError, match="Unknown redaction class"):
            RedactionRuleUpsert(
                key_pattern="risk.*",
                role="viewer",
                redacted_fields=["unknown_class"],
            )

        rule = RedactionRuleUpsert(
            key_pattern="risk.*",
            role="viewer",
            redacted_fields=["sensitive_output", "sensitive_output"],
        )
        assert rule.redacted_fields == ["sensitive_output"]

    def test_redaction_rule_endpoints_and_plain_evidence_reads(self) -> None:
        row = _fake_evidence()
        rule = _fake_rule()
        with (
            _client("admin") as client,
            patch(f"{_ROUTES}._load_redaction_rules", new=AsyncMock(return_value=[rule])),
            patch(f"{_ROUTES}._upsert_redaction_rule", new=AsyncMock(return_value=rule)),
            patch(f"{_ROUTES}._load_evidence_row", new=AsyncMock(return_value=row)),
            patch(f"{_ROUTES}._load_evidence_rows", new=AsyncMock(return_value=[row])),
            patch(f"{_ROUTES}.load_redaction_rules", new=AsyncMock(return_value=[])),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
        ):
            rules_response = client.get("/api/v1/evidence/redaction-rules")
            upsert_response = client.put(
                "/api/v1/evidence/redaction-rules",
                json={"key_pattern": "risk.*", "role": "viewer", "redacted_fields": []},
            )
            get_response = client.get(f"/api/v1/evidence/{_EVIDENCE_ID}")
            list_response = client.get(f"/api/v1/evidence?run_id={_RUN_ID}")

        assert rules_response.status_code == 200
        assert len(rules_response.json()) == 1
        assert upsert_response.status_code == 200
        assert get_response.status_code == 200
        assert get_response.json()["action"] is None
        assert list_response.status_code == 200
        assert len(list_response.json()) == 1

    def test_get_evidence_returns_404_for_missing_row(self) -> None:
        with (
            _client("viewer") as client,
            patch(f"{_ROUTES}._load_evidence_row", new=AsyncMock(return_value=None)),
            patch(f"{_ROUTES}.set_rls_org", new=AsyncMock()),
            patch(f"{_ROUTES}.set_rls_user_context", new=AsyncMock()),
        ):
            response = client.get(f"/api/v1/evidence/{_EVIDENCE_ID}")

        assert response.status_code == 404
        assert response.json()["detail"] == "Evidence row not found"
