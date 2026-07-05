"""Unit tests for OKR-aligned eval suite progress tracking.

Tests ``track_okr_progress()``, ``alert_on_breach()``, and
the ``GET /api/v1/admin/evals/okr-progress/{suite_id}`` endpoint.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.eval_engine.okr import (
    OkrProgress,
    OkrSuite,
    OkrTrendPoint,
    _compute_trend_direction,
    _days_between,
    alert_on_breach,
    track_okr_progress,
)
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000011")
_SUITE_ID = "quality-suite-1"
_EVAL_ID_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
_EVAL_ID_2 = uuid.UUID("00000000-0000-0000-0000-000000000002")


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock()
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = AsyncMock(return_value=bind_mock)
    return session


def _make_row(**attrs) -> MagicMock:
    m = MagicMock()
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _make_result(all_value=None, scalar_value=None) -> MagicMock:
    m = MagicMock()
    if all_value is not None:
        m.all = MagicMock(return_value=all_value)
    if scalar_value is not None:
        m.scalar = MagicMock(return_value=scalar_value)
    return m


def _make_first_result(first_value=None) -> MagicMock:
    m = MagicMock()
    m.first = MagicMock(return_value=first_value)
    return m


# ── Pure function tests ───────────────────────────────────────────────────


class TestAlertOnBreach:
    def test_below_threshold_returns_true(self) -> None:
        assert alert_on_breach(0.8, 0.6) is True

    def test_above_threshold_returns_false(self) -> None:
        assert alert_on_breach(0.8, 0.9) is False

    def test_at_threshold_returns_false(self) -> None:
        assert alert_on_breach(0.8, 0.8) is False

    def test_zero_threshold_never_breaches(self) -> None:
        assert alert_on_breach(0.0, 0.0) is False

    def test_edge_zero_pass_rate(self) -> None:
        assert alert_on_breach(0.5, 0.0) is True

    def test_edge_perfect_pass_rate(self) -> None:
        assert alert_on_breach(1.0, 1.0) is False

    def test_alert_on_breach_from_suite_threshold(self) -> None:
        suite = OkrSuite(
            id="s1",
            name="test",
            pass_threshold=0.8,
            eval_definition_ids=[_EVAL_ID_1],
        )
        assert alert_on_breach(suite.pass_threshold, 0.6) is True
        assert alert_on_breach(suite.pass_threshold, 0.9) is False


class TestDaysBetween:
    def test_none_target_returns_none(self) -> None:
        from datetime import UTC, datetime

        assert _days_between(datetime.now(UTC), None) is None

    def test_future_date_returns_positive(self) -> None:
        from datetime import UTC, datetime

        result = _days_between(
            datetime(2026, 1, 1, tzinfo=UTC),
            "2026-09-30",
        )
        assert result == 272

    def test_target_in_past_returns_zero(self) -> None:
        from datetime import UTC, datetime

        result = _days_between(
            datetime(2026, 6, 26, tzinfo=UTC),
            "2026-01-01",
        )
        assert result == 0

    def test_invalid_target_returns_none(self) -> None:
        from datetime import UTC, datetime

        assert _days_between(datetime.now(UTC), "not-a-date") is None

    def test_target_today_returns_zero(self) -> None:
        from datetime import UTC, datetime

        result = _days_between(
            datetime(2026, 6, 26, tzinfo=UTC),
            "2026-06-26",
        )
        assert result == 0


class TestComputeTrendDirection:
    def test_less_than_two_points_returns_stable(self) -> None:
        trend = [OkrTrendPoint(period="7d", pass_rate=0.8, total_evals=10, passed_evals=8)]
        assert _compute_trend_direction(trend) == "stable"

    def test_empty_trend_returns_stable(self) -> None:
        assert _compute_trend_direction([]) == "stable"

    def test_declining_detected(self) -> None:
        trend = [
            OkrTrendPoint(period="14d", pass_rate=0.9, total_evals=10, passed_evals=9),
            OkrTrendPoint(period="7d", pass_rate=0.5, total_evals=10, passed_evals=5),
        ]
        assert _compute_trend_direction(trend) == "declining"

    def test_improving_detected(self) -> None:
        trend = [
            OkrTrendPoint(period="14d", pass_rate=0.5, total_evals=10, passed_evals=5),
            OkrTrendPoint(period="7d", pass_rate=0.9, total_evals=10, passed_evals=9),
        ]
        assert _compute_trend_direction(trend) == "improving"

    def test_stable_within_threshold(self) -> None:
        trend = [
            OkrTrendPoint(period="14d", pass_rate=0.85, total_evals=10, passed_evals=8),
            OkrTrendPoint(period="7d", pass_rate=0.83, total_evals=10, passed_evals=8),
        ]
        assert _compute_trend_direction(trend) == "stable"

    def test_skips_empty_periods(self) -> None:
        trend = [
            OkrTrendPoint(period="30d", pass_rate=0.0, total_evals=0, passed_evals=0),
            OkrTrendPoint(period="14d", pass_rate=0.9, total_evals=10, passed_evals=9),
            OkrTrendPoint(period="7d", pass_rate=0.4, total_evals=10, passed_evals=4),
        ]
        assert _compute_trend_direction(trend) == "declining"


# ── track_okr_progress tests ──────────────────────────────────────────────

_SUITE_THRESHOLD = 0.75


def _setup_suite_results_mock(session: AsyncMock) -> None:
    """Configure the mock session to return typical suite query results."""
    session.execute.side_effect = [
        _make_first_result(_make_row()),  # exists check
        _make_first_result(_make_row(pass_threshold=_SUITE_THRESHOLD)),  # threshold
        _make_first_result(
            _make_row(  # trend data
                total_7d=20,
                passed_7d=18,
                total_14d=30,
                passed_14d=24,
                total_30d=40,
                passed_30d=28,
                total_all=100,
                passed_all=80,
            )
        ),
    ]


class TestTrackOkrProgressDirect:
    async def test_raises_value_error_for_missing_suite(self) -> None:
        session = _make_mock_session()
        session.execute.return_value = _make_first_result(None)

        with pytest.raises(ValueError, match="not found"):
            await track_okr_progress(session, _ORG_ID, "nonexistent-suite")

    async def test_returns_okr_progress_model(self) -> None:
        session = _make_mock_session()
        _setup_suite_results_mock(session)

        progress = await track_okr_progress(session, _ORG_ID, _SUITE_ID)

        assert isinstance(progress, OkrProgress)
        assert progress.suite_id == _SUITE_ID
        assert progress.current_score == 0.9  # 18/20
        assert progress.pass_threshold == _SUITE_THRESHOLD
        assert progress.breach is False

    async def test_trend_contains_four_periods(self) -> None:
        session = _make_mock_session()
        _setup_suite_results_mock(session)

        progress = await track_okr_progress(session, _ORG_ID, _SUITE_ID)

        assert len(progress.trend) == 4
        periods = [t.period for t in progress.trend]
        assert periods == ["7d", "14d", "30d", "overall"]

    async def test_trend_correct_values(self) -> None:
        session = _make_mock_session()
        _setup_suite_results_mock(session)

        progress = await track_okr_progress(session, _ORG_ID, _SUITE_ID)

        assert progress.trend[0] == OkrTrendPoint(period="7d", pass_rate=0.9, total_evals=20, passed_evals=18)
        assert progress.trend[1] == OkrTrendPoint(period="14d", pass_rate=0.8, total_evals=30, passed_evals=24)
        assert progress.trend[2] == OkrTrendPoint(period="30d", pass_rate=0.7, total_evals=40, passed_evals=28)
        assert progress.trend[3] == OkrTrendPoint(period="overall", pass_rate=0.8, total_evals=100, passed_evals=80)

    async def test_breach_true_when_below_threshold(self) -> None:
        session = _make_mock_session()
        session.execute.side_effect = [
            _make_first_result(_make_row()),
            _make_first_result(_make_row(pass_threshold=0.95)),
            _make_first_result(
                _make_row(
                    total_7d=20,
                    passed_7d=15,  # 0.75 < 0.95
                    total_14d=30,
                    passed_14d=24,
                    total_30d=40,
                    passed_30d=28,
                    total_all=100,
                    passed_all=80,
                )
            ),
        ]

        progress = await track_okr_progress(session, _ORG_ID, _SUITE_ID)
        assert progress.breach is True
        assert progress.current_score == 0.75

    async def test_no_threshold_no_breach(self) -> None:
        session = _make_mock_session()
        session.execute.side_effect = [
            _make_first_result(_make_row()),
            _make_first_result(None),  # no threshold
            _make_first_result(
                _make_row(
                    total_7d=10,
                    passed_7d=2,
                    total_14d=10,
                    passed_14d=5,
                    total_30d=10,
                    passed_30d=7,
                    total_all=30,
                    passed_all=14,
                )
            ),
        ]

        progress = await track_okr_progress(session, _ORG_ID, _SUITE_ID)
        assert progress.breach is False
        assert progress.pass_threshold is None

    async def test_days_to_target_with_date(self) -> None:
        session = _make_mock_session()
        _setup_suite_results_mock(session)

        progress = await track_okr_progress(session, _ORG_ID, _SUITE_ID, target_date="2026-09-30")
        assert progress.days_to_target is not None
        assert isinstance(progress.days_to_target, int)

    async def test_days_to_target_none_without_date(self) -> None:
        session = _make_mock_session()
        _setup_suite_results_mock(session)

        progress = await track_okr_progress(session, _ORG_ID, _SUITE_ID)
        assert progress.days_to_target is None

    async def test_trend_direction_declining(self) -> None:
        session = _make_mock_session()
        session.execute.side_effect = [
            _make_first_result(_make_row()),
            _make_first_result(_make_row(pass_threshold=0.5)),
            _make_first_result(
                _make_row(
                    total_7d=20,
                    passed_7d=5,  # 0.25
                    total_14d=20,
                    passed_14d=16,  # 0.80
                    total_30d=20,
                    passed_30d=18,
                    total_all=60,
                    passed_all=39,
                )
            ),
        ]

        progress = await track_okr_progress(session, _ORG_ID, _SUITE_ID)
        assert progress.trend_direction == "declining"

    async def test_no_data_yet_returns_zero_score(self) -> None:
        session = _make_mock_session()
        session.execute.side_effect = [
            _make_first_result(_make_row()),
            _make_first_result(_make_row(pass_threshold=0.5)),
            _make_first_result(
                _make_row(
                    total_7d=0,
                    passed_7d=0,
                    total_14d=0,
                    passed_14d=0,
                    total_30d=0,
                    passed_30d=0,
                    total_all=0,
                    passed_all=0,
                )
            ),
        ]

        progress = await track_okr_progress(session, _ORG_ID, _SUITE_ID)
        assert progress.current_score == 0.0
        # 0.0 < 0.5 threshold, so breach is True
        assert progress.breach is True


class TestOkrSuiteModel:
    def test_minimal_suite(self) -> None:
        suite = OkrSuite(
            id="s1",
            name="test",
            pass_threshold=0.8,
            eval_definition_ids=[_EVAL_ID_1],
        )
        assert suite.id == "s1"
        assert suite.name == "test"
        assert suite.pass_threshold == 0.8
        assert suite.eval_definition_ids == [_EVAL_ID_1]
        assert suite.target_date is None
        assert suite.owner is None

    def test_full_suite(self) -> None:
        suite = OkrSuite(
            id="s1",
            name="test",
            pass_threshold=0.8,
            eval_definition_ids=[_EVAL_ID_1, _EVAL_ID_2],
            target_date="2026-09-30",
            owner="alice",
        )
        assert suite.target_date == "2026-09-30"
        assert suite.owner == "alice"


# ── OkrProgress model tests ──────────────────────────────────────────────


class TestOkrProgressModel:
    def test_fields(self) -> None:
        progress = OkrProgress(
            suite_id="s1",
            suite_name="test",
            current_score=0.85,
            pass_threshold=0.8,
            trend=[OkrTrendPoint(period="7d", pass_rate=0.85, total_evals=20, passed_evals=17)],
            trend_direction="stable",
            days_to_target=90,
            breach=False,
        )
        assert progress.suite_id == "s1"
        assert progress.current_score == 0.85
        assert progress.breach is False


# ── API endpoint tests ────────────────────────────────────────────────────


class TestOkrProgressEndpoint:
    URL = "/api/v1/admin/evals/okr-progress/quality-suite-1"

    @pytest.fixture()
    def _configure_session(self):
        def _setup(mock_session: AsyncMock) -> None:
            mock_session.execute.side_effect = [
                _make_result(scalar_value=None),  # set_rls_org
                _make_first_result(_make_row()),  # exists check
                _make_first_result(_make_row(pass_threshold=0.75)),  # threshold
                _make_first_result(
                    _make_row(  # trend data
                        total_7d=20,
                        passed_7d=18,
                        total_14d=30,
                        passed_14d=24,
                        total_30d=40,
                        passed_30d=28,
                        total_all=100,
                        passed_all=80,
                    )
                ),
            ]

        return _setup

    @pytest.fixture()
    def client(self, _configure_session) -> Generator[TestClient, None, None]:
        mock_session = _make_mock_session()
        _configure_session(mock_session)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
        )
        yield TestClient(app)
        app.dependency_overrides.clear()

    @pytest.fixture()
    def unauth_client(self) -> Generator[TestClient, None, None]:
        app.dependency_overrides.clear()
        app.dependency_overrides[get_settings] = _make_settings
        yield TestClient(app)
        app.dependency_overrides.clear()

    def test_unauthenticated_returns_401(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code == 401

    def test_admin_returns_200(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        assert resp.status_code == 200

    def test_response_shape(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        data = resp.json()
        assert set(data.keys()) == {
            "suite_id",
            "suite_name",
            "current_score",
            "pass_threshold",
            "trend",
            "trend_direction",
            "days_to_target",
            "breach",
        }

    def test_trend_points_in_response(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        data = resp.json()
        assert len(data["trend"]) == 4
        for point in data["trend"]:
            assert set(point.keys()) == {"period", "pass_rate", "total_evals", "passed_evals"}

    def test_correct_suite_data(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        data = resp.json()
        assert data["suite_id"] == "quality-suite-1"
        assert data["current_score"] == 0.9
        assert data["pass_threshold"] == 0.75
        assert data["breach"] is False

    def test_trend_values(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        data = resp.json()
        trends = {t["period"]: t for t in data["trend"]}
        assert trends["7d"]["pass_rate"] == 0.9
        assert trends["7d"]["total_evals"] == 20
        assert trends["14d"]["pass_rate"] == 0.8
        assert trends["overall"]["pass_rate"] == 0.8

    def test_with_target_date_query(self) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),
            _make_first_result(_make_row()),
            _make_first_result(_make_row(pass_threshold=0.75)),
            _make_first_result(
                _make_row(
                    total_7d=20,
                    passed_7d=18,
                    total_14d=30,
                    passed_14d=24,
                    total_30d=40,
                    passed_30d=28,
                    total_all=100,
                    passed_all=80,
                )
            ),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
        )
        resp = TestClient(app).get(self.URL + "?target_date=2026-09-30")
        assert resp.status_code == 200
        data = resp.json()
        assert data["days_to_target"] is not None

    def test_missing_suite_returns_404(self) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),
            _make_first_result(None),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
        )
        resp = TestClient(app).get("/api/v1/admin/evals/okr-progress/missing-suite")
        assert resp.status_code == 404

    def test_breach_true_in_response(self) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),
            _make_first_result(_make_row()),
            _make_first_result(_make_row(pass_threshold=0.95)),
            _make_first_result(
                _make_row(
                    total_7d=20,
                    passed_7d=15,
                    total_14d=30,
                    passed_14d=24,
                    total_30d=40,
                    passed_30d=28,
                    total_all=100,
                    passed_all=80,
                )
            ),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
        )
        resp = TestClient(app).get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["breach"] is True
