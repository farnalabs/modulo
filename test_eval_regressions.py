import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from tests.unit.api.mock_session import configure_mock_session

from conftest import make_mock_result, make_mock_row, make_mock_session, make_settings

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.eval_engine.regression import detect_regressions

_EVAL_ID_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
_EVAL_ID_2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
_EVAL_ID_3 = uuid.UUID("00000000-0000-0000-0000-000000000003")
_EVAL_ID_4 = uuid.UUID("00000000-0000-0000-0000-000000000004")
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000011")
_RUN_ID_1 = uuid.UUID("00000000-0000-0000-0000-000000000020")
_RUN_ID_2 = uuid.UUID("00000000-0000-0000-0000-000000000021")
_RUN_ID_3 = uuid.UUID("00000000-0000-0000-0000-000000000022")


class TestDetectRegressionsDirect:
    async def test_no_results_returns_empty(self) -> None:
        session = make_mock_session()
        session.execute.return_value = make_mock_result(all_value=[])
        alerts = await detect_regressions(session, _ORG_ID, days=7)
        assert alerts == []

    async def test_declining_triggers_alert(self) -> None:
        session = make_mock_session()
        row = make_mock_row(
            eval_id=_EVAL_ID_1,
            eval_name="accuracy-check",
            recent_total=10,
            recent_passed=3,
            baseline_total=10,
            baseline_passed=10,
            affected_run_ids=[_RUN_ID_1, _RUN_ID_2],
        )
        session.execute.return_value = make_mock_result(all_value=[row])

        alerts = await detect_regressions(session, _ORG_ID, days=7, threshold=0.15)

        assert len(alerts) == 1
        a = alerts[0]
        assert a.eval_id == _EVAL_ID_1
        assert a.eval_name == "accuracy-check"
        assert a.prev_pass_rate == 1.0
        assert a.current_pass_rate == 0.3
        assert a.drop_pct == 0.7
        assert a.trend == "declining"
        assert a.affected_run_ids == [_RUN_ID_1, _RUN_ID_2]

    async def test_improving_not_alerted(self) -> None:
        session = make_mock_session()
        row = make_mock_row(
            eval_id=_EVAL_ID_1,
            eval_name="quality-gate",
            recent_total=10,
            recent_passed=10,
            baseline_total=10,
            baseline_passed=3,
            affected_run_ids=[],
        )
        session.execute.return_value = make_mock_result(all_value=[row])

        alerts = await detect_regressions(session, _ORG_ID, days=7, threshold=0.15)

        assert len(alerts) == 1
        assert alerts[0].trend == "improving"

    async def test_stable_not_alerted(self) -> None:
        session = make_mock_session()
        row = make_mock_row(
            eval_id=_EVAL_ID_1,
            eval_name="stable-check",
            recent_total=10,
            recent_passed=8,
            baseline_total=10,
            baseline_passed=8,
            affected_run_ids=[],
        )
        session.execute.return_value = make_mock_result(all_value=[row])

        alerts = await detect_regressions(session, _ORG_ID, days=7, threshold=0.15)

        assert len(alerts) == 1
        assert alerts[0].trend == "stable"

    async def test_drop_below_threshold_returns_stable(self) -> None:
        session = make_mock_session()
        row = make_mock_row(
            eval_id=_EVAL_ID_1,
            eval_name="narrow-drop",
            recent_total=100,
            recent_passed=80,
            baseline_total=100,
            baseline_passed=88,
            affected_run_ids=[],
        )
        session.execute.return_value = make_mock_result(all_value=[row])

        alerts = await detect_regressions(session, _ORG_ID, days=7, threshold=0.15)

        assert len(alerts) == 1
        assert alerts[0].trend == "stable"

    async def test_skipped_when_no_baseline(self) -> None:
        session = make_mock_session()
        row = make_mock_row(
            eval_id=_EVAL_ID_1,
            eval_name="new-eval",
            recent_total=5,
            recent_passed=4,
            baseline_total=0,
            baseline_passed=0,
            affected_run_ids=[],
        )
        session.execute.return_value = make_mock_result(all_value=[row])

        alerts = await detect_regressions(session, _ORG_ID, days=7, threshold=0.15)
        assert alerts == []

    async def test_skipped_when_no_recent(self) -> None:
        session = make_mock_session()
        row = make_mock_row(
            eval_id=_EVAL_ID_1,
            eval_name="stale-eval",
            recent_total=0,
            recent_passed=0,
            baseline_total=5,
            baseline_passed=4,
            affected_run_ids=[],
        )
        session.execute.return_value = make_mock_result(all_value=[row])

        alerts = await detect_regressions(session, _ORG_ID, days=7, threshold=0.15)
        assert alerts == []

    async def test_multiple_evals_mixed(self) -> None:
        session = make_mock_session()
        rows = [
            make_mock_row(
                eval_id=_EVAL_ID_1,
                eval_name="declining-eval",
                recent_total=10,
                recent_passed=3,
                baseline_total=10,
                baseline_passed=9,
                affected_run_ids=[_RUN_ID_1],
            ),
            make_mock_row(
                eval_id=_EVAL_ID_2,
                eval_name="stable-eval",
                recent_total=10,
                recent_passed=8,
                baseline_total=10,
                baseline_passed=8,
                affected_run_ids=[],
            ),
            make_mock_row(
                eval_id=_EVAL_ID_3,
                eval_name="improving-eval",
                recent_total=10,
                recent_passed=9,
                baseline_total=10,
                baseline_passed=3,
                affected_run_ids=[],
            ),
        ]
        session.execute.return_value = make_mock_result(all_value=rows)

        alerts = await detect_regressions(session, _ORG_ID, days=7, threshold=0.20)

        trends = {a.eval_id: a.trend for a in alerts}
        assert trends[_EVAL_ID_1] == "declining"
        assert trends[_EVAL_ID_2] == "stable"
        assert trends[_EVAL_ID_3] == "improving"

    async def test_affected_run_ids_empty_when_none_failed(self) -> None:
        session = make_mock_session()
        row = make_mock_row(
            eval_id=_EVAL_ID_1,
            eval_name="perfect-recent",
            recent_total=10,
            recent_passed=10,
            baseline_total=10,
            baseline_passed=5,
            affected_run_ids=[],
        )
        session.execute.return_value = make_mock_result(all_value=[row])

        alerts = await detect_regressions(session, _ORG_ID, days=7, threshold=0.15)
        assert len(alerts) == 1
        assert alerts[0].affected_run_ids == []


