"""Unit tests for the FAR-1270 approaching-deadline HITL warning sweep.

Covers the four design decisions stated in ``deadline_warning``'s docstring:
the lead-time computation (including the 60s minimum review window), the
dual-arm deadline (stamped ``terminalize_at`` vs legacy ``expires_at +
grace``), the once-only fire-once guard, and every skip condition (claimed /
decided / not-awaiting / past deadline / not-yet-in-band / claimed sibling /
no opted-in recipients / lock denied).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.hitl_manager import deadline_warning as dw
from modulo.core.hitl_manager.deadline_warning import (
    _SWEEP_CADENCE_SECONDS,
    dispatch_deadline_notifications,
    effective_deadline,
    lead_time_seconds,
    review_window_seconds,
)
from modulo.db.models.hitl_claim import HitlClaim

_ORG = uuid.uuid4()
_RUN = uuid.uuid4()
_PIPE = uuid.uuid4()
_GRACE = 3600  # shipped default: hitl_review_cancel_grace_seconds
_RECIPIENTS = ["reviewer@example.com"]


# ---------------------------------------------------------------------------
# Pure helpers: lead time, deadline arms, band membership
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("window_seconds", "expected"),
    [
        pytest.param(60, 60, id="minimum_window_covers_the_whole_window"),
        pytest.param(90, 60, id="sub_2x_window_floored_at_cadence"),
        pytest.param(120, 60, id="two_minute_window_half_equals_cadence"),
        pytest.param(4500, 2250, id="default_75_minute_window_is_half"),
        pytest.param(28800, 3600, id="eight_hour_window_capped_at_one_hour"),
        pytest.param(604800, 3600, id="seven_day_window_capped_at_one_hour"),
    ],
)
def test_lead_time_table(window_seconds: float, expected: float) -> None:
    assert lead_time_seconds(window_seconds) == expected


@pytest.mark.parametrize("window_seconds", [60, 90, 120, 4500, 28800, 604800])
def test_lead_time_invariants(window_seconds: float) -> None:
    """Never beyond the window, never below the sweep cadence (unless the
    window itself is narrower — the band then equals the whole window)."""
    lead = lead_time_seconds(window_seconds)
    assert lead <= window_seconds
    assert lead >= min(_SWEEP_CADENCE_SECONDS, window_seconds)


@pytest.mark.parametrize("window_seconds", [0, -5])
def test_lead_time_rejects_non_positive_window(window_seconds: float) -> None:
    with pytest.raises(ValueError, match="window_seconds must be positive"):
        lead_time_seconds(window_seconds)


def _claim(
    *,
    expires_at: datetime,
    created_at: datetime | None = None,
    claim_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
    gate_label: str | None = None,
    account_id: uuid.UUID | None = None,
    decision: str | None = None,
) -> HitlClaim:
    claim = HitlClaim(
        organisation_id=_ORG,
        run_id=run_id or _RUN,
        pipeline_id=pipeline_id or _PIPE,
        review_id="gate-a",
        account_id=account_id,
        decision=decision,
        expires_at=expires_at,
    )
    claim.id = claim_id or uuid.uuid4()
    # Transient rows never ran the server default; stamp the column the DB
    # would have stamped so window arithmetic is exercised like production.
    claim.created_at = created_at if created_at is not None else datetime.now(UTC)
    if gate_label is not None:
        claim.gate_config_json = {"label": gate_label}
    return claim


def test_effective_deadline_legacy_fallback_is_expires_at_plus_grace() -> None:
    """Unstamped rows (this tree, and post-FAR-1257 rows with NULL stamp) use
    the exact arithmetic the current terminaliser collects on."""
    expires_at = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    claim = _claim(expires_at=expires_at)
    assert effective_deadline(claim, _GRACE) == expires_at + timedelta(seconds=_GRACE)


def test_effective_deadline_prefers_stamped_terminalize_at() -> None:
    """A stamped ``terminalize_at`` IS the deadline — grace is ignored, which
    is what keeps the sweep aligned with the terminaliser once FAR-1257
    lands (the column may not be mapped in this tree, hence the setattr)."""
    claim = _claim(expires_at=datetime(2026, 9, 28, 12, 0, tzinfo=UTC))
    stamped = datetime(2026, 9, 28, 13, 45, tzinfo=UTC)
    claim.terminalize_at = stamped  # type: ignore[attr-defined]
    assert effective_deadline(claim, _GRACE) == stamped


def test_effective_deadline_explicit_none_stamp_falls_back_to_legacy() -> None:
    claim = _claim(expires_at=datetime(2026, 9, 28, 12, 0, tzinfo=UTC))
    claim.terminalize_at = None  # type: ignore[attr-defined]
    assert effective_deadline(claim, _GRACE) == datetime(2026, 9, 28, 13, 0, tzinfo=UTC)


def test_effective_deadline_unusable_expires_at_returns_none() -> None:
    claim = _claim(expires_at=datetime(2026, 9, 28, 12, 0, tzinfo=UTC))
    claim.expires_at = None  # type: ignore[assignment]
    assert effective_deadline(claim, _GRACE) is None


def test_review_window_seconds_derived_from_row_stamps() -> None:
    created = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    deadline = datetime(2026, 9, 28, 13, 15, tzinfo=UTC)
    claim = _claim(expires_at=created, created_at=created)
    assert review_window_seconds(claim, deadline) == 4500.0
    assert review_window_seconds(claim, None) is None

    unstamped = _claim(expires_at=created, created_at=created)
    unstamped.created_at = None  # type: ignore[assignment]
    assert review_window_seconds(unstamped, deadline) is None


def test_within_lead_matrix() -> None:
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    window = 4500.0
    lead = 2250.0

    # Inside the band: deadline is 10 minutes away, lead is 37.5 minutes.
    assert dw._within_lead(now + timedelta(minutes=10), window, now) is True
    # Exactly at the band edge (inclusive).
    assert dw._within_lead(now + timedelta(seconds=lead), window, now) is True
    # Outside the band: deadline still 2 hours away.
    assert dw._within_lead(now + timedelta(hours=2), window, now) is False
    # Exactly at the deadline: the terminaliser owns it (half-open band).
    assert dw._within_lead(now, window, now) is False
    # Already past the deadline: skip.
    assert dw._within_lead(now - timedelta(seconds=1), window, now) is False
    # Missing / unusable inputs: skip, never crash.
    assert dw._within_lead(None, window, now) is False
    assert dw._within_lead(now + timedelta(minutes=10), None, now) is False
    assert dw._within_lead(now + timedelta(minutes=10), 0, now) is False
    assert dw._within_lead(now + timedelta(minutes=10), -10, now) is False


def test_sixty_second_window_band_covers_the_whole_gate_life() -> None:
    """The point of the cadence floor: for the 60s minimum window the band is
    the entire window, so an every-minute tick always lands inside it."""
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    deadline = now + timedelta(seconds=60)
    window = 60.0
    assert lead_time_seconds(window) == 60
    assert dw._within_lead(deadline, window, now) is True
    assert dw._within_lead(deadline, window, deadline - timedelta(seconds=1)) is True


# ---------------------------------------------------------------------------
# Fire-once claim (idempotency)
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal async Redis double honouring SET NX EX semantics."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.keys: dict[str, str] = {}
        self.set_calls = 0

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> Any:
        self.set_calls += 1
        if self.fail:
            raise RuntimeError("redis down")
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


async def test_fire_once_claims_first_send_then_blocks() -> None:
    redis = _FakeRedis()
    claim_id = uuid.uuid4()
    assert await dw._claim_deadline_warning(redis, claim_id) is True
    assert await dw._claim_deadline_warning(redis, claim_id) is False
    assert f"{dw._FIRE_ONCE_KEY_PREFIX}:{claim_id}" in redis.keys


async def test_fire_once_blocked_when_key_already_set() -> None:
    redis = _FakeRedis()
    claim_id = uuid.uuid4()
    redis.keys[f"{dw._FIRE_ONCE_KEY_PREFIX}:{claim_id}"] = "1"  # marker from a prior tick
    assert await dw._claim_deadline_warning(redis, claim_id) is False


async def test_fire_once_redis_failure_falls_back_to_memory_not_bare_fail_open() -> None:
    """A failing Redis must NOT re-approve every tick (this sweep runs every
    60s — bare fail-open would email the whole band repeatedly)."""
    redis = _FakeRedis(fail=True)
    claim_id = uuid.uuid4()
    assert await dw._claim_deadline_warning(redis, claim_id) is True
    assert await dw._claim_deadline_warning(redis, claim_id) is False
    # A different claim still claims: the backstop is per-gate, not global.
    assert await dw._claim_deadline_warning(redis, uuid.uuid4()) is True


async def test_fire_once_without_redis_uses_memory_backstop() -> None:
    claim_id = uuid.uuid4()
    assert await dw._claim_deadline_warning(None, claim_id) is True
    assert await dw._claim_deadline_warning(None, claim_id) is False


# ---------------------------------------------------------------------------
# Dispatch flow (mocked sessions)
# ---------------------------------------------------------------------------


def _mock_begin(session: AsyncMock) -> None:
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)


def _cm(session: AsyncMock) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _org_list_session(orgs: list[uuid.UUID]) -> AsyncMock:
    session = AsyncMock(name="org_session")
    _mock_begin(session)
    result = MagicMock()
    result.scalars.return_value = orgs
    session.execute = AsyncMock(return_value=result)
    return session


def _lock_result(acquired: bool) -> MagicMock:
    result = MagicMock(name="lock_result")
    result.scalar_one.return_value = acquired
    return result


def _rows_result(rows: list[Any]) -> MagicMock:
    result = MagicMock(name="rows_result")
    result.all.return_value = rows
    return result


def _tx_session(
    results: list[Any],
    *,
    lock_failure: Exception | None = None,
) -> AsyncMock:
    """Org-transaction session: execute() replays *results* in order.

    Index 0 is the advisory-lock probe — when *lock_failure* is given it
    raises on that first call (then continues through *results*).
    """
    session = AsyncMock(name="tx_session")
    _mock_begin(session)
    executed: list[Any] = []
    call_count = 0

    async def _execute(stmt: object, *args: object) -> MagicMock:
        nonlocal call_count
        index = call_count
        call_count += 1
        executed.append(stmt)
        if lock_failure is not None and index == 0:
            raise lock_failure
        return results[index]

    session.execute = _execute  # type: ignore[method-assign]
    session.executed = executed  # type: ignore[attr-defined]
    return session


async def _run_dispatch(
    *,
    candidate_rows: list[Any],
    sibling_rows: list[Any] | None = None,
    lock_acquired: bool = True,
    lock_failure: Exception | None = None,
    recipients: list[str] | None = None,
    redis_client: Any = None,
    send_side_effect: Any = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], AsyncMock, AsyncMock]:
    """Run dispatch_deadline_notifications against fully mocked sessions.

    Returns ``(notified, send_mock, resolve_mock)``.
    """
    resolved_recipients = _RECIPIENTS if recipients is None else recipients
    # Always queue a third result for the sibling-guard SELECT — it only runs
    # when the band produced entries (an unconsumed extra result is harmless;
    # a MISSING one would IndexError inside the code under test).
    results = [
        _lock_result(lock_acquired),
        _rows_result(candidate_rows),
        _rows_result(sibling_rows or []),
    ]

    org_session = _org_list_session([_ORG])
    tx_session = _tx_session(results, lock_failure=lock_failure)
    factory = MagicMock(side_effect=[_cm(org_session), _cm(tx_session)])

    send = AsyncMock(name="send_hitl_deadline_alerts", side_effect=send_side_effect)
    resolve = AsyncMock(name="_resolve_recipients", return_value=resolved_recipients)

    with (
        patch.object(dw, "set_rls_org", new=AsyncMock()),
        patch.object(dw, "set_rls_execution_context", new=AsyncMock()),
        patch.object(dw, "_resolve_recipients", resolve),
        patch.object(dw, "send_hitl_deadline_alerts", send),
    ):
        notified = await dispatch_deadline_notifications(
            factory,
            grace_seconds=_GRACE,
            redis_client=redis_client,
            now=now,
        )
    return notified, send, resolve


def _approaching_gate(now: datetime, **kwargs: Any) -> tuple[HitlClaim, str]:
    """A gate whose legacy deadline is exactly 60s from *now*.

    ``expires_at = now + 60 - grace`` makes ``effective_deadline`` land on
    ``now + 60``; ``created_at = now`` makes the window 60s — the minimum
    window, whose lead covers the whole band.
    """
    claim = _claim(expires_at=now + timedelta(seconds=60 - _GRACE), created_at=now, **kwargs)
    return (claim, "My Pipeline")


async def test_dispatch_notifies_one_approaching_gate() -> None:
    now = datetime.now(UTC)
    claim, _ = _approaching_gate(now)
    notified, send, resolve = await _run_dispatch(candidate_rows=[(claim, "My Pipeline")], now=now)

    assert len(notified) == 1
    entry = notified[0]
    assert entry["claim_id"] == claim.id
    assert entry["run_id"] == _RUN
    assert entry["pipeline_name"] == "My Pipeline"
    assert entry["gate_label"] == "gate-a"  # no configured label -> review_id
    assert entry["minutes_remaining"] == 1
    assert entry["deadline"] == now + timedelta(seconds=60)

    resolve.assert_awaited_once()
    assert resolve.await_args.args[1:] == (_ORG, _PIPE)  # (factory, org, pipeline)
    send.assert_awaited_once_with(_RECIPIENTS, _RUN, "gate-a", "My Pipeline", 1)


async def test_dispatch_uses_configured_gate_label() -> None:
    now = datetime.now(UTC)
    claim, _ = _approaching_gate(now, gate_label="Security sign-off")
    notified, send, _ = await _run_dispatch(candidate_rows=[(claim, "My Pipeline")], now=now)

    assert len(notified) == 1
    assert send.await_args.args[2] == "Security sign-off"


async def test_dispatch_sends_once_across_ticks() -> None:
    """Once-only: the second tick's fire-once claim blocks a re-send."""
    now = datetime.now(UTC)
    claim, _ = _approaching_gate(now)
    redis = _FakeRedis()

    first, send_first, _ = await _run_dispatch(candidate_rows=[(claim, "My Pipeline")], redis_client=redis, now=now)
    second, send_second, _ = await _run_dispatch(candidate_rows=[(claim, "My Pipeline")], redis_client=redis, now=now)

    assert len(first) == 1
    assert not second
    assert send_first.await_count == 1
    assert send_second.await_count == 0


