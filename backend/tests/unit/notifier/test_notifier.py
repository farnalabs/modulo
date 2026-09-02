"""Unit tests for Notifier dispatch, HMAC signing, retry, and dead-letter logic."""

import asyncio
import hashlib
import hmac
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
    RETRY_DELAYS,
    DispatchResult,
    Notifier,
)
from modulo.core.notifier.event_mapper import NotificationEventMapper
from modulo.db.models.notification_delivery import NotificationDeliveryLog
from modulo.db.models.notification_endpoint import NotificationEndpoint

_KEY = Fernet.generate_key().decode()
_ORG = uuid.uuid4()
_RUN = uuid.uuid4()


def _configure_rls_session(session: AsyncMock) -> MagicMock:
    """Configure a mock async session for ``set_rls_org`` + an explicit transaction.

    ``set_rls_org`` requires an active transaction (RuntimeError otherwise);
    the sqlite-dialect bind makes it store the org id in ``session.info``
    instead of calling Postgres ``set_config``. ``session.begin()`` is wired to
    a synchronous-callable returning the context manager (mirroring
    ``AsyncSession.begin()``). Returns the ``session.begin()`` context manager
    so tests can assert the caller opened an explicit transaction.
    """
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=bind)
    session.info = {}
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return begin_cm


def _make_db_session(
    *,
    execute: AsyncMock | None = None,
    add: MagicMock | None = None,
) -> tuple[AsyncMock, MagicMock]:
    """Build a mock session + factory for one ``async with session, session.begin()`` pass.

    ``session.begin()`` is pre-wired so both ``_record_delivery``/``_reset_dead_letter``
    style methods and the in-app notification block inside ``_dispatch_inline`` can use it.
    """
    session = AsyncMock()
    _configure_rls_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    if execute is not None:
        session.execute = execute
    if add is not None:
        session.add = add
    factory = MagicMock(
        side_effect=lambda: AsyncMock(
            __aenter__=AsyncMock(return_value=session),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    return session, factory


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
    assert not found


async def test_get_subscribed_endpoints_filters_auto_disabled_in_query() -> None:
    """The generated query excludes auto-disabled endpoints (SQL-level filter)."""
    result = MagicMock()
    result.scalars.return_value.__iter__ = lambda self: iter([_fake_endpoint()])

    session = AsyncMock()
    _configure_rls_session(session)
    executed: list[Any] = []

    async def _capture(stmt: Any) -> MagicMock:
        executed.append(stmt)
        return result

    session.execute = AsyncMock(side_effect=_capture)

    factory = MagicMock(
        side_effect=lambda: AsyncMock(
            __aenter__=AsyncMock(return_value=session),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    n = Notifier(MagicMock(), _KEY)
    with patch.object(n, "_session_factory", factory):
        found = await n._get_subscribed_endpoints(_ORG, "hitl_awaiting")

    assert len(found) == 1
    assert executed, "expected an executed query"
    assert "auto_disabled" in str(executed[0])


async def test_get_subscribed_endpoints_sets_rls_org_context() -> None:
    """FAR-523 regression: the endpoint read must activate the org's RLS
    context. ``notification_endpoints`` carries the ``rls_org_isolation``
    policy and ``modulo_app`` is NOBYPASSRLS, so a query without
    ``app.organisation_id`` set silently matches ZERO rows — dispatch would
    deliver to nobody."""
    ep = _fake_endpoint()

    with patch("modulo.core.notifier.set_rls_org", AsyncMock()) as mock_set_rls:
        found = await _get_endpoints_for_event([ep])

    assert len(found) == 1
    mock_set_rls.assert_awaited_once()
    # First arg is the session; the org id is the RLS context passed second.
    assert mock_set_rls.await_args.args[1] == _ORG


async def test_get_subscribed_endpoints_runs_in_explicit_transaction() -> None:
    """FAR-523: the read must open an explicit transaction AND pin the org
    context inside it — ``set_rls_org`` is transaction-scoped (SET LOCAL
    semantics), so the execute must happen inside the same BEGIN block. Uses
    the real ``set_rls_org`` (sqlite dialect stores the org in
    ``session.info``) so the full call chain is exercised."""
    ep = _fake_endpoint()
    result = MagicMock()
    result.scalars.return_value.__iter__ = lambda self: iter([ep])

    session = AsyncMock()
    begin_cm = _configure_rls_session(session)
    session.execute = AsyncMock(return_value=result)

    factory = MagicMock(
        side_effect=lambda: AsyncMock(
            __aenter__=AsyncMock(return_value=session),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    n = Notifier(MagicMock(), _KEY)
    with patch.object(n, "_session_factory", factory):
        found = await n._get_subscribed_endpoints(_ORG, "hitl_awaiting")

    assert len(found) == 1
    session.begin.assert_called_once()
    begin_cm.__aenter__.assert_awaited_once()
    assert session.info["org_id"] == _ORG


# ---------------------------------------------------------------------------
# _filter_subscribed
# ---------------------------------------------------------------------------


def _make_filter_endpoint(events: Any) -> NotificationEndpoint:
    ep = _fake_endpoint()
    ep.events = events
    return ep


def test_filter_subscribed_accepts_list() -> None:
    ep = _make_filter_endpoint(["hitl_awaiting", "run_failed"])
    n = Notifier(MagicMock(), _KEY)
    assert n._filter_subscribed([ep], "run_failed") == [ep]
    assert not n._filter_subscribed([ep], "hitl_overdue")


def test_filter_subscribed_parses_json_string_events() -> None:
    ep = _make_filter_endpoint('["hitl_awaiting","run_failed"]')
    n = Notifier(MagicMock(), _KEY)
    assert n._filter_subscribed([ep], "run_failed") == [ep]


def test_filter_subscribed_skips_unparseable_events() -> None:
    ep = _make_filter_endpoint("not-json")
    n = Notifier(MagicMock(), _KEY)
    assert not n._filter_subscribed([ep], "run_failed")


# ---------------------------------------------------------------------------
# _sign_payload
# ---------------------------------------------------------------------------


async def test_sign_payload_returns_hmac(notifier: Notifier) -> None:
    ep = _fake_endpoint(secret="test-secret")

    sig = await notifier._sign_payload(b'{"hello":"world"}', ep)
    expected = "sha256="
    assert sig.startswith(expected)
    assert len(sig) > len(expected)


async def test_sign_payload_matches_expected_hmac(notifier: Notifier) -> None:
    ep = _fake_endpoint(secret="test-secret")
    body = b'{"hello":"world"}'

    sig = await notifier._sign_payload(body, ep)
    expected_digest = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    assert sig == f"sha256={expected_digest}"


async def test_sign_payload_empty_when_no_secret(notifier: Notifier) -> None:
    ep = _fake_endpoint(secret=None)

    sig = await notifier._sign_payload(b"data", ep)
    assert sig == ""


async def test_sign_payload_empty_when_secret_corrupt(notifier: Notifier) -> None:
    """A ciphertext that cannot be decrypted must produce no signature."""
    ep = _fake_endpoint(secret="test-secret")
    ep.secret_ciphertext = b"not-valid-fernet-ciphertext"

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

    ciphertext = record_kwargs.get("payload_ciphertext")
    assert ciphertext is not None
    body = json.dumps(
        {
            "event": "hitl_awaiting",
            "payload": {"run_id": str(_RUN)},
        },
        default=str,
        separators=(",", ":"),
    ).encode()
    assert Fernet(_KEY.encode()).decrypt(ciphertext) == body


async def test_dispatch_does_not_retain_payload_unless_requested(notifier: Notifier) -> None:
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
            await _do_dispatch(notifier, ep, retain_payload=False)

    assert record_kwargs.get("payload_ciphertext") is None


async def test_dispatch_429_honors_retry_after_before_dead_letter(notifier: Notifier) -> None:
    """A 429 with Retry-After should use that delay instead of the default backoff."""
    ep = _fake_endpoint()
    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    with (
        patch.object(notifier, "_record_delivery", AsyncMock()),
        patch.object(notifier, "_increment_dead_letter", AsyncMock()),
        patch("modulo.core.notifier.asyncio.sleep", _fake_sleep),
    ):
        async with respx.mock:
            respx.post(ep.url).mock(Response(429, headers={"Retry-After": "3"}))
            result = await _do_dispatch(notifier, ep)

    assert result.status == "dead_lettered"
    assert sleeps, "expected at least one retry delay"
    assert sleeps[0] == 3.0


async def test_dispatch_429_retry_after_capped_at_60(notifier: Notifier) -> None:
    """A Retry-After larger than 60s must be clamped to 60s."""
    ep = _fake_endpoint()
    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    with (
        patch.object(notifier, "_record_delivery", AsyncMock()),
        patch.object(notifier, "_increment_dead_letter", AsyncMock()),
        patch("modulo.core.notifier.asyncio.sleep", _fake_sleep),
    ):
        async with respx.mock:
            respx.post(ep.url).mock(Response(429, headers={"Retry-After": "120"}))
            await _do_dispatch(notifier, ep)

    assert sleeps, "expected at least one retry delay"
    assert sleeps == [60.0] * (MAX_ATTEMPTS - 1)


async def test_dispatch_429_invalid_retry_after_uses_default_backoff(notifier: Notifier) -> None:
    """A non-numeric Retry-After must fall back to the default backoff schedule."""
    ep = _fake_endpoint()
    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    with (
        patch.object(notifier, "_record_delivery", AsyncMock()),
        patch.object(notifier, "_increment_dead_letter", AsyncMock()),
        patch("modulo.core.notifier.asyncio.sleep", _fake_sleep),
    ):
        async with respx.mock:
            respx.post(ep.url).mock(Response(429, headers={"Retry-After": "soon"}))
            await _do_dispatch(notifier, ep)

    assert sleeps == RETRY_DELAYS


async def test_dispatch_429_missing_retry_after_uses_default_backoff(notifier: Notifier) -> None:
    """A 429 without a Retry-After header must use the default backoff schedule."""
    ep = _fake_endpoint()
    sleeps: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    with (
        patch.object(notifier, "_record_delivery", AsyncMock()),
        patch.object(notifier, "_increment_dead_letter", AsyncMock()),
        patch("modulo.core.notifier.asyncio.sleep", _fake_sleep),
    ):
        async with respx.mock:
            respx.post(ep.url).mock(Response(429))
            await _do_dispatch(notifier, ep)

    assert sleeps == RETRY_DELAYS


async def test_dispatch_retain_payload_re_raises_cancelled_error_on_encrypt(notifier: Notifier) -> None:
    """A cancellation while encrypting the retained payload must propagate."""
    ep = _fake_endpoint(secret=None)
    notifier._fernet = MagicMock()
    notifier._fernet.encrypt = MagicMock(side_effect=asyncio.CancelledError)

    with (
        patch.object(notifier, "_record_delivery", AsyncMock()),
        patch.object(notifier, "_increment_dead_letter", AsyncMock()),
        patch.object(notifier, "_reset_dead_letter", AsyncMock()),
        pytest.raises(asyncio.CancelledError),
    ):
        async with respx.mock:
            respx.post(ep.url).mock(Response(200))
            await _do_dispatch(notifier, ep, retain_payload=True)


async def test_dispatch_retain_payload_encrypt_failure_logs_and_continues(notifier: Notifier) -> None:
    """A failed payload encryption must be logged but must not drop the delivery."""
    ep = _fake_endpoint(secret=None)
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

    notifier._fernet = MagicMock()
    notifier._fernet.encrypt = MagicMock(side_effect=RuntimeError("crypto unavailable"))

    with (
        patch.object(notifier, "_record_delivery", _record),
        patch.object(notifier, "_increment_dead_letter", AsyncMock()),
        patch.object(notifier, "_reset_dead_letter", AsyncMock()),
        patch("modulo.core.notifier._log.exception") as mock_log,
    ):
        async with respx.mock:
            respx.post(ep.url).mock(Response(200))
            result = await _do_dispatch(notifier, ep, retain_payload=True)

    assert result.status == "delivered"
    assert record_kwargs.get("payload_ciphertext") is None
    mock_log.assert_called_once()
    assert mock_log.call_args.args[0] == "notifier.encrypt_failed"


# ---------------------------------------------------------------------------
# _increment_dead_letter
# ---------------------------------------------------------------------------


async def test_increment_dead_letter_does_not_auto_disable_below_threshold(notifier: Notifier) -> None:
    ep = _fake_endpoint(dead_letter_count=5)

    scalar_result = MagicMock()
    scalar_result.scalar_one.return_value = 6  # new count

    _, factory = _make_db_session(execute=AsyncMock(return_value=scalar_result))

    with patch.object(notifier, "_session_factory", factory):
        await notifier._increment_dead_letter(ep)

    assert not ep.auto_disabled, "Should not auto-disable below threshold"


async def test_increment_dead_letter_auto_disables_at_threshold(notifier: Notifier) -> None:
    ep = _fake_endpoint(dead_letter_count=MAX_DEAD_LETTERS - 1)

    scalar_result = MagicMock()
    scalar_result.scalar_one.return_value = MAX_DEAD_LETTERS

    call_count = 0

    async def execute_side_effect(stmt: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return scalar_result
        ep.auto_disabled = True
        return MagicMock()

    _, factory = _make_db_session(execute=AsyncMock(side_effect=execute_side_effect))

    with patch.object(notifier, "_session_factory", factory):
        await notifier._increment_dead_letter(ep)

    assert ep.auto_disabled, "Should auto-disable at threshold"


async def test_increment_dead_letter_re_raises_cancelled_error(notifier: Notifier) -> None:
    """A cancellation during the dead-letter UPDATE must propagate."""
    ep = _fake_endpoint()
    _, factory = _make_db_session(execute=AsyncMock(side_effect=asyncio.CancelledError))

    with (
        patch.object(notifier, "_session_factory", factory),
        pytest.raises(asyncio.CancelledError),
    ):
        await notifier._increment_dead_letter(ep)


async def test_increment_dead_letter_survives_db_error(notifier: Notifier) -> None:
    """A DB error during the dead-letter UPDATE is logged, never re-raised."""
    ep = _fake_endpoint()
    _, factory = _make_db_session(execute=AsyncMock(side_effect=RuntimeError("db down")))

    with (
        patch.object(notifier, "_session_factory", factory),
        patch("modulo.core.notifier._log.exception") as mock_log,
    ):
        await notifier._increment_dead_letter(ep)

    mock_log.assert_called_once()
    assert mock_log.call_args.args[0] == "notifier.increment_dead_letter_failed"


# ---------------------------------------------------------------------------
# _reset_dead_letter
# ---------------------------------------------------------------------------


async def test_reset_dead_letter_resets_counter(notifier: Notifier) -> None:
    ep = _fake_endpoint()
    executed: list[Any] = []

    async def _capture(stmt: Any) -> MagicMock:
        executed.append(stmt)
        return MagicMock()

    _, factory = _make_db_session(execute=AsyncMock(side_effect=_capture))

    with patch.object(notifier, "_session_factory", factory):
        await notifier._reset_dead_letter(ep)

    assert len(executed) == 1
    stmt = str(executed[0].compile(compile_kwargs={"literal_binds": True}))
    assert "consecutive_dead_letter_count=0" in stmt


async def test_reset_dead_letter_only_touches_positive_counters(notifier: Notifier) -> None:
    """The UPDATE must be guarded by count > 0 so zero counters are not rewritten."""
    ep = _fake_endpoint()
    executed: list[Any] = []

    async def _capture(stmt: Any) -> MagicMock:
        executed.append(stmt)
        return MagicMock()

    _, factory = _make_db_session(execute=AsyncMock(side_effect=_capture))

    with patch.object(notifier, "_session_factory", factory):
        await notifier._reset_dead_letter(ep)

    assert len(executed) == 1
    stmt = str(executed[0].compile(compile_kwargs={"literal_binds": True}))
    assert "consecutive_dead_letter_count > 0" in stmt


async def test_reset_dead_letter_survives_db_error(notifier: Notifier) -> None:
    ep = _fake_endpoint()
    _, factory = _make_db_session(execute=AsyncMock(side_effect=RuntimeError("db down")))

    with (
        patch.object(notifier, "_session_factory", factory),
        patch("modulo.core.notifier._log.exception") as mock_log,
    ):
        await notifier._reset_dead_letter(ep)

    mock_log.assert_called_once()


async def test_reset_dead_letter_re_raises_cancelled_error(notifier: Notifier) -> None:
    """A cancellation during the reset UPDATE must propagate."""
    ep = _fake_endpoint()
    _, factory = _make_db_session(execute=AsyncMock(side_effect=asyncio.CancelledError))

    with (
        patch.object(notifier, "_session_factory", factory),
        pytest.raises(asyncio.CancelledError),
    ):
        await notifier._reset_dead_letter(ep)


# ---------------------------------------------------------------------------
# _record_delivery
# ---------------------------------------------------------------------------


async def test_record_delivery_adds_log_entry(notifier: Notifier) -> None:
    ep = _fake_endpoint()
    session, factory = _make_db_session(add=MagicMock())

    with patch.object(notifier, "_session_factory", factory):
        await notifier._record_delivery(ep, "hitl_awaiting", _RUN, "delivered", 1, 200, None, None)

    session.add.assert_called_once()
    entry = session.add.call_args.args[0]
    assert isinstance(entry, NotificationDeliveryLog)
    assert entry.organisation_id == ep.organisation_id
    assert entry.endpoint_id == ep.id
    assert entry.event_type == "hitl_awaiting"
    assert entry.run_id == _RUN
    assert entry.status == "delivered"
    assert entry.attempt_count == 1
    assert entry.response_code == 200
    assert entry.last_error is None
    assert entry.payload_ciphertext is None


async def test_record_delivery_retains_payload_ciphertext(notifier: Notifier) -> None:
    ep = _fake_endpoint()
    ciphertext = b"encrypted-payload-bytes"
    session, factory = _make_db_session(add=MagicMock())

    with patch.object(notifier, "_session_factory", factory):
        await notifier._record_delivery(ep, "run_failed", _RUN, "dead_lettered", 4, 500, "HTTP 500", ciphertext)

    entry = session.add.call_args.args[0]
    assert entry.payload_ciphertext == ciphertext
    assert entry.status == "dead_lettered"
    assert entry.last_error == "HTTP 500"


async def test_record_delivery_survives_db_error(notifier: Notifier) -> None:
    ep = _fake_endpoint()
    _, factory = _make_db_session(add=MagicMock(side_effect=RuntimeError("db down")))

    with (
        patch.object(notifier, "_session_factory", factory),
        patch("modulo.core.notifier._log.exception") as mock_log,
    ):
        await notifier._record_delivery(ep, "hitl_awaiting", _RUN, "delivered", 1, 200, None, None)

    mock_log.assert_called_once()


async def test_record_delivery_re_raises_cancelled_error(notifier: Notifier) -> None:
    """A cancellation while writing the delivery log must propagate."""
    ep = _fake_endpoint()
    _, factory = _make_db_session(add=MagicMock(side_effect=asyncio.CancelledError))

    with (
        patch.object(notifier, "_session_factory", factory),
        pytest.raises(asyncio.CancelledError),
    ):
        await notifier._record_delivery(ep, "hitl_awaiting", _RUN, "delivered", 1, 200, None, None)


# ---------------------------------------------------------------------------
# dispatch_event (integration of the above)
# ---------------------------------------------------------------------------


async def test_dispatch_event_no_subscribers_returns_empty(notifier: Notifier) -> None:
    with patch.object(notifier, "_get_subscribed_endpoints", AsyncMock(return_value=[])):
        result = await notifier.dispatch_event(_ORG, "hitl_overdue", {"run_id": str(_RUN)})
    assert result == []


async def test_dispatch_event_creates_in_app_notification_with_zero_webhook_endpoints(
    notifier: Notifier,
) -> None:
    """hitl-gate-removal-guard-plan.md v19 §5 Notifier silent-loss fix: the
    early `if not endpoints: return []` is gone, so in-app Notification creation
    runs even when an org has zero webhook subscribers."""
    mapper_instance = MagicMock()
    mapper_instance.create_from_event = AsyncMock(return_value=None)

    with (
        patch.object(notifier, "_get_subscribed_endpoints", AsyncMock(return_value=[])),
        patch(
            "modulo.core.notifier.event_mapper.NotificationEventMapper",
            return_value=mapper_instance,
        ),
        patch.object(notifier, "_session_factory") as factory,
    ):
        session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        factory.return_value.__aenter__.return_value = session

        results = await notifier.dispatch_event(_ORG, "hitl_overdue", {"run_id": str(_RUN)})

    assert results == []  # no webhook dispatches...
    mapper_instance.create_from_event.assert_awaited_once()  # ...but the in-app notification still runs
    assert mapper_instance.create_from_event.call_args.kwargs["event_type"] == "hitl_overdue"


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


async def test_dispatch_event_in_app_failure_does_not_abort_webhooks(notifier: Notifier) -> None:
    """A failure creating the in-app notification must not drop webhook results."""
    ep = _fake_endpoint()

    _, factory = _make_db_session()

    with (
        patch.object(notifier, "_session_factory", factory),
        patch.object(notifier, "_get_client", AsyncMock(return_value=AsyncMock())),
        patch.object(notifier, "_get_subscribed_endpoints", AsyncMock(return_value=[ep])),
        patch.object(notifier, "_dispatch_to_endpoint") as mock_dispatch,
        patch.object(NotificationEventMapper, "create_from_event", AsyncMock(side_effect=RuntimeError("db down"))),
        patch("modulo.core.notifier._log.exception") as mock_log,
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
    mock_log.assert_called_once()


async def test_dispatch_event_re_raises_cancelled_error_from_in_app_block(notifier: Notifier) -> None:
    """A cancellation inside the in-app notification block must propagate, not be
    swallowed by the ``except Exception`` guard."""
    mapper_instance = MagicMock()
    mapper_instance.create_from_event = AsyncMock(return_value=None)

    with (
        patch.object(notifier, "_get_subscribed_endpoints", AsyncMock(return_value=[])),
        patch.object(notifier, "_session_factory") as factory,
        patch(
            "modulo.core.notifier.event_mapper.NotificationEventMapper",
            return_value=mapper_instance,
        ),
        patch("modulo.core.notifier.set_rls_org", AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        factory.return_value.__aenter__.return_value = session

        await notifier.dispatch_event(_ORG, "hitl_overdue", {"run_id": str(_RUN)})

    mapper_instance.create_from_event.assert_not_called()


async def test_dispatch_pins_client_to_validated_endpoint(notifier: Notifier) -> None:
    """FAR-517: the webhook dispatch client is built through pinned_async_client,
    keyed to the saved endpoint URL, so the connection is pinned to the address
    validated at save time rather than re-resolved at dispatch."""
    ep = _fake_endpoint()

    with (
        patch.object(notifier, "_get_subscribed_endpoints", AsyncMock(return_value=[ep])) as mock_get,
        patch.object(notifier, "_dispatch_to_endpoint") as mock_dispatch,
        patch("modulo.core.notifier.pinned_async_client") as mock_pinned,
    ):
        mock_dispatch.return_value = DispatchResult(
            endpoint_id=ep.id,
            status="delivered",
            attempt_count=1,
            response_code=200,
        )
        mock_pinned.return_value = AsyncMock()

        results = await notifier.dispatch_event(_ORG, "hitl_awaiting", {"run_id": str(_RUN)})

    assert len(results) == 1
    assert results[0].status == "delivered"
    # The dispatch client was built with the saved endpoint URL (not re-resolved).
    mock_pinned.assert_awaited_once_with(ep.url)
    mock_get.assert_called_once_with(_ORG, "hitl_awaiting", team_id=None)


async def test_dispatch_rejects_ssrf_rebind_to_metadata(notifier: Notifier) -> None:
    """FAR-517 regression: a saved webhook URL whose host re-resolves to cloud
    metadata (169.254.169.254) at dispatch must NOT be connected. The dispatch
    client construction re-resolves + re-validates via pinned_async_client; a
    rebind to a blocked address fails closed and dead-letters the endpoint rather
    than connecting to the unvalidated metadata address."""
    ep = _fake_endpoint()

    async def _fake_pinned(_url: str) -> httpx.AsyncClient:
        raise ValueError(
            "URL hostname hooks.example.com resolves to a private/internal address "
            "(169.254.169.254). Add its CIDR to SSRF_ALLOW_PRIVATE_RANGES to allow "
            "this target, or use a public URL."
        )

    with (
        patch.object(notifier, "_get_subscribed_endpoints", AsyncMock(return_value=[ep])),
        patch.object(notifier, "_dispatch_to_endpoint") as mock_dispatch,
        patch.object(notifier, "_record_delivery", AsyncMock()) as mock_record,
        patch.object(notifier, "_increment_dead_letter", AsyncMock()) as mock_dead,
        patch("modulo.core.notifier.pinned_async_client", _fake_pinned),
    ):
        results = await notifier.dispatch_event(_ORG, "hitl_awaiting", {"run_id": str(_RUN)})

    assert len(results) == 1
    assert results[0].status == "dead_lettered"
    assert "169.254.169.254" in (results[0].last_error or "")
    assert results[0].attempt_count == 0
    # The HTTP request must never be issued for the rebind-to-metadata host.
    mock_dispatch.assert_not_called()
    mock_record.assert_awaited_once()
    mock_dead.assert_awaited_once()
    delivery = mock_record.await_args.args
    assert delivery[3] == "dead_lettered"  # status
    assert "169.254.169.254" in delivery[6]


async def test_dispatch_event_retains_payload_end_to_end(notifier: Notifier) -> None:
    """retain_payload must flow from dispatch_event through _dispatch_inline to the
    delivery log, encrypting the actual body that was POSTed (incl. timestamp)."""
    ep = _fake_endpoint()
    record_kwargs: dict[str, Any] = {}
    mapper_instance = MagicMock()
    mapper_instance.create_from_event = AsyncMock(return_value=None)

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

    _, factory = _make_db_session(add=MagicMock())

    with (
        patch.object(notifier, "_session_factory", factory),
        patch.object(notifier, "_get_subscribed_endpoints", AsyncMock(return_value=[ep])),
        patch.object(notifier, "_record_delivery", _record),
        patch.object(notifier, "_increment_dead_letter", AsyncMock()),
        patch.object(notifier, "_reset_dead_letter", AsyncMock()),
        patch(
            "modulo.core.notifier.event_mapper.NotificationEventMapper",
            return_value=mapper_instance,
        ),
    ):
        async with respx.mock:
            respx.post(ep.url).mock(Response(200))
            results = await notifier.dispatch_event(
                _ORG,
                "hitl_awaiting",
                {"run_id": str(_RUN)},
                retain_payload=True,
            )

    assert len(results) == 1
    assert results[0].status == "delivered"
    ciphertext = record_kwargs.get("payload_ciphertext")
    assert ciphertext is not None
    decrypted = json.loads(Fernet(_KEY.encode()).decrypt(ciphertext))
    assert decrypted["event"] == "hitl_awaiting"
    assert decrypted["payload"] == {"run_id": str(_RUN)}
    assert "timestamp" in decrypted
    mapper_instance.create_from_event.assert_awaited_once()


# ---------------------------------------------------------------------------
# Team-scoped dispatch
# ---------------------------------------------------------------------------


_TEAM = uuid.uuid4()


@pytest.mark.parametrize(
    ("team_id", "expected_team_id"),
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
    ("team_id", "team_endpoints", "org_endpoints", "expected_count", "expected_team"),
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


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_init_raises_on_invalid_fernet_key() -> None:
    with pytest.raises(ValueError, match="Invalid Fernet key"):
        Notifier(MagicMock(), "not-a-valid-fernet-key-1234")


async def test_get_client_creates_and_reuses(notifier: Notifier) -> None:
    first_client = MagicMock()
    first_client.is_closed = False
    with patch("modulo.core.notifier.httpx.AsyncClient", return_value=first_client) as client_cls:
        first = await notifier._get_client()
        second = await notifier._get_client()
    assert first is second is first_client
    client_cls.assert_called_once()


async def test_get_client_recreates_closed_client(notifier: Notifier) -> None:
    closed = MagicMock()
    closed.is_closed = True
    notifier._http_client = closed
    fresh = MagicMock()
    fresh.is_closed = False

    with patch("modulo.core.notifier.httpx.AsyncClient", return_value=fresh) as client_cls:
        got = await notifier._get_client()

    assert got is fresh
    client_cls.assert_called_once()


async def test_close_noop_when_client_never_created() -> None:
    notifier_instance = Notifier(MagicMock(), _KEY)
    await notifier_instance.close()
    # No client was ever created, so close must leave the client slot untouched.
    assert notifier_instance._http_client is None


async def test_close_acloses_http_client() -> None:
    notifier_instance = Notifier(MagicMock(), _KEY)
    client = AsyncMock()
    client.is_closed = False
    notifier_instance._http_client = client
    await notifier_instance.close()
    client.aclose.assert_awaited_once()
    assert notifier_instance._http_client is None
