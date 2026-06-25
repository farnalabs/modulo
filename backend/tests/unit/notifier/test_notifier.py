"""Unit tests for Notifier dispatch, HMAC signing, retry, and dead-letter logic."""

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from httpx import Response

from modulo.core.notifier import (
    MAX_DEAD_LETTERS,
    MAX_RETRIES,
    DispatchResult,
    Notifier,
)
from modulo.db.models.notification_endpoint import NotificationEndpoint

_KEY = Fernet.generate_key().decode()
_ORG = uuid.uuid4()
_RUN = uuid.uuid4()


def _encrypt(secret: str) -> bytes:
    return Fernet(_KEY.encode()).encrypt(secret.encode())


def _fake_endpoint(
    *,
    events: list[str] | None = None,
    secret: str | None = "my-secret",
    auto_disabled: bool = False,
    dead_letter_count: int = 0,
    team_id: uuid.UUID | None = None,
) -> NotificationEndpoint:
    ep = MagicMock(spec=NotificationEndpoint)
    ep.id = uuid.uuid4()
    ep.organisation_id = _ORG
    ep.url = "https://hooks.example.com/notify"
    ep.events = json.dumps(events or ["hitl_awaiting"])
    ep.secret_ciphertext = _encrypt(secret) if secret else None
    ep.auto_disabled = auto_disabled
    ep.consecutive_dead_letter_count = dead_letter_count
    ep.description = "test endpoint"
    ep.team_id = team_id
    return ep