async def test_dispatch_skips_past_deadline_gate() -> None:
    """Already-past-deadline gates are the terminaliser's business — never warned."""
    now = datetime.now(UTC)
    claim = _claim(expires_at=now - timedelta(seconds=_GRACE + 60), created_at=now - timedelta(minutes=75))
    notified, send, _ = await _run_dispatch(candidate_rows=[(claim, "My Pipeline")], now=now)

    assert not notified
    send.assert_not_awaited()


async def test_dispatch_skips_gate_not_yet_in_band() -> None:
    """Deadline still hours away (lead cap 1h) — far too early to warn."""
    now = datetime.now(UTC)
    claim = _claim(expires_at=now + timedelta(seconds=6400), created_at=now)
    notified, send, _ = await _run_dispatch(candidate_rows=[(claim, "My Pipeline")], now=now)

    assert not notified
    send.assert_not_awaited()


async def test_dispatch_skips_run_with_claimed_open_sibling_gate() -> None:
    """The terminaliser will NOT cancel a run with a claimed open gate — a
    warning would claim it will be cancelled when it will not."""
    now = datetime.now(UTC)
    claim, _ = _approaching_gate(now)
    notified, send, _ = await _run_dispatch(
        candidate_rows=[(claim, "My Pipeline")],
        sibling_rows=[(_RUN,)],
        now=now,
    )

    assert not notified
    send.assert_not_awaited()


