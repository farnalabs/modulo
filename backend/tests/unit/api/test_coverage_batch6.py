"""Coverage batch 6: route adapters and refused-trigger persistence seams."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from modulo.api import trigger_busy
from modulo.api.routes import analytics, cost_components
from modulo.auth.jwt import TenantPrincipal
from modulo.core.analytics.service import (
    AnalyticsDatabaseError,
    AnalyticsMigrationRequiredError,
    AnalyticsQueryTimeoutError,
    AnalyticsValidationError,
)

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000d2")
_TRIGGER = uuid.UUID("00000000-0000-0000-0000-0000000000d3")


def _principal(role: str = "admin", org_id: uuid.UUID | None = _ORG) -> TenantPrincipal:
    return TenantPrincipal(
        username="coverage-user",
        organisation_id=org_id,
        account_id=_ACCOUNT,
        org_role=role,
    )


def _analytics_result() -> dict[str, object]:
    return {
        "group_by": "day",
        "dimension": None,
        "date_from": None,
        "date_to": None,
        "facts_freshness_hours": None,
        "facts_stale": False,
        "buckets": [],
    }


def _concurrency_result() -> dict[str, object]:
    return {"group_by": "day", "date_from": None, "date_to": None, "pool_reference": 4, "buckets": []}


class TestAnalyticsRouteCoverage:
    async def test_query_passes_repeated_filters_and_response(self) -> None:
        pipeline = uuid.uuid4()
        service = AsyncMock(return_value=_analytics_result())
        with (
            patch.object(analytics, "_resolve_scoped_team_ids", new=AsyncMock(return_value=(uuid.uuid4(),))),
            patch.object(analytics, "_analytics_session_factory", return_value=MagicMock()),
            patch.object(analytics, "run_analytics_query", new=service),
        ):
            result = await analytics.analytics_query(
                params=analytics.AnalyticsParams(pipeline_ids=(pipeline,)),
                settings=MagicMock(),
                principal=_principal("viewer"),
                _=None,
            )
        assert not result.buckets
        assert service.await_args.kwargs["org_id"] == _ORG
        assert service.await_args.kwargs["team_ids"] is not None

    async def test_query_without_org_is_forbidden(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await analytics.analytics_query(
                params=analytics.AnalyticsParams(),
                settings=MagicMock(),
                principal=_principal(org_id=None),
                _=None,
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.parametrize(
        ("error", "status_code"),
        [
            (AnalyticsQueryTimeoutError("slow"), 503),
            (AnalyticsMigrationRequiredError("migrate"), 501),
            (AnalyticsDatabaseError("database"), 503),
            (AnalyticsValidationError("invalid"), 422),
        ],
    )
    async def test_query_maps_typed_service_errors(self, error: Exception, status_code: int) -> None:
        with (
            patch.object(analytics, "_resolve_scoped_team_ids", new=AsyncMock(return_value=None)),
            patch.object(analytics, "_analytics_session_factory", return_value=MagicMock()),
            patch.object(analytics, "run_analytics_query", new=AsyncMock(side_effect=error)),
            pytest.raises(HTTPException) as exc_info,
        ):
            await analytics.analytics_query(
                params=analytics.AnalyticsParams(),
                settings=MagicMock(),
                principal=_principal(),
                _=None,
            )
        assert exc_info.value.status_code == status_code

    async def test_concurrency_passes_filters_and_returns_response(self) -> None:
        service = AsyncMock(return_value=_concurrency_result())
        with (
            patch.object(analytics, "_resolve_scoped_team_ids", new=AsyncMock(return_value=None)),
            patch.object(analytics, "_analytics_session_factory", return_value=MagicMock()),
            patch.object(analytics, "run_concurrency_query", new=service),
        ):
            result = await analytics.analytics_concurrency(
                group_by=analytics.AnalyticsGroupBy.HOUR,
                auto_granularity=True,
                trigger_type=None,
                status=None,
                pipeline_id=[uuid.uuid4()],
                folder_id=None,
                date_from=None,
                date_to=None,
                limit=10,
                settings=MagicMock(),
                principal=_principal(),
                _=None,
            )
        assert result.pool_reference == 4
        assert service.await_args.kwargs["params"].auto_granularity is True

    async def test_guardrails_and_export_error_paths(self) -> None:
        with (
            patch.object(analytics, "_resolve_scoped_team_ids", new=AsyncMock(return_value=None)),
            patch.object(analytics, "_analytics_session_factory", return_value=MagicMock()),
            patch.object(analytics, "run_guardrail_scorecard", new=AsyncMock(side_effect=SQLAlchemyError("db"))),
            pytest.raises(HTTPException) as guardrail_error,
        ):
            await analytics.analytics_guardrails(
                date_from=None,
                date_to=None,
                settings=MagicMock(),
                principal=_principal(),
                _=None,
            )
        assert guardrail_error.value.status_code == 500

        with (
            patch.object(analytics, "_resolve_scoped_team_ids", new=AsyncMock(return_value=None)),
            patch.object(analytics, "_analytics_session_factory", return_value=MagicMock()),
            patch.object(analytics, "export_facts", new=AsyncMock(return_value={"items": [], "total": 0})),
        ):
            response = await analytics.analytics_export(
                format="csv",
                offset=3,
                params=analytics.AnalyticsParams(limit=20),
                settings=MagicMock(),
                principal=_principal(),
                _=None,
            )
        assert response.headers["content-disposition"] == 'attachment; filename="analytics-export.csv"'

    def test_cost_formula_and_mapping_branches(self) -> None:
        with pytest.raises(cost_components.CostFormulaError, match="reserved"):
            cost_components.validate_component_fields(name="reported")
        with pytest.raises(cost_components.CostFormulaError, match="reserved"):
            cost_components.validate_component_fields(report_key="rate")
        with pytest.raises(cost_components.CostFormulaError, match="rate_fallback"):
            cost_components.validate_rate_fallback("missing")
        with pytest.raises(cost_components.CostFormulaError, match="rate"):
            cost_components.validate_component_formula(
                kind="calculated", formula="rate", rate_usd=None, rate_fallback=None
            )
        with pytest.raises(ValueError, match="self_reported"):
            cost_components._cross_field_validation(
                kind="self_reported", rate_usd=None, rate_fallback=None, formula=None, report_key=None
            )
        assert cost_components._map_validation_error(ValueError("duplicate_component")).status_code == 409
        assert cost_components._map_validation_error(ValueError("last_calculated guard")).status_code == 422


class _AsyncSessionContext:
    def __init__(self, session: MagicMock) -> None:
        self.session = session

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *_args: object) -> bool:
        return False


class TestTriggerBusyCoverage:
    async def test_busy_records_original_hash_and_webhook_payload(self) -> None:
        original = MagicMock()
        original.scalar_one_or_none.return_value = "original-hash"
        session = MagicMock()
        session.execute = AsyncMock(return_value=original)
        session.flush = AsyncMock()
        session.add = MagicMock()
        factory = MagicMock(return_value=_AsyncSessionContext(session))
        with (
            patch.object(trigger_busy, "get_or_create_engine", return_value=MagicMock()),
            patch.object(trigger_busy, "get_settings", return_value=MagicMock()),
            patch.object(trigger_busy, "async_sessionmaker", return_value=factory),
            patch.object(trigger_busy, "set_rls_org", new=AsyncMock()),
        ):
            await trigger_busy.record_busy_delivery(
                trigger_id=_TRIGGER,
                org_id=_ORG,
                trigger_type="webhook",
                source_event_id=uuid.uuid4(),
                raw_body=b'{"x":1}',
                raw_payload={"x": 1},
            )
        assert session.add.call_count == 2
        event = session.add.call_args_list[0].args[0]
        payload = session.add.call_args_list[1].args[0]
        assert event.raw_payload_hash == "original-hash"
        assert event.validation_result == trigger_busy.BUSY_VALIDATION_RESULT
        assert payload.raw_body == b'{"x":1}'

    async def test_backpressure_reason_is_added_and_recording_failure_is_logged(self) -> None:
        recorder = AsyncMock()
        with patch.object(trigger_busy, "_record_refused_delivery", new=recorder):
            await trigger_busy.record_backpressure_delivery(
                trigger_id=_TRIGGER,
                org_id=_ORG,
                trigger_type="webhook",
                reason="queue_depth=4",
            )
        assert recorder.await_args.kwargs["error_detail"].endswith("(queue_depth=4)")

        with (
            patch.object(trigger_busy, "get_or_create_engine", side_effect=RuntimeError("write failed")),
            patch.object(trigger_busy, "get_settings", return_value=MagicMock()),
        ):
            await trigger_busy.record_busy_delivery(trigger_id=_TRIGGER, org_id=_ORG, trigger_type="webhook")


class TestRouteDispatchSmoke:
    def test_cost_components_rejects_invalid_payload_through_fastapi(self) -> None:
        from modulo.api.main import app

        client = TestClient(app)
        response = client.post("/api/v1/admin/costs/components", json={"name": "bad name"})
        assert response.status_code in (401, 403, 422)

    def test_me_rejects_invalid_hitl_payload_through_fastapi(self) -> None:
        from modulo.api.main import app

        client = TestClient(app)
        response = client.put("/api/v1/me/hitl-email-preferences", json={"default": "yes"})
        assert response.status_code in (401, 403, 422)
