"""Unit tests for the GET /api/v1/analytics/capacity route (FAR-1343).

The route is a thin adapter: this module pins the wiring (principal -> org,
team boundary resolution, capacity params, response model) and the typed-error
mapping, with the service boundary patched out. The metric logic itself is
covered by ``tests/unit/test_analytics_capacity.py``.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from modulo.api.routes import analytics as route
from modulo.auth.jwt import TenantPrincipal
from modulo.core.analytics.service import AnalyticsRateLimitedError, AnalyticsValidationError

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000c2")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000c3")


def _principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="capacity-user",
        organisation_id=_ORG,
        account_id=_ACCOUNT,
        org_role="admin",
    )


def _payload() -> dict:
    return {
        "generated_at": "2026-10-17T12:00:00+00:00",
        "window_start": "2026-10-17T11:00:00+00:00",
        "window_seconds": 3600,
        "interval_seconds": 300,
        "limits": {
            "org_run_concurrency_limit": 5,
            "sandbox_concurrency_limit": 4,
            "sandbox_limit_is_default": False,
        },
        "pending": {
            "total": 2,
            "oldest_pending_age_seconds": 600.0,
            "by_cause": {"pipeline": 1, "org_run": 0, "sandbox": 0, "unmarked": 1},
            "by_pipeline": [
                {
                    "pipeline_id": str(_PIPELINE),
                    "pipeline_name": "alpha",
                    "pending_count": 2,
                    "oldest_pending_age_seconds": 600.0,
                    "max_concurrent_runs": 5,
                }
            ],
        },
        "drain": {
            "created": 10,
            "started": 9,
            "completed": 8,
            "ratio": 0.9,
            "baseline_depth": 3,
            "baseline_oldest_pending_age_seconds": 60.0,
            "final_depth": 2,
            "depth_change": -1,
            "verdict": "draining",
        },
        "binding": {
            "scope": "org_run",
            "limit": 5,
            "in_use": 5,
            "headroom": 0,
            "pending_blocked": 0,
            "admissible_if_raised": 0,
        },
        "queue_if_raised": {"pending_now": 2, "admissible_if_binding_raised": 0, "pending_after_raise": 2},
        "active_health": {
            "org_run_limit": 5,
            "gate_total": 5,
            "executing_total": 4,
            "running_total": 4,
            "parked_total": 1,
            "cancelling_total": 0,
            "utilisation": 1.0,
            "cap_held": True,
            "completions_in_window": 8,
            "zero_completion_intervals": 0,
            "stall_intervals": 3,
            "stall_suspected": False,
            "stale_heartbeat_threshold_seconds": 1800,
            "stale_heartbeat_count": 0,
            "attribution": [
                {
                    "pipeline_id": str(_PIPELINE),
                    "pipeline_name": "alpha",
                    "executing": 4,
                    "running": 4,
                    "parked": 1,
                    "oldest_executing_age_seconds": 120.0,
                    "stale_heartbeat_count": 0,
                }
            ],
        },
        "series": [
            {
                "interval_start": "2026-10-17T11:00:00+00:00",
                "interval_end": "2026-10-17T11:05:00+00:00",
                "created": 1,
                "started": 1,
                "completed": 0,
                "pending_depth": 1,
                "oldest_pending_age_seconds": 120.0,
                "drain_ratio": 1.0,
                "complete": True,
                "by_pipeline": [
                    {
                        "pipeline_id": str(_PIPELINE),
                        "pipeline_name": "alpha",
                        "pending_depth": 1,
                        "oldest_pending_age_seconds": 120.0,
                    }
                ],
            }
        ],
    }


async def _call(**overrides: object) -> route.CapacityResponse:
    kwargs: dict[str, object] = {
        "pipeline_id": None,
        "window_minutes": 60,
        "interval_seconds": 300,
        "stall_intervals": 3,
        "settings": MagicMock(),
        "principal": _principal(),
        "_": None,
    }
    kwargs.update(overrides)
    with (
        patch.object(route, "_resolve_scoped_team_ids", new=AsyncMock(return_value=None)),
        patch.object(route, "_analytics_session_factory", return_value=MagicMock()),
        patch.object(route, "run_capacity_query", new=AsyncMock(return_value=_payload())),
    ):
        return await route.analytics_capacity(**kwargs)  # type: ignore[arg-type]


class TestCapacityRoute:
    async def test_returns_typed_capacity_response(self) -> None:
        response = await _call()
        assert isinstance(response, route.CapacityResponse)
        assert response.pending.total == 2
        assert response.binding.scope == "org_run"
        assert response.active_health.executing_total == 4
        assert len(response.series) == 1

    async def test_pipeline_filter_is_passed_to_the_service(self) -> None:
        with (
            patch.object(route, "_resolve_scoped_team_ids", new=AsyncMock(return_value=())),
            patch.object(route, "_analytics_session_factory", return_value=MagicMock()),
            patch.object(route, "run_capacity_query", new=AsyncMock(return_value=_payload())) as service,
        ):
            await route.analytics_capacity(
                pipeline_id=[_PIPELINE],
                window_minutes=30,
                interval_seconds=120,
                stall_intervals=2,
                settings=MagicMock(),
                principal=_principal(),
                _=None,
            )
        params = service.await_args.kwargs["params"]
        assert params.pipeline_ids == (_PIPELINE,)
        assert params.window_minutes == 30
        assert params.interval_seconds == 120
        assert params.stall_intervals == 2

    async def test_rate_limited_maps_to_429(self) -> None:
        with (
            patch.object(route, "_resolve_scoped_team_ids", new=AsyncMock(return_value=None)),
            patch.object(route, "_analytics_session_factory", return_value=MagicMock()),
            patch.object(
                route,
                "run_capacity_query",
                new=AsyncMock(side_effect=AnalyticsRateLimitedError("Rate limit exceeded")),
            ),
            pytest.raises(HTTPException) as excinfo,
        ):
            await route.analytics_capacity(
                pipeline_id=None,
                window_minutes=60,
                interval_seconds=300,
                stall_intervals=3,
                settings=MagicMock(),
                principal=_principal(),
                _=None,
            )
        assert excinfo.value.status_code == 429

    async def test_http_exception_is_reraised_unchanged(self) -> None:
        with (
            patch.object(route, "_resolve_scoped_team_ids", new=AsyncMock(return_value=None)),
            patch.object(route, "_analytics_session_factory", return_value=MagicMock()),
            patch.object(
                route,
                "run_capacity_query",
                new=AsyncMock(side_effect=HTTPException(status_code=404, detail="gone")),
            ),
            pytest.raises(HTTPException) as excinfo,
        ):
            await route.analytics_capacity(
                pipeline_id=None,
                window_minutes=60,
                interval_seconds=300,
                stall_intervals=3,
                settings=MagicMock(),
                principal=_principal(),
                _=None,
            )
        assert excinfo.value.status_code == 404

    async def test_validation_error_maps_to_422(self) -> None:
        with (
            patch.object(route, "_resolve_scoped_team_ids", new=AsyncMock(return_value=None)),
            patch.object(route, "_analytics_session_factory", return_value=MagicMock()),
            patch.object(
                route,
                "run_capacity_query",
                new=AsyncMock(side_effect=AnalyticsValidationError("narrow the window")),
            ),
            pytest.raises(HTTPException) as excinfo,
        ):
            await route.analytics_capacity(
                pipeline_id=None,
                window_minutes=60,
                interval_seconds=300,
                stall_intervals=3,
                settings=MagicMock(),
                principal=_principal(),
                _=None,
            )
        assert excinfo.value.status_code == 422