async def test_dispatch_with_no_recipients_does_not_burn_the_marker() -> None:
    """Nobody opted in → no email AND no fire-once claim, so a later opt-in
    can still warn while the band is open."""
    now = datetime.now(UTC)
    claim, _ = _approaching_gate(now)
    redis = _FakeRedis()

    opted_out, send_out, _ = await _run_dispatch(
        candidate_rows=[(claim, "My Pipeline")], recipients=[], redis_client=redis, now=now
    )
    assert not opted_out
    send_out.assert_not_awaited()
    assert not redis.keys  # marker untouched

    opted_in, send_in, _ = await _run_dispatch(
        candidate_rows=[(claim, "My Pipeline")], recipients=_RECIPIENTS, redis_client=redis, now=now
    )
    assert len(opted_in) == 1
    send_in.assert_awaited_once()


async def test_dispatch_skips_org_when_lock_denied() -> None:
    now = datetime.now(UTC)
    claim, _ = _approaching_gate(now)
    notified, send, _ = await _run_dispatch(candidate_rows=[(claim, "My Pipeline")], lock_acquired=False, now=now)

    assert not notified
    send.assert_not_awaited()


async def test_dispatch_proceeds_when_lock_query_fails() -> None:
    now = datetime.now(UTC)
    claim, _ = _approaching_gate(now)
    notified, send, _ = await _run_dispatch(
        candidate_rows=[(claim, "My Pipeline")],
        lock_failure=RuntimeError("pg_try_advisory_xact_lock unavailable"),
        now=now,
    )

    assert len(notified) == 1
    send.assert_awaited_once()


