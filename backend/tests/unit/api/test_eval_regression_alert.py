"""FAR-379 eval regression alerting — API config endpoint + Alerting layer.

Covers, without Docker (mocked AsyncSession + transient ORM objects):

* ``PUT /api/v1/evals/suites/{suite_id}/alerting`` — the config surface
  (authz, admin-only, org-scoped 404, validation, and the persisted response);
* ``maybe_alert_eval_regression`` — the Alerting-layer decision function
  (requires-baseline, not-regressed, partial-run, idempotency, minimum-delta,
  cooldown rate-limit, subscriber guard);
* the **transport isolation** guarantee: the eval notifier topic namespace
  shares ZERO subscribers with production error forwarders, asserted both at the
  event-family level (the two sets are disjoint) and at the runtime guard level
  (``assert_eval_notification_isolated`` rejects any forwarder leak).
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.eval_engine.suite_run import (
    _ERROR_FORWARDER_EVENT_TYPES,
    _EVAL_EVENT_TYPES,
    SuiteRunError,
    assert_eval_notification_isolated,
    maybe_alert_eval_regression,
)
from modulo.db.models.eval_suite import EvalSuite
from modulo.db.models.eval_suite_run import SuiteRun
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "b" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_SUITE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


# ── API config endpoint ─────────────────────────────────────────────────────


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock()
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind_mock)
    return session


def _make_result(scalar_one_value=None, scalar_value=None, all_value=None) -> MagicMock:
    m = MagicMock()
    m.scalar_one_or_none = MagicMock(return_value=scalar_one_value)
    if scalar_value is not None:
        m.scalar = MagicMock(return_value=scalar_value)
    if all_value is not None:
        m.all = MagicMock(return_value=all_value)
        m.scalars.return_value = m
    return m


def _make_suite(**overrides) -> MagicMock:
    m = MagicMock()
    m.id = overrides.get("id", _SUITE_ID)
    m.organisation_id = overrides.get("organisation_id", _ORG_ID)
    m.name = overrides.get("name", "suites")
    m.baseline_window = overrides.get("baseline_window")
    m.minimum_delta = overrides.get("minimum_delta")
    m.cooldown = overrides.get("cooldown")
    return m


class _AdminClient:
    """Build a TestClient whose session yields a controllable mock."""

    def __init__(self, mock_session: AsyncMock) -> None:
        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
        )

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        self.client = TestClient(app)

    def close(self) -> None:
        app.dependency_overrides.clear()


class TestUpdateSuiteAlertingEndpoint:
    URL = "/api/v1/evals/suites/{}/alerting"

    def test_update_returns_200(self) -> None:
        suite = _make_suite()
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(),  # require_permission authz_enforce (kill-switch) read
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=suite),  # suite ownership lookup
        ]
        mock_session.flush = AsyncMock()

        client = _AdminClient(mock_session)
        try:
            resp = client.client.put(
                self.URL.format(_SUITE_ID),
                json={"baseline_window": 5, "minimum_delta": 0.2, "cooldown": 60},
            )
        finally:
            client.close()
        assert resp.status_code == 200
        data = resp.json()
        assert data["suite_id"] == str(_SUITE_ID)
        assert data["baseline_window"] == 5
        assert data["minimum_delta"] == pytest.approx(0.2)
        assert data["cooldown"] == 60
        assert suite.baseline_window == 5
        assert suite.minimum_delta == pytest.approx(0.2)
        assert suite.cooldown == 60

    def test_update_clears_fields_when_null_passed(self) -> None:
        suite = _make_suite(baseline_window=5, minimum_delta=0.2, cooldown=60)
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(),
            _make_result(scalar_value=None),
            _make_result(scalar_value=None),
            _make_result(scalar_value=None),
            _make_result(scalar_one_value=suite),
        ]
        mock_session.flush = AsyncMock()

        client = _AdminClient(mock_session)
        try:
            resp = client.client.put(self.URL.format(_SUITE_ID), json={"cooldown": None})
        finally:
            client.close()
        assert resp.status_code == 200
        data = resp.json()
        assert data["cooldown"] is None
        assert suite.cooldown is None
        assert suite.baseline_window == 5  # untouched fields retained

    def test_update_not_found_returns_404(self) -> None:
        mock_session = _make_mock_session()
        mock_session.execute.side_effect = [
            _make_result(),
            _make_result(scalar_value=None),
            _make_result(scalar_value=None),
            _make_result(scalar_value=None),
            _make_result(scalar_one_value=None),  # no suite for this org
        ]

        client = _AdminClient(mock_session)
        try:
            resp = client.client.put(self.URL.format(uuid.uuid4()), json={"cooldown": 60})
        finally:
            client.close()
        assert resp.status_code == 404

    def test_update_admin_required_returns_403(self) -> None:
        mock_session = _make_mock_session()
        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="operator",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="operator",
        )

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        try:
            resp = TestClient(app).put(self.URL.format(_SUITE_ID), json={"cooldown": 60})
        finally:
            app.dependency_overrides.clear()
        assert resp.status_code == 403

    def test_update_validation(self) -> None:
        mock_session = _make_mock_session()
        client = _AdminClient(mock_session)
        try:
            resp = client.client.put(self.URL.format(_SUITE_ID), json={"minimum_delta": 1.5})
            assert resp.status_code == 422
            resp = client.client.put(self.URL.format(_SUITE_ID), json={"baseline_window": 0})
            assert resp.status_code == 422
            resp = client.client.put(self.URL.format(_SUITE_ID), json={"cooldown": -1})
            assert resp.status_code == 422
        finally:
            client.close()


# ── Alerting-layer decision function ─────────────────────────────────────────


class _FakeScalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeScalars:
    def __init__(self, endpoints: list[Any]) -> None:
        self._endpoints = endpoints

    def all(self) -> list[Any]:
        return self._endpoints


class _Endpoint:
    def __init__(self, events: Any) -> None:
        self.events = events
        self.auto_disabled = False


def _run(
    org: uuid.UUID,
    *,
    regressed: bool | None = True,
    notified_at: datetime | None = None,
    state: str = "completed",
    comparison: dict[str, Any] | None = None,
) -> SuiteRun:
    run = SuiteRun(
        organisation_id=org,
        suite_id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        dataset_version=1,
        definition_checksum="a" * 64,
        model_backend_id=uuid.uuid4(),
        state=state,
        regressed=regressed,
        notified_at=notified_at,
        comparison_json=comparison,
        baseline_run_id=uuid.uuid4(),
    )
    run.id = uuid.uuid4()
    return run


def _suite(
    org: uuid.UUID,
    *,
    baseline_window: int | None = None,
    minimum_delta: float | None = None,
    cooldown: int | None = None,
) -> EvalSuite:
    suite = EvalSuite(
        organisation_id=org,
        name="suite",
        legacy_suite_id=None,
        baseline_window=baseline_window,
        minimum_delta=minimum_delta,
        cooldown=cooldown,
    )
    suite.id = uuid.uuid4()
    return suite


_COMPARISON = {"alerts": [{"prev_pass_rate": 0.9, "current_pass_rate": 0.3, "drop_pct": 0.6}]}


def _session(exec_value: Any = None, subscribers: list[Any] | None = None) -> AsyncMock:
    session = AsyncMock()
    session.execute.return_value = _FakeScalar(exec_value)
    session.scalars.return_value = _FakeScalars(subscribers or [])
    session.flush = AsyncMock()
    return session


async def test_alert_requires_baseline() -> None:
    run = _run(_ORG_ID)
    notifier = AsyncMock()
    outcome = await maybe_alert_eval_regression(_session(), run, _suite(_ORG_ID), None, notifier)
    assert outcome == "skipped_no_baseline"
    notifier.dispatch_event.assert_not_awaited()


async def test_alert_skips_not_regressed() -> None:
    run = _run(_ORG_ID, regressed=False)
    baseline = _run(_ORG_ID)
    notifier = AsyncMock()
    outcome = await maybe_alert_eval_regression(_session(), run, _suite(_ORG_ID), baseline, notifier)
    assert outcome == "skipped_not_regressed"
    notifier.dispatch_event.assert_not_awaited()


async def test_alert_never_pages_partial_run() -> None:
    run = _run(_ORG_ID, state="partial")
    baseline = _run(_ORG_ID)
    notifier = AsyncMock()
    out = await maybe_alert_eval_regression(_session(), run, _suite(_ORG_ID), baseline, notifier)
    assert out == "skipped_partial_run"
    notifier.dispatch_event.assert_not_awaited()


async def test_alert_idempotent_on_suite_run_id() -> None:
    run = _run(_ORG_ID, notified_at=datetime.now(UTC))
    baseline = _run(_ORG_ID)
    notifier = AsyncMock()
    out = await maybe_alert_eval_regression(_session(), run, _suite(_ORG_ID), baseline, notifier)
    assert out == "skipped_already_notified"
    notifier.dispatch_event.assert_not_awaited()


async def test_alert_below_minimum_delta_is_skipped() -> None:
    # Detection says regressed (drop 0.6), but the suite requires a larger delta.
    run = _run(_ORG_ID, comparison=_COMPARISON)
    baseline = _run(_ORG_ID)
    suite = _suite(_ORG_ID, minimum_delta=0.7)
    notifier = AsyncMock()
    out = await maybe_alert_eval_regression(_session(), run, suite, baseline, notifier)
    assert out == "skipped_below_minimum_delta"
    notifier.dispatch_event.assert_not_awaited()


async def test_alert_rate_limited_within_cooldown() -> None:
    run = _run(_ORG_ID, comparison=_COMPARISON)
    baseline = _run(_ORG_ID)
    suite = _suite(_ORG_ID, cooldown=60)
    recent = datetime.now(UTC) - timedelta(minutes=30)
    notifier = AsyncMock()
    out = await maybe_alert_eval_regression(_session(exec_value=recent), run, suite, baseline, notifier)
    assert out == "skipped_rate_limited"
    notifier.dispatch_event.assert_not_awaited()


async def test_alert_no_eval_subscribers_fails_loudly() -> None:
    run = _run(_ORG_ID, comparison=_COMPARISON)
    baseline = _run(_ORG_ID)
    suite = _suite(_ORG_ID)
    notifier = AsyncMock()
    with pytest.raises(SuiteRunError, match="no eval-scoped subscribers"):
        await maybe_alert_eval_regression(_session(), run, suite, baseline, notifier)
    notifier.dispatch_event.assert_not_awaited()


async def test_alert_dispatches_and_stamps_notified_at() -> None:
    run = _run(_ORG_ID, comparison=_COMPARISON)
    baseline = _run(_ORG_ID)
    suite = _suite(_ORG_ID)
    session = _session(subscribers=[_Endpoint(["eval_regression"])])
    notifier = AsyncMock()
    out = await maybe_alert_eval_regression(session, run, suite, baseline, notifier)
    assert out == "dispatched"
    notifier.dispatch_event.assert_awaited_once()
    assert run.notified_at is not None
    session.flush.assert_awaited_once()
    args, _ = notifier.dispatch_event.call_args
    assert args[0] == _ORG_ID
    payload = args[2]
    assert payload["suite_id"] == str(suite.id)
    assert payload["suite_name"] == "suite"
    assert payload["agent_name"] == "suite"
    assert payload["drop_pct"] == pytest.approx(0.6)
    assert payload["baseline_run_id"] == str(baseline.id)


async def test_alert_no_cooldown_skips_last_alert_query() -> None:
    run = _run(_ORG_ID, comparison=_COMPARISON)
    baseline = _run(_ORG_ID)
    suite = _suite(_ORG_ID)
    session = _session(subscribers=[_Endpoint(["eval_regression"])])
    session.execute = AsyncMock()
    notifier = AsyncMock()
    out = await maybe_alert_eval_regression(session, run, suite, baseline, notifier)
    assert out == "dispatched"
    session.execute.assert_not_awaited()
    session.scalars.assert_awaited_once()  # subscriber load only


# ── Transport isolation: eval topic vs error-forwarder topic ────────────────


def test_eval_event_family_disjoint_from_error_forwarders() -> None:
    """The eval notifier topic namespace shares ZERO subscribers with the
    production error-forwarder event family — the two sets are disjoint."""
    assert not (_EVAL_EVENT_TYPES & _ERROR_FORWARDER_EVENT_TYPES)
    assert _EVAL_EVENT_TYPES  # the eval namespace is non-empty


def test_error_forwarder_leak_rejected_at_runtime_guard() -> None:
    """A subscriber bound to an error-forwarder event is rejected by the guard."""
    for forwarder_event in sorted(_ERROR_FORWARDER_EVENT_TYPES):
        with pytest.raises(SuiteRunError, match="leaks to error-forwarder"):
            assert_eval_notification_isolated(["eval_regression", forwarder_event])
    # A pure eval subscriber (no forwarder overlap) is accepted.
    assert_eval_notification_isolated(["eval_regression"])
    assert_eval_notification_isolated(["eval_blocked"])
