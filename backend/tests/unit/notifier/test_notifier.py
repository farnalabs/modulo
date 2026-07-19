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
    MAX_ATTEMPTS,
    MAX_DEAD_LETTERS,
    DispatchResult,
    Notifier,
)
from modulo.db.models.notification_endpoint import NotificationEndpoint

_KEY = Fernet.generate_key().decode()
_ORG = uuid.uuid4()
_RUN = uuid.uuid4()


def _configure_rls_session(session: AsyncMock) -> None:
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=bind)
    session.info = {}


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
    ep.events = events or ["hitl_awaiting"]
    ep.secret_ciphertext = _encrypt(secret) if secret else None
    ep.auto_disabled = auto_disabled
    ep.consecutive_dead_letter_count = dead_letter_count
    ep.description = "test endpoint"
    ep.team_id = team_id
    return ep


async def _get_endpoints_for_event(
    endpoints: list[NotificationEndpoint],
    event_type: str = "hitl_awaiting",
    team_id: uuid.UUID | None = None,
) -> list[NotificationEndpoint]:
    """Helper: create a mock session, call _get_subscribed_endpoints, return results."""
    result = MagicMock()
    result.scalars.return_value.__iter__ = lambda self: iter(endpoints)

    session = AsyncMock()
    _configure_rls_session(session)
    session.execute = AsyncMock(return_value=result)

    factory = MagicMock(
        side_effect=lambda: AsyncMock(
            __aenter__=AsyncMock(return_value=session),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    n = Notifier(MagicMock(), _KEY)
    with patch.object(n, "_session_factory", factory):
        return await n._get_subscribed_endpoints(_ORG, event_type, team_id=team_id)


@pytest.fixture
def notifier() -> Notifier:
    engine = MagicMock()
    return Notifier(engine, _KEY)


# ---------------------------------------------------------------------------
# _get_subscribed_endpoints
# ---------------------------------------------------------------------------


async def test_get_subscribed_endpoints_returns_matching() -> None:
    ep = _fake_endpoint()
    ep.events = ["hitl_awaiting", "run_failed"]
    found = await _get_endpoints_for_event([ep])
    assert len(found) == 1
    assert found[0] is ep


async def test_get_subscribed_endpoints_skips_unsubscribed() -> None:
    ep = _fake_endpoint(events=["run_failed"])
    found = await _get_endpoints_for_event([ep])
    assert len(found) == 0


async def test_get_subscribed_endpoints_skips_auto_disabled() -> None:
    found = await _get_endpoints_for_event([])
    assert len(found) == 0


# ---------------------------------------------------------------------------
# _sign_payload
# ---------------------------------------------------------------------------


async def test_sign_payload_returns_hmac(notifier: Notifier) -> None:
    ep = _fake_endpoint(secret="test-secret")

    sig = await notifier._sign_payload(b'{"hello":"world"}', ep)
    expected = "sha256="
    assert sig.startswith(expected)
    assert len(sig) > len(expected)


async def test_sign_payload_empty_when_no_secret(notifier: Notifier) -> None:
    ep = _fake_endpoint(secret=None)

    sig = await notifier._sign_payload(b"data", ep)
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
    body = json.dumps(
        {
            "event": event_type,
            "payload": payload or {"run_id": str(run_id or _RUN)},
        },
        default=str,
        separators=(",", ":"),
    ).encode()
    async with httpx.AsyncClient() as client:
        return await n._dispatch_to_endpoint(
            client,
            ep,
            event_type,
            body,
            run_id or _RUN,
            retain_payload=retain_payload,
        )


async def test_dispatch_successful_delivery(notifier: Notifier) -> None:
    ep = _fake_endpoint()

    with (
        patch.object(notifier, "_record_delivery", AsyncMock()) as mock_record,
        patch.object(notifier, "_increment_dead_letter", AsyncMock()) as mock_dead,
        patch.object(notifier, "_reset_dead_letter", AsyncMock()) as mock_reset,
    ):
        async with respx.mock:
            respx.post(ep.url).mock(Response(200, text="OK"))
            result = await _do_dispatch(notifier, ep)

    assert result.status == "delivered"
    assert result.response_code == 200
    assert result.attempt_count == 1
    mock_record.assert_called_once()
    mock_dead.assert_not_called()
    mock_reset.assert_called_once()


async def test_dispatch_retries_then_dead_letters(notifier: Notifier) -> None:
    ep = _fake_endpoint()

    with (
        patch.object(notifier, "_record_delivery", AsyncMock()),
        patch.object(notifier, "_increment_dead_letter", AsyncMock()) as mock_dead,
    ):
        async with respx.mock:
            respx.post(ep.url).mock(Response(500, text="Server Error"))
            result = await _do_dispatch(notifier, ep, "run_failed")

    assert result.status == "dead_lettered"
    assert result.attempt_count == MAX_ATTEMPTS
    mock_dead.assert_called_once()


async def test_dispatch_network_failure_then_dead_letters(notifier: Notifier) -> None:
    ep = _fake_endpoint()

    with (
        patch.object(notifier, "_record_delivery", AsyncMock()),
        patch.object(notifier, "_increment_dead_letter", AsyncMock()),
    ):
        async with respx.mock:
            respx.post(ep.url).mock(side_effect=httpx.ConnectError("Connection refused"))
            result = await _do_dispatch(notifier, ep, "run_failed")

    assert result.status == "dead_lettered"
    assert result.response_code is None


async def test_dispatch_retains_payload_when_requested(notifier: Notifier) -> None:
    ep = _fake_endpoint()
    record_kwargs: dict[str, Any] = {}

    async def _record(
        endpoint: Any,
        event_type: str,
        run_id: Any,
        status: str,
        attempt_count: int,
        response_code: Any,
        last_error: Any,
        payload_ciphertext: Any,
    ) -> None:
        record_kwargs.update(payload_ciphertext=payload_ciphertext)

    with (
        patch.object(notifier, "_record_delivery", _record),
        patch.object(notifier, "_increment_dead_letter", AsyncMock()),
        patch.object(notifier, "_reset_dead_letter", AsyncMock()),
    ):
        async with respx.mock:
            respx.post(ep.url).mock(Response(200))
            await _do_dispatch(notifier, ep, retain_payload=True)

    assert record_kwargs.get("payload_ciphertext") is not None


# ---------------------------------------------------------------------------
# _increment_dead_letter
# ---------------------------------------------------------------------------


async def test_increment_dead_letter_does_not_auto_disable_below_threshold(notifier: Notifier) -> None:
    ep = _fake_endpoint(dead_letter_count=5)

    scalar_result = MagicMock()
    scalar_result.scalar_one.return_value = 6  # new count

    session = AsyncMock()
    _configure_rls_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=scalar_result)

    factory = MagicMock(
        side_effect=lambda: AsyncMock(
            __aenter__=AsyncMock(return_value=session),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    with patch.object(notifier, "_session_factory", factory):
        await notifier._increment_dead_letter(ep)

    assert not ep.auto_disabled, "Should not auto-disable below threshold"


async def test_increment_dead_letter_auto_disables_at_threshold(notifier: Notifier) -> None:
    ep = _fake_endpoint(dead_letter_count=MAX_DEAD_LETTERS - 1)

    scalar_result = MagicMock()
    scalar_result.scalar_one.return_value = MAX_DEAD_LETTERS

    session = AsyncMock()
    _configure_rls_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    call_count = 0

    async def execute_side_effect(stmt: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return scalar_result
        ep.auto_disabled = True
        return MagicMock()

    session.execute = AsyncMock(side_effect=execute_side_effect)

    factory = MagicMock(
        side_effect=lambda: AsyncMock(
            __aenter__=AsyncMock(return_value=session),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    with patch.object(notifier, "_session_factory", factory):
        await notifier._increment_dead_letter(ep)

    assert ep.auto_disabled, "Should auto-disable at threshold"


# ---------------------------------------------------------------------------
# dispatch_event (integration of the above)
# ---------------------------------------------------------------------------


async def test_dispatch_event_no_subscribers_returns_empty(notifier: Notifier) -> None:
    with patch.object(notifier, "_get_subscribed_endpoints", AsyncMock(return_value=[])):
        result = await notifier.dispatch_event(_ORG, "hitl_overdue", {"run_id": str(_RUN)})
    assert result == []


async def test_dispatch_event_with_subscriber_sends_notification(notifier: Notifier) -> None:
    ep = _fake_endpoint()

    with (
        patch.object(notifier, "_get_subscribed_endpoints", AsyncMock(return_value=[ep])) as mock_get,
        patch.object(notifier, "_dispatch_to_endpoint") as mock_dispatch,
    ):
        mock_dispatch.return_value = DispatchResult(
            endpoint_id=ep.id,
            status="delivered",
            attempt_count=1,
            response_code=200,
        )

        results = await notifier.dispatch_event(_ORG, "hitl_awaiting", {"run_id": str(_RUN)})

    assert len(results) == 1
    assert results[0].status == "delivered"
    mock_get.assert_called_once_with(_ORG, "hitl_awaiting", team_id=None)


# ---------------------------------------------------------------------------
# Team-scoped dispatch
# ---------------------------------------------------------------------------


_TEAM = uuid.uuid4()


@pytest.mark.parametrize(
    "team_id,expected_team_id",
    [
        (_TEAM, _TEAM),
        (_TEAM, None),
        (None, None),
    ],
)
async def test_team_scoped_dispatch(notifier: Notifier, team_id, expected_team_id) -> None:
    ep = _fake_endpoint(team_id=expected_team_id)
    with (
        patch.object(notifier, "_get_subscribed_endpoints", AsyncMock(return_value=[ep])) as mock_get,
        patch.object(notifier, "_dispatch_to_endpoint") as mock_dispatch,
    ):
        mock_dispatch.return_value = DispatchResult(
            endpoint_id=ep.id,
            status="delivered",
            attempt_count=1,
            response_code=200,
        )
        results = await notifier.dispatch_event(_ORG, "hitl_awaiting", {"run_id": str(_RUN)}, team_id=team_id)
    assert len(results) == 1
    assert results[0].status == "delivered"
    mock_get.assert_called_once_with(_ORG, "hitl_awaiting", team_id=team_id)


# ---------------------------------------------------------------------------
# _get_subscribed_endpoints with team_id
# ---------------------------------------------------------------------------


def _make_scalar_result(endpoints: list[NotificationEndpoint]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.__iter__ = lambda self: iter(endpoints)
    return result


def _make_session_factory(
    endpoints_by_team: list[NotificationEndpoint], endpoints_org: list[NotificationEndpoint]
) -> MagicMock:
    """Build a session factory that returns team endpoints first, then org-wide on second call."""
    call_count = 0

    async def execute_side_effect(stmt: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_scalar_result(endpoints_by_team)
        return _make_scalar_result(endpoints_org)

    session = AsyncMock()
    _configure_rls_session(session)
    session.execute = AsyncMock(side_effect=execute_side_effect)

    return MagicMock(
        side_effect=lambda: AsyncMock(
            __aenter__=AsyncMock(return_value=session),
            __aexit__=AsyncMock(return_value=False),
        )
    )


@pytest.mark.parametrize(
    "team_id,team_endpoints,org_endpoints,expected_count,expected_team",
    [
        (_TEAM, [_fake_endpoint(team_id=_TEAM)], [], 1, _TEAM),
        (_TEAM, [], [_fake_endpoint(team_id=None)], 1, None),
        (None, [_fake_endpoint(team_id=None)], [], 1, None),
    ],
)
async def test_get_subscribed_endpoints(
    notifier: Notifier, team_id, team_endpoints, org_endpoints, expected_count, expected_team
) -> None:
    factory = _make_session_factory(team_endpoints, org_endpoints)
    with patch.object(notifier, "_session_factory", factory):
        found = await notifier._get_subscribed_endpoints(_ORG, "hitl_awaiting", team_id=team_id)
    assert len(found) == expected_count
    assert found[0].team_id == expected_team