async def test_dispatch_cancellation_from_send_propagates() -> None:
    now = datetime.now(UTC)
    claim, _ = _approaching_gate(now)

    async def _cancel(*args: object, **kwargs: object) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _run_dispatch(candidate_rows=[(claim, "My Pipeline")], send_side_effect=_cancel, now=now)


async def test_dispatch_rejects_negative_grace() -> None:
    with pytest.raises(ValueError, match="grace_seconds must be non-negative"):
        await dispatch_deadline_notifications(MagicMock(), grace_seconds=-1)


# ---------------------------------------------------------------------------
# The SQL predicate itself (structural skips live in SQL)
# ---------------------------------------------------------------------------


async def test_predicate_carries_every_structural_skip() -> None:
    """Claimed / decided / non-awaiting / cancellation-requested gates are
    filtered by the SELECT — asserted on the WHERE clause via literal binds
    (a substring check over the full render would pass trivially)."""
    now = datetime.now(UTC)
    org_session = _org_list_session([_ORG])
    tx_session = _tx_session([_lock_result(True), _rows_result([])])
    factory = MagicMock(side_effect=[_cm(org_session), _cm(tx_session)])

    with (
        patch.object(dw, "set_rls_org", new=AsyncMock()),
        patch.object(dw, "set_rls_execution_context", new=AsyncMock()),
    ):
        await dispatch_deadline_notifications(factory, grace_seconds=_GRACE, now=now)

    # executed: [lock, candidate-select]
    assert len(tx_session.executed) == 2
    select_stmt = tx_session.executed[1]
    compiled = str(select_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "hitl_claims.decision IS NULL" in compiled
    assert "hitl_claims.account_id IS NULL" in compiled
    assert "awaiting_human" in compiled
    assert "cancellation_requested IS false" in compiled
    # literal_binds renders Uuid as hex without dashes.
    assert str(_ORG).replace("-", "") in compiled


async def test_resolve_recipients_uses_own_session_rls_and_shared_resolver() -> None:
    """Recipients come from the SAME resolver the gate-fire email uses, in
    their own RLS transaction (opt-in contract is shared, never re-implemented)."""
    session = AsyncMock(name="recipient_session")
    _mock_begin(session)
    factory = MagicMock(side_effect=[_cm(session)])

    rls = AsyncMock()
    resolver = AsyncMock(return_value=["a@example.com"])
    with (
        patch.object(dw, "set_rls_org", rls),
        patch.object(dw, "resolve_hitl_email_recipients", resolver),
    ):
        recipients = await dw._resolve_recipients(factory, _ORG, _PIPE)

    assert recipients == ["a@example.com"]
    rls.assert_awaited_once_with(session, _ORG)
    resolver.assert_awaited_once_with(session, _ORG, _PIPE)


def test_sweep_lock_key_distinct_from_siblings() -> None:
    """Three HITL system crons must never contend on one advisory lock."""
    assert dw._DEADLINE_LOCK_KEY not in (721_336_517, 721_336_518)
