"""Unit tests for break-glass use-time revalidation at outbound webhook dispatch.

The mint-marker (``deny_break_glass_mint``, plan v17 API-key + long-lived deny)
covers create/update/delete routes but not the dispatch site. This chunk closes
that gap: when the notifier dispatches an outbound webhook it re-checks the
endpoint's owning account against the shared break-glass builder and skips
endpoints owned by a break-glass account (live OR denied) — fail-closed, with
per-endpoint isolation.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.exc import SQLAlchemyError

from modulo.core.notifier import DispatchResult, Notifier, _record_owner_read_failure
from modulo.db.models.account import Account
from modulo.db.models.notification_endpoint import NotificationEndpoint

_ORG = uuid.uuid4()
_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def notifier() -> Notifier:
    return Notifier(MagicMock(), Fernet.generate_key().decode())


def _owner(**kwargs: Any) -> Account:
    acc = MagicMock(spec=Account)
    acc.id = uuid.uuid4()
    acc.is_break_glass = kwargs.get("is_break_glass", False)
    acc.break_glass_expires_at = kwargs.get("expires_at")
    acc.break_glass_deactivated_at = kwargs.get("deactivated_at")
    acc.active = kwargs.get("active", True)
    return acc


def _endpoint(account_id: uuid.UUID | None = None) -> NotificationEndpoint:
    ep = MagicMock(spec=NotificationEndpoint)
    ep.id = uuid.uuid4()
    ep.organisation_id = _ORG
    ep.url = "https://hooks.example.com/notify"
    ep.account_id = account_id
    return ep


def _session_factory(owners: list[Account], *, raise_on_execute: bool = False) -> MagicMock:
    """Session factory returning one session shared by owner-load + in-app blocks."""
    result = MagicMock()
    result.scalars.return_value.__iter__ = lambda self: iter(owners)
    session = AsyncMock()
    if raise_on_execute:
        session.execute = AsyncMock(side_effect=SQLAlchemyError("db down"))
    else:
        session.execute = AsyncMock(return_value=result)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return MagicMock(
        side_effect=lambda: AsyncMock(
            __aenter__=AsyncMock(return_value=session),
            __aexit__=AsyncMock(return_value=False),
        )
    )


async def _reject(n: Notifier, endpoints: list[NotificationEndpoint], owners: list[Account]) -> list[Any]:
    with patch.object(n, "_session_factory", _session_factory(owners)):
        return await n._reject_break_glass_owned(endpoints)


# ---------------------------------------------------------------------------
# _record_owner_read_failure
# ---------------------------------------------------------------------------


def test_record_owner_read_failure_noop_when_no_meter_provider() -> None:
    """When OTel reports no meter provider the counter stays unset and nothing is recorded."""
    from opentelemetry import metrics as otel_metrics

    with (
        patch.object(otel_metrics, "get_meter_provider", return_value=None),
        patch("modulo.core.notifier._owner_read_failures_total", None),
    ):
        _record_owner_read_failure()

    from modulo.core.notifier import _owner_read_failures_total

    assert _owner_read_failures_total is None


def test_record_owner_read_failure_initialises_and_increments_counter() -> None:
    """First call builds the OTel counter, subsequent calls reuse it and increment."""
    from opentelemetry import metrics as otel_metrics

    counter = MagicMock()
    meter = MagicMock()
    meter.create_counter.return_value = counter
    provider = MagicMock()
    provider.get_meter.return_value = meter

    with (
        patch.object(otel_metrics, "get_meter_provider", return_value=provider),
        patch("modulo.core.notifier._owner_read_failures_total", None),
    ):
        _record_owner_read_failure()
        _record_owner_read_failure()

    assert counter.add.call_count == 2
    meter.create_counter.assert_called_once_with(
        name="modulo_notifier_break_glass_owner_read_failures_total",
        description="Fail-closed webhook dispatch suppressions from break-glass owner-read DB errors",
        unit="1",
    )


def test_record_owner_read_failure_logs_warning_when_counter_creation_fails() -> None:
    """If OTel counter creation raises, the metric setup is abandoned with a warning."""
    from opentelemetry import metrics as otel_metrics

    provider = MagicMock()
    provider.get_meter = MagicMock(side_effect=RuntimeError("otel down"))

    with (
        patch.object(otel_metrics, "get_meter_provider", return_value=provider),
        patch("modulo.core.notifier._owner_read_failures_total", None),
        patch("modulo.core.notifier._log.warning") as mock_warning,
    ):
        _record_owner_read_failure()

    mock_warning.assert_called_once_with("notifier.metrics_owner_read_counter_failed")


# ---------------------------------------------------------------------------
# _reject_break_glass_owned
# ---------------------------------------------------------------------------


async def test_reject_skips_denied_break_glass_owned_endpoint(notifier: Notifier) -> None:
    ep = _endpoint(account_id=uuid.uuid4())
    owner = _owner(is_break_glass=True, expires_at=_NOW - timedelta(hours=1))
    owner.id = ep.account_id
    kept = await _reject(notifier, [ep], [owner])
    assert kept == []


async def test_reject_skips_live_break_glass_owned_endpoint(notifier: Notifier) -> None:
    """A LIVE (unexpired, active) break-glass owner is also skipped — the deny is
    the shared denied-OR-live union, not just the denied predicate."""
    ep = _endpoint(account_id=uuid.uuid4())
    owner = _owner(is_break_glass=True, expires_at=_NOW + timedelta(hours=1), active=True)
    owner.id = ep.account_id
    kept = await _reject(notifier, [ep], [owner])
    assert kept == []


async def test_reject_keeps_normal_owned_endpoint(notifier: Notifier) -> None:
    ep = _endpoint(account_id=uuid.uuid4())
    owner = _owner(is_break_glass=False)
    owner.id = ep.account_id
    kept = await _reject(notifier, [ep], [owner])
    assert kept == [ep]


async def test_reject_keeps_endpoint_without_owner(notifier: Notifier) -> None:
    """An endpoint with account_id IS NULL has no owner to deny — it is kept."""
    ep = _endpoint(account_id=None)
    kept = await _reject(notifier, [ep], [])
    assert kept == [ep]


async def test_reject_keeps_non_uuid_owner_reference(notifier: Notifier) -> None:
    """A non-UUID owner reference cannot point at an accounts row, so it cannot
    be a break-glass owner — kept, and no query is issued."""
    ep = _endpoint()
    ep.account_id = "not-a-uuid"
    with patch.object(notifier, "_session_factory") as factory:
        kept = await notifier._reject_break_glass_owned([ep])
    assert kept == [ep]
    factory.assert_not_called()


async def test_reject_keeps_non_uuid_reference_alongside_uuid_owners(notifier: Notifier) -> None:
    """When other endpoints force an owner query, an endpoint carrying a non-UUID
    account_id is still kept — it cannot reference an accounts row."""
    uuid_ep = _endpoint(account_id=uuid.uuid4())
    owner = _owner(is_break_glass=False)
    owner.id = uuid_ep.account_id
    non_uuid_ep = _endpoint()
    non_uuid_ep.account_id = "not-a-uuid"

    kept = await _reject(notifier, [uuid_ep, non_uuid_ep], [owner])

    assert kept == [uuid_ep, non_uuid_ep]


async def test_reject_re_raises_cancelled_error(notifier: Notifier) -> None:
    """A cancelled owner read must propagate as CancelledError, not be swallowed."""
    ep = _endpoint(account_id=uuid.uuid4())
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=asyncio.CancelledError)
    factory = MagicMock(
        side_effect=lambda: AsyncMock(
            __aenter__=AsyncMock(return_value=session),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    with (
        patch.object(notifier, "_session_factory", factory),
        pytest.raises(asyncio.CancelledError),
    ):
        await notifier._reject_break_glass_owned([ep])


async def test_reject_skips_orphaned_owner_id(notifier: Notifier) -> None:
    """An endpoint whose account_id is set but whose account row is missing cannot
    be resolved — fail-closed, skipped."""
    ep = _endpoint(account_id=uuid.uuid4())
    kept = await _reject(notifier, [ep], [])
    assert kept == []


async def test_reject_fail_closed_on_db_error(notifier: Notifier) -> None:
    """A DB read error must not fail-open a break-glass endpoint — all endpoints
    are treated as denied, and the owner-read failure counter is emitted."""
    ep = _endpoint(account_id=uuid.uuid4())
    with (
        patch.object(notifier, "_session_factory", _session_factory([], raise_on_execute=True)),
        patch("modulo.core.notifier._record_owner_read_failure") as mock_metric,
    ):
        kept = await notifier._reject_break_glass_owned([ep])
    assert kept == []
    mock_metric.assert_called_once()


async def test_reject_uses_shared_builder_rule(notifier: Notifier) -> None:
    """The deny decision is the shared builder's denied-OR-live union, not a
    duplicated rule — patching the shared functions must flip the outcome."""
    ep = _endpoint(account_id=uuid.uuid4())
    owner = _owner(is_break_glass=True, expires_at=_NOW + timedelta(hours=1))
    owner.id = ep.account_id
    with (
        patch.object(notifier, "_session_factory", _session_factory([owner])),
        patch("modulo.core.notifier.is_break_glass_denied", return_value=False) as mock_denied,
        patch("modulo.core.notifier.is_break_glass_live", return_value=False) as mock_live,
    ):
        kept = await notifier._reject_break_glass_owned([ep])
    assert kept == [ep]
    mock_denied.assert_called_once()
    mock_live.assert_called_once()
    kwargs = mock_denied.call_args.kwargs
    assert kwargs["is_break_glass"] is True
    assert kwargs["break_glass_expires_at"] == owner.break_glass_expires_at
    assert kwargs["now"] is not None


async def test_reject_empty_endpoints_no_query(notifier: Notifier) -> None:
    with patch.object(notifier, "_session_factory") as factory:
        kept = await notifier._reject_break_glass_owned([])
    assert kept == []
    factory.assert_not_called()


# ---------------------------------------------------------------------------
# dispatch_event integration — the filter runs in the real dispatch path
# ---------------------------------------------------------------------------


async def _dispatch(
    n: Notifier,
    endpoints: list[NotificationEndpoint],
    owners: list[Account],
    *,
    raise_on_execute: bool = False,
) -> tuple[list[DispatchResult], AsyncMock]:
    mapper_instance = MagicMock()
    mapper_instance.create_from_event = AsyncMock(return_value=None)
    with (
        patch.object(n, "_get_subscribed_endpoints", AsyncMock(return_value=endpoints)),
        patch.object(n, "_get_client", AsyncMock(return_value=AsyncMock())),
        patch.object(n, "_dispatch_to_endpoint") as mock_dispatch,
        patch.object(n, "_session_factory", _session_factory(owners, raise_on_execute=raise_on_execute)),
        patch("modulo.core.notifier.event_mapper.NotificationEventMapper", return_value=mapper_instance),
    ):
        mock_dispatch.return_value = DispatchResult(
            endpoint_id=uuid.uuid4(),
            status="delivered",
            attempt_count=1,
            response_code=200,
        )
        results = await n.dispatch_event(_ORG, "hitl_awaiting", {"run_id": str(uuid.uuid4())})
    return results, mock_dispatch


async def test_dispatch_skips_break_glass_owned_endpoint(notifier: Notifier) -> None:
    ep = _endpoint(account_id=uuid.uuid4())
    owner = _owner(is_break_glass=True, expires_at=_NOW - timedelta(hours=1))
    owner.id = ep.account_id
    results, mock_dispatch = await _dispatch(notifier, [ep], [owner])
    assert results == []
    mock_dispatch.assert_not_called()


async def test_dispatch_delivers_normal_owned_endpoint(notifier: Notifier) -> None:
    ep = _endpoint(account_id=uuid.uuid4())
    owner = _owner(is_break_glass=False)
    owner.id = ep.account_id
    results, mock_dispatch = await _dispatch(notifier, [ep], [owner])
    assert len(results) == 1
    assert results[0].status == "delivered"
    mock_dispatch.assert_called_once()


async def test_dispatch_mixed_endpoints_only_normal_delivers(notifier: Notifier) -> None:
    bg_ep = _endpoint(account_id=uuid.uuid4())
    bg_owner = _owner(is_break_glass=True, expires_at=_NOW + timedelta(hours=1))
    bg_owner.id = bg_ep.account_id
    normal_ep = _endpoint(account_id=uuid.uuid4())
    normal_owner = _owner(is_break_glass=False)
    normal_owner.id = normal_ep.account_id

    results, mock_dispatch = await _dispatch(notifier, [bg_ep, normal_ep], [bg_owner, normal_owner])
    assert len(results) == 1
    assert results[0].status == "delivered"
    assert mock_dispatch.call_count == 1
    dispatched_ep = mock_dispatch.call_args.args[1]
    assert dispatched_ep is normal_ep


async def test_dispatch_fail_closed_on_db_error(notifier: Notifier) -> None:
    """A DB blip during owner resolution suppresses webhook dispatch entirely —
    fail-closed, never fail-open."""
    ep = _endpoint(account_id=uuid.uuid4())
    results, mock_dispatch = await _dispatch(notifier, [ep], [], raise_on_execute=True)
    assert results == []
    mock_dispatch.assert_not_called()