class TestRegressionAlertsEndpoint:
    URL = "/api/v1/admin/evals/regressions"

    @pytest.fixture()
    def _regression_rows(self) -> list[MagicMock]:
        return [
            make_mock_row(
                eval_id=_EVAL_ID_1,
                eval_name="accuracy-check",
                recent_total=10,
                recent_passed=3,
                baseline_total=10,
                baseline_passed=9,
                affected_run_ids=[_RUN_ID_1],
            ),
        ]

    @pytest.fixture()
    def _configure_session(self, _regression_rows):
        def _setup(mock_session: AsyncMock) -> None:
            mock_session.execute.side_effect = [
                make_mock_result(scalar_value=None),
                make_mock_result(all_value=_regression_rows),
            ]

        return _setup

    @pytest.fixture()
    def client(self, _configure_session) -> Generator[TestClient, None, None]:
        mock_session = make_mock_session()
        _configure_session(mock_session)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_settings] = make_settings
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
        app.dependency_overrides[get_settings] = make_settings
        yield TestClient(app)
        app.dependency_overrides.clear()

    @pytest.fixture()
    def operator_client(self) -> Generator[TestClient, None, None]:
        mock_session = make_mock_session()
        mock_session.execute.side_effect = [
            make_mock_result(scalar_value=None),
            make_mock_result(all_value=[]),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_settings] = make_settings
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="operator",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="operator",
        )
        yield TestClient(app)
        app.dependency_overrides.clear()

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.get(self.URL)
        assert resp.status_code == 403

    def test_unauthenticated_returns_401(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code == 401

    def test_admin_returns_200(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        assert resp.status_code == 200

    def test_returns_regression_alerts(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        data = resp.json()
        assert set(data.keys()) == {"alerts", "total_regressions", "threshold", "lookback_days"}
        assert data["total_regressions"] == 1
        assert data["threshold"] == 0.15
        assert data["lookback_days"] == 7

    def test_alert_shape(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        a = resp.json()["alerts"][0]
        assert set(a.keys()) == {
            "eval_id",
            "eval_name",
            "prev_pass_rate",
            "current_pass_rate",
            "drop_pct",
            "trend",
            "affected_run_ids",
        }
        assert a["eval_id"] == str(_EVAL_ID_1)
        assert a["eval_name"] == "accuracy-check"
        assert a["prev_pass_rate"] == 0.9
        assert a["current_pass_rate"] == 0.3
        assert a["drop_pct"] == 0.6
        assert a["trend"] == "declining"
        assert a["affected_run_ids"] == [str(_RUN_ID_1)]

    def test_custom_days_and_threshold(self, _configure_session) -> None:
        mock_session = make_mock_session()
        row = make_mock_row(
            eval_id=_EVAL_ID_1,
            eval_name="custom-eval",
            recent_total=20,
            recent_passed=10,
            baseline_total=20,
            baseline_passed=18,
            affected_run_ids=[_RUN_ID_1, _RUN_ID_2],
        )
        mock_session.execute.side_effect = [
            make_mock_result(scalar_value=None),
            make_mock_result(all_value=[row]),
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
        resp = TestClient(app).get(self.URL + "?days=14&threshold=0.10")
        data = resp.json()
        assert data["lookback_days"] == 14
        assert data["threshold"] == 0.1

    def test_empty_no_alerts(self, _configure_session) -> None:
        mock_session = make_mock_session()
        mock_session.execute.side_effect = [
            make_mock_result(scalar_value=None),
            make_mock_result(all_value=[]),
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
        data = resp.json()
        assert data["total_regressions"] == 0
        assert data["alerts"] == []