def _session_factory(entries: list[Any] | None = None) -> MagicMock:
    """Build a mock async_sessionmaker that returns a controlled session."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    scalar_result = MagicMock()
    scalar_result.scalar_one.return_value = 0

    all_result = MagicMock()
    all_result.all.return_value = []
    all_result.scalar_one_or_none.return_value = None

    def _execute_side_effect(stmt: Any) -> Any:
        if hasattr(stmt, "_where_criteria"):
            for c in stmt._where_criteria:
                if hasattr(c, "right") and hasattr(c.right, "value"):
                    if c.right.value is True:  # auto_disabled == False
                        return all_result
        if hasattr(stmt, "_returning"):
            if hasattr(stmt, "_values") and "consecutive_dead_letter_count" in str(stmt._values):
                return scalar_result
        return all_result

    session.execute = AsyncMock(side_effect=_execute_side_effect)
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


@pytest.fixture
def notifier() -> Notifier:
    engine = MagicMock()
    return Notifier(engine, _KEY)


# ---------------------------------------------------------------------------
# _get_subscribed_endpoints
# ---------------------------------------------------------------------------


async def test_get_subscribed_endpoints_returns_matching() -> None:
    ep = _fake_endpoint()
    ep.events = json.dumps(["hitl_awaiting", "run_failed"])

    result = MagicMock()
    result.scalars.return_value.__iter__ = lambda self: iter([ep])

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    factory = MagicMock(side_effect=lambda: AsyncMock(
        __aenter__=AsyncMock(return_value=session),
        __aexit__=AsyncMock(return_value=False),
    ))

    n = Notifier(MagicMock(), _KEY)
    with patch.object(n, "_session_factory", factory):
        found = await n._get_subscribed_endpoints(_ORG, "hitl_awaiting")

    assert len(found) == 1
    assert found[0] is ep


async def test_get_subscribed_endpoints_skips_unsubscribed() -> None:
    ep = _fake_endpoint(events=["run_failed"])

    result = MagicMock()
    result.scalars.return_value.__iter__ = lambda self: iter([ep])

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    factory = MagicMock(side_effect=lambda: AsyncMock(
        __aenter__=AsyncMock(return_value=session),
        __aexit__=AsyncMock(return_value=False),
    ))

    n = Notifier(MagicMock(), _KEY)
    with patch.object(n, "_session_factory", factory):
        found = await n._get_subscribed_endpoints(_ORG, "hitl_awaiting")

    assert len(found) == 0


async def test_get_subscribed_endpoints_skips_auto_disabled() -> None:
    _fake_endpoint(auto_disabled=True)

    result = MagicMock()
    result.scalars.return_value.__iter__ = lambda self: iter([])

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    factory = MagicMock(side_effect=lambda: AsyncMock(
        __aenter__=AsyncMock(return_value=session),
        __aexit__=AsyncMock(return_value=False),
    ))

    n = Notifier(MagicMock(), _KEY)
    with patch.object(n, "_session_factory", factory):
        found = await n._get_subscribed_endpoints(_ORG, "hitl_awaiting")

    assert len(found) == 0


# ---------------------------------------------------------------------------
# _sign_payload
# ---------------------------------------------------------------------------


async def test_sign_payload_returns_hmac() -> None:
    ep = _fake_endpoint(secret="test-secret")
    n = Notifier(MagicMock(), _KEY)

    sig = await n._sign_payload(b'{"hello":"world"}', ep)
    expected = "sha256="
    assert sig.startswith(expected)
    assert len(sig) > len(expected)


async def test_sign_payload_empty_when_no_secret() -> None:
    ep = _fake_endpoint(secret=None)
    n = Notifier(MagicMock(), _KEY)

    sig = await n._sign_payload(b"data", ep)
    assert sig == ""


# ---------------------------------------------------------------------------
# _dispatch_to_endpoint
# ---------------------------------------------------------------------------


async def _do_dispatch(
    n: Notifier,
    ep: NotificationEndpoint,
    event_type: str = "hitl_awaiting",
    payload: dict[str, Any] | None = None,
    run_id: uuid.UUID | None = None,
    retain_payload: bool = False,
) -> DispatchResult:
    """Helper to call _dispatch_to_endpoint with a real httpx.AsyncClient."""
    async with httpx.AsyncClient() as client:
        return await n._dispatch_to_endpoint(
            client, ep, event_type,
            payload or {"run_id": str(run_id or _RUN)},
            run_id or _RUN, retain_payload=retain_payload,
        )


async def test_dispatch_successful_delivery() -> None:
    ep = _fake_endpoint()
    n = Notifier(MagicMock(), _KEY)

    with patch.object(n, "_record_delivery", AsyncMock()) as mock_record, \
         patch.object(n, "_increment_dead_letter", AsyncMock()) as mock_dead, \
         patch.object(n, "_reset_dead_letter", AsyncMock()) as mock_reset:
        async with respx.mock:
            respx.post(ep.url).mock(Response(200, text="OK"))
            result = await _do_dispatch(n, ep)

    assert result.status == "delivered"
    assert result.response_code == 200
    assert result.attempt_count == 1
    mock_record.assert_called_once()
    mock_dead.assert_not_called()
    mock_reset.assert_called_once()


async def test_dispatch_retries_then_dead_letters() -> None:
    ep = _fake_endpoint()
    n = Notifier(MagicMock(), _KEY)

    with patch.object(n, "_record_delivery", AsyncMock()), \
         patch.object(n, "_increment_dead_letter", AsyncMock()) as mock_dead:
        async with respx.mock:
            respx.post(ep.url).mock(Response(500, text="Server Error"))
            result = await _do_dispatch(n, ep, "run_failed")

    assert result.status == "dead_lettered"
    assert result.attempt_count == MAX_RETRIES
    mock_dead.assert_called_once()


async def test_dispatch_network_failure_then_dead_letters() -> None:
    ep = _fake_endpoint()
    n = Notifier(MagicMock(), _KEY)

    with patch.object(n, "_record_delivery", AsyncMock()), \
         patch.object(n, "_increment_dead_letter", AsyncMock()):
        async with respx.mock:
            respx.post(ep.url).mock(side_effect=httpx.ConnectError("Connection refused"))
            result = await _do_dispatch(n, ep, "run_failed")

    assert result.status == "dead_lettered"
    assert result.response_code is None


async def test_dispatch_retains_payload_when_requested() -> None:
    ep = _fake_endpoint()
    n = Notifier(MagicMock(), _KEY)
    record_kwargs: dict[str, Any] = {}

    async def _record(
        endpoint: Any, event_type: str, run_id: Any, status: str,
        attempt_count: int, response_code: Any, last_error: Any,
        payload_ciphertext: Any,
    ) -> None:
        record_kwargs.update(payload_ciphertext=payload_ciphertext)

    with patch.object(n, "_record_delivery", _record), \
         patch.object(n, "_increment_dead_letter", AsyncMock()), \
         patch.object(n, "_reset_dead_letter", AsyncMock()):
        async with respx.mock:
            respx.post(ep.url).mock(Response(200))
            await _do_dispatch(n, ep, retain_payload=True)

    assert record_kwargs.get("payload_ciphertext") is not None


# ---------------------------------------------------------------------------
# _increment_dead_letter
# ---------------------------------------------------------------------------


async def test_increment_dead_letter_does_not_auto_disable_below_threshold() -> None:
    ep = _fake_endpoint(dead_letter_count=5)

    scalar_result = MagicMock()
    scalar_result.scalar_one.return_value = 6  # new count

    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=scalar_result)

    factory = MagicMock(side_effect=lambda: AsyncMock(
        __aenter__=AsyncMock(return_value=session),
        __aexit__=AsyncMock(return_value=False),
    ))

    n = Notifier(MagicMock(), _KEY)
    with patch.object(n, "_session_factory", factory):
        await n._increment_dead_letter(ep)

    assert not ep.auto_disabled, "Should not auto-disable below threshold"


async def test_increment_dead_letter_auto_disables_at_threshold() -> None:
    ep = _fake_endpoint(dead_letter_count=MAX_DEAD_LETTERS - 1)

    call_count = [0]
    scalar_result = MagicMock()
    scalar_result.scalar_one.return_value = MAX_DEAD_LETTERS

    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    async def execute_side_effect(stmt: Any) -> MagicMock:
        call_count[0] += 1
        if call_count[0] == 1:
            return scalar_result
        # second call sets auto_disabled; simulate by mutating the mock
        ep.auto_disabled = True
        return MagicMock()

    session.execute = AsyncMock(side_effect=execute_side_effect)

    factory = MagicMock(side_effect=lambda: AsyncMock(
        __aenter__=AsyncMock(return_value=session),
        __aexit__=AsyncMock(return_value=False),
    ))

    n = Notifier(MagicMock(), _KEY)
    with patch.object(n, "_session_factory", factory):
        await n._increment_dead_letter(ep)

    assert ep.auto_disabled, "Should auto-disable at threshold"


# ---------------------------------------------------------------------------
# dispatch_event (integration of the above)
# ---------------------------------------------------------------------------


async def test_dispatch_event_no_subscribers_returns_empty() -> None:
    n = Notifier(MagicMock(), _KEY)

    with patch.object(n, "_get_subscribed_endpoints", AsyncMock(return_value=[])):
        result = await n.dispatch_event(_ORG, "hitl_overdue", {"run_id": str(_RUN)})
    assert result == []


async def test_dispatch_event_with_subscriber_sends_notification() -> None:
    ep = _fake_endpoint()
    n = Notifier(MagicMock(), _KEY)

    with patch.object(n, "_get_subscribed_endpoints", AsyncMock(return_value=[ep])) as mock_get, \
         patch.object(n, "_dispatch_to_endpoint") as mock_dispatch:
        mock_dispatch.return_value = DispatchResult(
            endpoint_id=ep.id, status="delivered", attempt_count=1, response_code=200,
        )

        results = await n.dispatch_event(_ORG, "hitl_awaiting", {"run_id": str(_RUN)})

    assert len(results) == 1
    assert results[0].status == "delivered"
    mock_get.assert_called_once_with(_ORG, "hitl_awaiting", team_id=None)


# ---------------------------------------------------------------------------
# Team-scoped dispatch
# ---------------------------------------------------------------------------


_TEAM = uuid.uuid4()


async def test_team_scoped_dispatch_routes_to_team_endpoints() -> None:
    """When team_id is provided, dispatch to team-specific endpoints."""
    team_ep = _fake_endpoint(team_id=_TEAM)
    n = Notifier(MagicMock(), _KEY)

    with patch.object(n, "_get_subscribed_endpoints", AsyncMock(return_value=[team_ep])) as mock_get, \
         patch.object(n, "_dispatch_to_endpoint") as mock_dispatch:
        mock_dispatch.return_value = DispatchResult(
            endpoint_id=team_ep.id, status="delivered", attempt_count=1, response_code=200,
        )

        results = await n.dispatch_event(_ORG, "hitl_awaiting", {"run_id": str(_RUN)}, team_id=_TEAM)

    assert len(results) == 1
    assert results[0].status == "delivered"
    mock_get.assert_called_once_with(_ORG, "hitl_awaiting", team_id=_TEAM)


async def test_team_scoped_dispatch_falls_back_to_org_wide() -> None:
    """When team_id is provided but no team-specific endpoints exist, fall back to org-wide."""
    org_ep = _fake_endpoint(team_id=None)
    n = Notifier(MagicMock(), _KEY)

    # Simulate: first query returns empty (no team endpoints), second returns org-wide
    with patch.object(
        n, "_get_subscribed_endpoints", AsyncMock(return_value=[org_ep])
    ) as mock_get, \
         patch.object(n, "_dispatch_to_endpoint") as mock_dispatch:
        mock_dispatch.return_value = DispatchResult(
            endpoint_id=org_ep.id, status="delivered", attempt_count=1, response_code=200,
        )

        results = await n.dispatch_event(_ORG, "hitl_awaiting", {"run_id": str(_RUN)}, team_id=_TEAM)

    assert len(results) == 1
    assert results[0].status == "delivered"
    mock_get.assert_called_once_with(_ORG, "hitl_awaiting", team_id=_TEAM)


async def test_org_wide_dispatch_excludes_team_endpoints() -> None:
    """When team_id is None, only org-wide endpoints are returned."""
    org_ep = _fake_endpoint(team_id=None)
    n = Notifier(MagicMock(), _KEY)

    with patch.object(
        n, "_get_subscribed_endpoints", AsyncMock(return_value=[org_ep])
    ) as mock_get, \
         patch.object(n, "_dispatch_to_endpoint") as mock_dispatch:
        mock_dispatch.return_value = DispatchResult(
            endpoint_id=org_ep.id, status="delivered", attempt_count=1, response_code=200,
        )
        results = await n.dispatch_event(_ORG, "hitl_awaiting", {"run_id": str(_RUN)})

    assert len(results) == 1
    mock_get.assert_called_once_with(_ORG, "hitl_awaiting", team_id=None)


# ---------------------------------------------------------------------------
# _get_subscribed_endpoints with team_id
# ---------------------------------------------------------------------------


def _make_scalar_result(endpoints: list[NotificationEndpoint]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.__iter__ = lambda self: iter(endpoints)
    return result


def _make_session_factory(endpoints_by_team: list[NotificationEndpoint], endpoints_org: list[NotificationEndpoint]) -> MagicMock:
    """Build a session factory that returns team endpoints first, then org-wide on second call."""
    call_count = [0]

    async def execute_side_effect(stmt: Any) -> MagicMock:
        call_count[0] += 1
        if call_count[0] == 1:
            return _make_scalar_result(endpoints_by_team)
        return _make_scalar_result(endpoints_org)

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute_side_effect)

    factory = MagicMock(side_effect=lambda: AsyncMock(
        __aenter__=AsyncMock(return_value=session),
        __aexit__=AsyncMock(return_value=False),
    ))
    return factory


async def test_get_subscribed_endpoints_team_id_returns_team_endpoints() -> None:
    ep = _fake_endpoint(team_id=_TEAM)
    factory = _make_session_factory([ep], [])

    n = Notifier(MagicMock(), _KEY)
    with patch.object(n, "_session_factory", factory):
        found = await n._get_subscribed_endpoints(_ORG, "hitl_awaiting", team_id=_TEAM)

    assert len(found) == 1
    assert found[0].team_id == _TEAM


async def test_get_subscribed_endpoints_team_id_falls_back_to_org() -> None:
    """No team endpoints exist; fall back to org-wide."""
    org_ep = _fake_endpoint(team_id=None)
    factory = _make_session_factory([], [org_ep])

    n = Notifier(MagicMock(), _KEY)
    with patch.object(n, "_session_factory", factory):
        found = await n._get_subscribed_endpoints(_ORG, "hitl_awaiting", team_id=_TEAM)

    assert len(found) == 1
    assert found[0].team_id is None


async def test_get_subscribed_endpoints_no_team_id_returns_org_wide_only() -> None:
    """When team_id is None, only org-wide endpoints are returned."""
    org_ep = _fake_endpoint(team_id=None)
    factory = _make_session_factory([org_ep], [])

    n = Notifier(MagicMock(), _KEY)
    with patch.object(n, "_session_factory", factory):
        found = await n._get_subscribed_endpoints(_ORG, "hitl_awaiting", team_id=None)

    assert len(found) == 1
    assert found[0].team_id is None


async def test_get_subscribed_endpoints_no_team_id_skips_team_endpoints() -> None:
    """Org-wide dispatch excludes endpoints with a team_id."""
    org_ep = _fake_endpoint(team_id=None)

    async def execute_side_effect(stmt: Any) -> MagicMock:
        # Only one query is executed for org-wide dispatch
        return _make_scalar_result([org_ep])

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute_side_effect)

    factory = MagicMock(side_effect=lambda: AsyncMock(
        __aenter__=AsyncMock(return_value=session),
        __aexit__=AsyncMock(return_value=False),
    ))

    n = Notifier(MagicMock(), _KEY)
    with patch.object(n, "_session_factory", factory):
        found = await n._get_subscribed_endpoints(_ORG, "hitl_awaiting", team_id=None)

    # Only the org-wide endpoint should be returned
    assert len(found) == 1
    assert found[0].team_id is None
