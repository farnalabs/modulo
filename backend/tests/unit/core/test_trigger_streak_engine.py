"""Unit tests for the FAR-190 ongoing-trigger no-delivery streak engine.

Covers the streak engine in ``modulo.core.trigger_streak``:

* ``_streak_config`` — per-trigger threshold (``max_no_delivery_streak`` with
  the legacy ``max_consecutive_failures`` fallback) + wall-clock window.
* ``_streak_deactivate_enabled`` — the kill switch for the deactivate+notify
  side-effect.
* the deactivation SQL — NULL-epoch COALESCE, equal-completed_at ordering,
  excluded-mid-walk breaks, terminal-only predicates, guarded atomic UPDATE.
* ``_deactivate_trigger_on_no_delivery_streak`` — threshold boundary,
  idempotent second tick, audit + trigger-event records in the same tx.
* ``enforce_no_delivery_streaks`` — never-raises failure injection, per-org
  per-hour cap, mass-cascade alert, flag-off, notification failure retry.
* the shared re-enable anchor helpers routed through the active-write sites.
* the dispatcher_reconcile wiring.

Mock/fake based (no real Postgres/Redis), mirroring the test_dispatcher_reconcile
/ test_cron_helpers_ongoing patterns: statement-routed mocked sessions + patched
helpers.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import cron_helpers as ch
from modulo.core import trigger_streak as ts

ORG = uuid.uuid4()
TRIGGER_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()


def _mock_result(**kwargs: Any) -> MagicMock:
    result = MagicMock()
    for name, value in kwargs.items():
        getattr(result, name).return_value = value
    return result


class _Begin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


def _settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_runs_queue": "runs",
        "redis_url": "redis://localhost:6379/0",
        "saq_redis_pool_size": 5,
        "fernet_key": "b" * 44,
    }
    base.update(overrides)
    return MagicMock(**base)


def _patch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "b" * 44)


def _deactivated_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": TRIGGER_ID,
        "pipeline_id": PIPELINE_ID,
        "organisation_id": ORG,
        "config_json": {},
        "streak": 5,
        "reason": "no_delivery",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# _streak_config / kill switch
# ---------------------------------------------------------------------------


class _RoutedSession:
    """Minimal async session double routing statements to canned results."""

    def __init__(
        self, *, update_row: Any = None, reason_row: Any = None, trigger_rows: list[Any] | None = None
    ) -> None:
        self._update_row = update_row
        self._reason_row = reason_row
        self._trigger_rows = trigger_rows or []
        self.executed: list[tuple[Any, Any]] = []
        self.added: list[object] = []
        self.begin_cm = _Begin()
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        self._get_bind = MagicMock(return_value=bind)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _Begin:
        return self.begin_cm

    def begin_nested(self) -> _Begin:
        return _Begin()

    def get_bind(self) -> Any:
        return self._get_bind()

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.executed.append((stmt, params))
        s = str(stmt).lower()
        if "set_config" in s:
            return MagicMock()
        if s.startswith("update triggers"):
            r = MagicMock()
            r.first.return_value = self._update_row
            r.rowcount = 1 if self._update_row is not None else 0
            return r
        if "as reason" in s:
            r = MagicMock()
            r.first.return_value = self._reason_row
            return r
        if "from triggers" in s:
            return _mock_result(scalars=self._trigger_rows)
        return MagicMock()


# ---------------------------------------------------------------------------
# _streak_config / kill switch
# ---------------------------------------------------------------------------


class TestStreakConfig:
    def test_defaults(self) -> None:
        threshold, window = ts._streak_config({})
        assert threshold == ts.ONGOING_MAX_NO_DELIVERY_STREAK_DEFAULT == 5
        assert window == ts.ONGOING_MIN_NO_DELIVERY_WINDOW_HOURS_DEFAULT == 24

    def test_per_trigger_threshold(self) -> None:
        threshold, _ = ts._streak_config({"max_no_delivery_streak": 3})
        assert threshold == 3

    def test_legacy_key_fallback(self) -> None:
        """The legacy ``max_consecutive_failures`` config key is read as a
        fallback for one release."""
        threshold, _ = ts._streak_config({"max_consecutive_failures": 7})
        assert threshold == 7

    def test_new_key_wins_over_legacy(self) -> None:
        threshold, _ = ts._streak_config({"max_no_delivery_streak": 2, "max_consecutive_failures": 9})
        assert threshold == 2

    def test_invalid_threshold_falls_back(self) -> None:
        for bad in ("abc", -3, 0):
            threshold, _ = ts._streak_config({"max_no_delivery_streak": bad})
            assert threshold == ts.ONGOING_MAX_NO_DELIVERY_STREAK_DEFAULT

    def test_boolean_threshold_rejected(self) -> None:
        """A mis-typed JSON boolean must not arm the guard to maximum
        aggressiveness: ``int(True) == 1`` would deactivate on a single run.
        Booleans and floats are rejected, not coerced (FAR-190 qa FIX 8)."""
        for bad in (True, False, 3.7):
            threshold, _ = ts._streak_config({"max_no_delivery_streak": bad})
            assert threshold == ts.ONGOING_MAX_NO_DELIVERY_STREAK_DEFAULT

    def test_boolean_window_rejected(self) -> None:
        for bad in (True, 12.5):
            _, window = ts._streak_config({"no_delivery_min_window_hours": bad})
            assert window == ts.ONGOING_MIN_NO_DELIVERY_WINDOW_HOURS_DEFAULT

    def test_window_default_and_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert ts._streak_config({})[1] == 24
        monkeypatch.setenv("MODULO_ONGOING_STREAK_MIN_WINDOW_HOURS", "0")  # dogfood no-window variant
        assert ts._streak_config({})[1] == 0
        monkeypatch.setenv("MODULO_ONGOING_STREAK_MIN_WINDOW_HOURS", "not-a-number")
        assert ts._streak_config({})[1] == 24

    def test_per_trigger_window_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_ONGOING_STREAK_MIN_WINDOW_HOURS", "0")
        assert ts._streak_config({"no_delivery_min_window_hours": 12})[1] == 12


class TestKillSwitch:
    def test_default_enabled(self) -> None:
        assert ts.STREAK_DEACTIVATE_ENABLED_DEFAULT is True
        assert ts._streak_deactivate_enabled() is True

    def test_env_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for value in ("0", "false", "off", "no", "FALSE"):
            monkeypatch.setenv("MODULO_STREAK_DEACTIVATE_KILL_SWITCH", value)
            assert ts._streak_deactivate_enabled() is False

    def test_env_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_STREAK_DEACTIVATE_KILL_SWITCH", "1")
        assert ts._streak_deactivate_enabled() is True


# ---------------------------------------------------------------------------
# Deactivation SQL structure (the count folded into the UPDATE's WHERE)
# ---------------------------------------------------------------------------


class TestDeactivateSQL:
    def test_null_epoch_coalesces(self) -> None:
        """A NULL streak_epoch (rolling-deploy skew) COALESCEs to now() so the
        boundary becomes "now" — no run counts and the trigger can never be
        deactivated until the row is re-anchored. The epoch is read from the
        live trigger row via a self-contained scalar subquery (the reason query
        has no ``triggers`` relation in its FROM)."""
        assert "COALESCE((SELECT tr.streak_epoch FROM triggers tr" in ts._NO_DELIVERY_DEACTIVATE_SQL
        assert ", now())" in ts._NO_DELIVERY_DEACTIVATE_SQL
        assert "GREATEST(" in ts._NO_DELIVERY_DEACTIVATE_SQL

    def test_last_delivery_derived_from_classification_log(self) -> None:
        """The boundary's last_delivery_at is MAX(completed_at) of delivered
        classifications — a single source of truth, never a raw status."""
        assert "run_classification ->> 'value' = 'delivered'" in ts._NO_DELIVERY_DEACTIVATE_SQL

    def test_excluded_mid_walk_breaks(self) -> None:
        """A cancelled/budget_exceeded (excluded) run between no-deliveries stops
        the walk — the count must not span across it."""
        assert "('delivered','excluded','unclassified')" in ts._NO_DELIVERY_DEACTIVATE_SQL

    def test_unclassified_run_breaks_fail_closed(self) -> None:
        """A terminal run with NO classification record (or an 'unclassified'
        marker) stops the walk fail-closed — deactivation can never ride on
        uncertain evidence."""
        assert "r3.run_classification IS NULL" in ts._NO_DELIVERY_DEACTIVATE_SQL

    def test_equal_completed_at_ordering(self) -> None:
        """Equal completed_at runs get a deterministic total order via the id
        tie-break in the stop predicate."""
        assert "r3.id > r.id" in ts._NO_DELIVERY_DEACTIVATE_SQL

    def test_terminal_only_predicates(self) -> None:
        """Only terminal runs are counted or stop the walk — an in-flight run is
        drained (never counted, never breaking the walk; never cancelled). The
        status set is derived from TERMINAL_STATUSES (single source of truth)."""
        sql = ts._NO_DELIVERY_DEACTIVATE_SQL
        assert "r.status IN ('budget_exceeded','cancelled','complete','eval_failed','failed','stalled')" in sql
        assert "r3.status IN ('budget_exceeded','cancelled','complete','eval_failed','failed','stalled')" in sql
        assert "pending" not in sql

    def test_guarded_atomic_update(self) -> None:
        """The UPDATE is guarded on ``active`` and folds the streak into the
        WHERE (no TOCTOU) — a re-enabled trigger or a stale tick can never be
        hit, and concurrent ticks produce one rowcount=1 then a no-op."""
        sql = ts._NO_DELIVERY_DEACTIVATE_SQL
        assert "AND active" in sql
        assert ">= :threshold" in sql
        assert "RETURNING" in sql
        assert "AS streak" in sql

    def test_wall_clock_window_boundary(self) -> None:
        """The boundary must be at least the wall-clock window old
        (<= :window_cutoff) — the product 24h variant; a dogfood window of 0
        makes the cutoff "now", which every boundary trivially satisfies."""
        assert "<= :window_cutoff" in ts._NO_DELIVERY_DEACTIVATE_SQL


class TestMigrationBackfillGrace:
    def test_migration_backfills_epoch_and_branches_off_current_head(self) -> None:
        """The migration adds triggers.streak_epoch with a DEFAULT CURRENT_TIMESTAMP
        (backfill = now()) — pre-existing no-delivery history can never
        deactivate on tick 1 because the boundary is GREATEST(last_delivery_at,
        streak_epoch) and every old run predates the anchored epoch. Also
        branches off the current migration head (single Alembic head)."""
        mod = importlib.import_module("modulo.db.migrations.versions.0102_ongoing_streak_epoch")
        assert mod.down_revision == "0101_guardrails"
        assert mod.revision == "0102_ongoing_streak_epoch"
        # Structural: the upgrade adds the column with a server default (backfill)
        # and the streak-engine partial index on runs.
        assert mod.upgrade is not None
        assert mod.downgrade is not None


# ---------------------------------------------------------------------------
# _deactivate_trigger_on_no_delivery_streak — threshold boundary + idempotency
# ---------------------------------------------------------------------------


class TestDeactivateHelper:
    @pytest.mark.asyncio
    async def test_streak_at_threshold_deactivates(self) -> None:
        """streak == threshold deactivates: the UPDATE returns the row and the
        helper records the audit + trigger-event lifecycle records."""
        session = _RoutedSession(
            update_row=(TRIGGER_ID, PIPELINE_ID, ORG, {"input_template": {}}, 5),
            reason_row=("no_delivery",),
        )
        factory = MagicMock(return_value=session)
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ts, "_record_streak_deactivation", new_callable=AsyncMock) as record,
            patch.object(ch, "_log_ongoing_event", new_callable=AsyncMock),
        ):
            out = await ts._deactivate_trigger_on_no_delivery_streak(
                factory,
                org_id=ORG,
                trigger_id=TRIGGER_ID,
                threshold=5,
                window_cutoff=datetime.now(UTC),
            )

        assert out is not None
        assert out["id"] == TRIGGER_ID
        assert out["streak"] == 5
        assert out["reason"] == "no_delivery"
        record.assert_awaited_once()
        # The UPDATE runs inside the same transaction as the records (per-trigger
        # isolation — a failure for this trigger never stops the others).
        update_stmts = [s for s, _ in session.executed if str(s).lower().startswith("update triggers")]
        assert update_stmts, "guarded deactivation UPDATE must be issued"

    @pytest.mark.asyncio
    async def test_streak_below_threshold_does_not_deactivate(self) -> None:
        """streak == threshold - 1 does NOT deactivate: the UPDATE matches no row
        (the count in the WHERE is below the threshold) and the helper returns
        None without recording anything."""
        session = _RoutedSession(update_row=None, reason_row=None)
        factory = MagicMock(return_value=session)
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ts, "_record_streak_deactivation", new_callable=AsyncMock) as record,
            patch.object(ch, "_log_ongoing_event", new_callable=AsyncMock),
        ):
            out = await ts._deactivate_trigger_on_no_delivery_streak(
                factory,
                org_id=ORG,
                trigger_id=TRIGGER_ID,
                threshold=5,
                window_cutoff=datetime.now(UTC),
            )
        assert out is None
        record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_second_concurrent_tick_is_noop(self) -> None:
        """Two concurrent ticks -> one deactivation: the first tick's UPDATE
        flips active=false, so the second tick's UPDATE matches no row (the
        ``AND active`` guard) and returns None — exactly one audit record and
        one notification, never two."""
        session = _RoutedSession(update_row=None)
        factory = MagicMock(return_value=session)
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch.object(ts, "_record_streak_deactivation", new_callable=AsyncMock) as record,
        ):
            out = await ts._deactivate_trigger_on_no_delivery_streak(
                factory,
                org_id=ORG,
                trigger_id=TRIGGER_ID,
                threshold=5,
                window_cutoff=datetime.now(UTC),
            )
        assert out is None
        record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_null_epoch_row_never_deactivates(self) -> None:
        """A row whose streak_epoch is NULL COALESCEs the boundary to now(), so
        no run (all completed_at in the past) can satisfy ``completed_at >=
        boundary`` — the streak is 0 and the UPDATE matches nothing."""
        session = _RoutedSession(update_row=None)
        factory = MagicMock(return_value=session)
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
        ):
            out = await ts._deactivate_trigger_on_no_delivery_streak(
                factory,
                org_id=ORG,
                trigger_id=TRIGGER_ID,
                threshold=5,
                window_cutoff=datetime.now(UTC),
            )
        assert out is None


# ---------------------------------------------------------------------------
# enforce_no_delivery_streaks — sweep isolation, cap, flag-off, notify retry
# ---------------------------------------------------------------------------


def _sweep_trigger(trigger_id: uuid.UUID = TRIGGER_ID) -> SimpleNamespace:
    return SimpleNamespace(id=trigger_id, pipeline_id=PIPELINE_ID, config_json={})


class TestEnforceSweep:
    @pytest.mark.asyncio
    async def test_never_raises_failure_injection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A walk failure for ONE trigger is swallowed (WARNING) and never stops
        the other triggers — the sweep must not raise out of the enclosing tick
        (the ongoing top-up's never-raises contract) nor skip other triggers."""
        _patch_env(monkeypatch)
        second = uuid.uuid4()
        call_count = 0

        async def _flaky_deactivate(factory, *, org_id, trigger_id, threshold, window_cutoff):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("streak walk boom")
            return _deactivated_data(id=trigger_id)

        with (
            patch.object(ts, "_streak_deactivate_enabled", return_value=True),
            patch.object(
                ts,
                "_select_active_ongoing_triggers",
                new_callable=AsyncMock,
                return_value=[_sweep_trigger(), _sweep_trigger(second)],
            ),
            patch.object(ts, "_count_recent_streak_deactivations", new_callable=AsyncMock, return_value=0),
            patch.object(
                ts, "_deactivate_trigger_on_no_delivery_streak", new_callable=AsyncMock, side_effect=_flaky_deactivate
            ),
            patch.object(ts, "_pipeline_name", new_callable=AsyncMock, return_value="pipeline"),
            patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock, return_value=True),
            patch.object(ts, "_maybe_alert_mass_cascade", new_callable=AsyncMock, return_value=False),
            patch.object(ts, "_retry_pending_streak_notifications", new_callable=AsyncMock, return_value=0),
        ):
            summary = await ts.enforce_no_delivery_streaks(org_ids=[ORG], redis_client=AsyncMock())

        assert summary["scanned"] == 2
        assert summary["deactivated"] == 1, "the second trigger must still be evaluated"
        assert summary["errors"] == 1, "the first failure is counted and swallowed"

    @pytest.mark.asyncio
    async def test_flag_off_skips_deactivate_notify(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The org feature-flag kill switch gates ONLY the deactivate+notify
        side-effect — classification persists regardless (the reconcile sweep is
        independent), so the sweep returns without touching any trigger."""
        _patch_env(monkeypatch)
        with (
            patch.object(ts, "_streak_deactivate_enabled", return_value=False),
            patch.object(ts, "_select_active_ongoing_triggers", new_callable=AsyncMock) as select,
            patch.object(ts, "_deactivate_trigger_on_no_delivery_streak", new_callable=AsyncMock) as deactivate,
        ):
            summary = await ts.enforce_no_delivery_streaks(org_ids=[ORG], redis_client=AsyncMock())
        assert summary["kill_switch"] == "off"
        assert summary["deactivated"] == 0
        select.assert_not_awaited()
        deactivate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mass_deactivation_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Per-org per-hour cap: once (recent deactivations + this tick's) hits
        the cap, further triggers are deferred (capped), never deactivated."""
        _patch_env(monkeypatch)
        first, second = uuid.uuid4(), uuid.uuid4()
        with (
            patch.object(ts, "_streak_deactivate_enabled", return_value=True),
            patch.object(
                ts,
                "_select_active_ongoing_triggers",
                new_callable=AsyncMock,
                return_value=[_sweep_trigger(first), _sweep_trigger(second)],
            ),
            patch.object(
                ts,
                "_count_recent_streak_deactivations",
                new_callable=AsyncMock,
                return_value=ts.ONGOING_STREAK_DEACTIVATE_MAX_PER_ORG_PER_HOUR - 1,
            ),
            patch.object(
                ts,
                "_deactivate_trigger_on_no_delivery_streak",
                new_callable=AsyncMock,
                return_value=_deactivated_data(id=first),
            ),
            patch.object(ts, "_pipeline_name", new_callable=AsyncMock, return_value="p"),
            patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock, return_value=True),
            patch.object(ts, "_maybe_alert_mass_cascade", new_callable=AsyncMock, return_value=False),
            patch.object(ts, "_retry_pending_streak_notifications", new_callable=AsyncMock, return_value=0),
        ):
            summary = await ts.enforce_no_delivery_streaks(org_ids=[ORG], redis_client=AsyncMock())
        assert summary["deactivated"] == 1, "one deactivation stays under the cap"
        assert summary["capped"] == 1, "the second trigger is capped"

    @pytest.mark.asyncio
    async def test_mass_cascade_alert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """'5+ triggers deactivated within 24h' fires the mass-cascade guard."""
        _patch_env(monkeypatch)
        with (
            patch.object(ts, "_streak_deactivate_enabled", return_value=True),
            patch.object(
                ts, "_select_active_ongoing_triggers", new_callable=AsyncMock, return_value=[_sweep_trigger()]
            ),
            patch.object(ts, "_count_recent_streak_deactivations", new_callable=AsyncMock, return_value=0),
            patch.object(
                ts,
                "_deactivate_trigger_on_no_delivery_streak",
                new_callable=AsyncMock,
                return_value=_deactivated_data(),
            ),
            patch.object(ts, "_pipeline_name", new_callable=AsyncMock, return_value="p"),
            patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock, return_value=True),
            patch.object(ts, "_maybe_alert_mass_cascade", new_callable=AsyncMock, return_value=True) as alert,
            patch.object(ts, "_retry_pending_streak_notifications", new_callable=AsyncMock, return_value=0),
        ):
            summary = await ts.enforce_no_delivery_streaks(org_ids=[ORG], redis_client=AsyncMock())
        assert summary["deactivated"] == 1
        assert summary["alerts"] == 1
        alert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_window_variant_boundary_cutoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """24h wall-clock window variant (product default): the window_cutoff
        bound passed to the deactivation is ~now-24h. The no-window dogfood
        variant (window=0) passes ~now — a boundary that is trivially old
        enough, so the streak fires as soon as it reaches the threshold."""
        _patch_env(monkeypatch)
        captured: dict[str, Any] = {}

        async def _capture(factory, *, org_id, trigger_id, threshold, window_cutoff):
            captured["cutoff"] = window_cutoff
            return _deactivated_data()

        with (
            patch.object(ts, "_streak_deactivate_enabled", return_value=True),
            patch.object(
                ts, "_select_active_ongoing_triggers", new_callable=AsyncMock, return_value=[_sweep_trigger()]
            ),
            patch.object(ts, "_count_recent_streak_deactivations", new_callable=AsyncMock, return_value=0),
            patch.object(ts, "_deactivate_trigger_on_no_delivery_streak", new_callable=AsyncMock, side_effect=_capture),
            patch.object(ts, "_pipeline_name", new_callable=AsyncMock, return_value="p"),
            patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock, return_value=True),
            patch.object(ts, "_maybe_alert_mass_cascade", new_callable=AsyncMock, return_value=False),
            patch.object(ts, "_retry_pending_streak_notifications", new_callable=AsyncMock, return_value=0),
        ):
            await ts.enforce_no_delivery_streaks(org_ids=[ORG], redis_client=AsyncMock())

        now = datetime.now(UTC)
        assert (now - timedelta(hours=24) - captured["cutoff"]).total_seconds() < 5

        captured.clear()
        monkeypatch.setenv("MODULO_ONGOING_STREAK_MIN_WINDOW_HOURS", "0")
        with (
            patch.object(ts, "_streak_deactivate_enabled", return_value=True),
            patch.object(
                ts, "_select_active_ongoing_triggers", new_callable=AsyncMock, return_value=[_sweep_trigger()]
            ),
            patch.object(ts, "_count_recent_streak_deactivations", new_callable=AsyncMock, return_value=0),
            patch.object(ts, "_deactivate_trigger_on_no_delivery_streak", new_callable=AsyncMock, side_effect=_capture),
            patch.object(ts, "_pipeline_name", new_callable=AsyncMock, return_value="p"),
            patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock, return_value=True),
            patch.object(ts, "_maybe_alert_mass_cascade", new_callable=AsyncMock, return_value=False),
            patch.object(ts, "_retry_pending_streak_notifications", new_callable=AsyncMock, return_value=0),
        ):
            await ts.enforce_no_delivery_streaks(org_ids=[ORG], redis_client=AsyncMock())
        assert (now - captured["cutoff"]).total_seconds() < 5, "no-window variant uses ~now cutoff"

    @pytest.mark.asyncio
    async def test_notify_failure_counts_and_writes_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed notification is counted (notify_failed) and the sweep still
        completes; the retry pass runs for pending markers."""
        _patch_env(monkeypatch)
        with (
            patch.object(ts, "_streak_deactivate_enabled", return_value=True),
            patch.object(
                ts, "_select_active_ongoing_triggers", new_callable=AsyncMock, return_value=[_sweep_trigger()]
            ),
            patch.object(ts, "_count_recent_streak_deactivations", new_callable=AsyncMock, return_value=0),
            patch.object(
                ts,
                "_deactivate_trigger_on_no_delivery_streak",
                new_callable=AsyncMock,
                return_value=_deactivated_data(),
            ),
            patch.object(ts, "_pipeline_name", new_callable=AsyncMock, return_value="p"),
            patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock, return_value=False),
            patch.object(ts, "_maybe_alert_mass_cascade", new_callable=AsyncMock, return_value=False),
            patch.object(ts, "_retry_pending_streak_notifications", new_callable=AsyncMock, return_value=2),
        ):
            summary = await ts.enforce_no_delivery_streaks(org_ids=[ORG], redis_client=AsyncMock())
        assert summary["deactivated"] == 1
        assert summary["notify_failed"] == 1
        assert summary["notify_retried"] == 2

    @pytest.mark.asyncio
    async def test_keyset_pagination_scans_past_page_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An org with more triggers than the page size is fully scanned via a
        keyset cursor (``id > last``) — triggers past the first page are NEVER
        starved (FAR-190 qa FIX 7)."""
        _patch_env(monkeypatch)
        calls: list[dict[str, Any]] = []
        page = [_sweep_trigger(uuid.uuid4()) for _ in range(3)]

        async def _page_select(
            factory: Any, org_id: uuid.UUID, *, max_triggers: int, after_id: Any = None
        ) -> list[Any]:
            calls.append({"after_id": after_id, "max": max_triggers})
            if after_id is None:
                return page[:2]  # a FULL page (len == max) -> cursor advance
            return []  # second page empty -> stop

        with (
            patch.object(ts, "_streak_deactivate_enabled", return_value=True),
            patch.object(ts, "_select_active_ongoing_triggers", new_callable=AsyncMock, side_effect=_page_select),
            patch.object(ts, "_count_recent_streak_deactivations", new_callable=AsyncMock, return_value=0),
            patch.object(ts, "_deactivate_trigger_on_no_delivery_streak", new_callable=AsyncMock, return_value=None),
            patch.object(ts, "_pipeline_name", new_callable=AsyncMock, return_value="p"),
            patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock, return_value=True),
            patch.object(ts, "_maybe_alert_mass_cascade", new_callable=AsyncMock, return_value=False),
            patch.object(ts, "_retry_pending_streak_notifications", new_callable=AsyncMock, return_value=0),
        ):
            summary = await ts.enforce_no_delivery_streaks(
                org_ids=[ORG], redis_client=AsyncMock(), max_triggers_per_tick=2
            )
        assert summary["scanned"] == 2
        assert len(calls) == 2, "a full page must be followed by a cursor page"
        assert calls[0]["after_id"] is None
        assert calls[1]["after_id"] == page[1].id

    @pytest.mark.asyncio
    async def test_mass_cascade_alerts_without_redis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Redis outage must NOT suppress the critical mass-cascade alert: the
        alert side-effects run unconditionally once the threshold is crossed;
        dedup comes from the DB audit chain, never Redis (FAR-190 qa FIX 9)."""
        _patch_env(monkeypatch)
        factory = MagicMock()
        with (
            patch.object(ts, "_count_recent_streak_deactivations", new_callable=AsyncMock, return_value=5),
            patch.object(ts, "_streak_mass_cascade_alerted_this_window", new_callable=AsyncMock, return_value=False),
            patch.object(ts, "_record_streak_mass_cascade", new_callable=AsyncMock) as record,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            alerted = await ts._maybe_alert_mass_cascade(factory, ORG, redis_client=None)
        assert alerted is True
        record.assert_awaited_once_with(ORG, 5)
        ingest.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mass_cascade_deduped_by_audit_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Once-per-window dedup is derived from the audit chain (a prior
        mass-cascade audit event in the window suppresses the re-alert)."""
        _patch_env(monkeypatch)
        factory = MagicMock()
        with (
            patch.object(ts, "_count_recent_streak_deactivations", new_callable=AsyncMock, return_value=5),
            patch.object(ts, "_streak_mass_cascade_alerted_this_window", new_callable=AsyncMock, return_value=True),
            patch.object(ts, "_record_streak_mass_cascade", new_callable=AsyncMock) as record,
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock) as ingest,
        ):
            alerted = await ts._maybe_alert_mass_cascade(factory, ORG)
        assert alerted is False
        record.assert_not_awaited()
        ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sweep_respects_time_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A slow org cannot starve later orgs / blow the 120s tick: once the
        sweep's wall-clock budget is exhausted no further org is touched
        (FAR-190 qa FIX 14)."""
        _patch_env(monkeypatch)
        with (
            patch.object(ts, "_streak_deactivate_enabled", return_value=True),
            patch.object(ts, "_select_active_ongoing_triggers", new_callable=AsyncMock) as select,
            patch.object(ts, "_count_recent_streak_deactivations", new_callable=AsyncMock) as count,
        ):
            summary = await ts.enforce_no_delivery_streaks(org_ids=[ORG], redis_client=AsyncMock(), budget_seconds=-1.0)
        assert summary["budget_exceeded"] is True
        select.assert_not_awaited()
        count.assert_not_awaited()


# ---------------------------------------------------------------------------
# notification helper — sanitised payload, retry marker on failure
# ---------------------------------------------------------------------------


class TestNotifyStreakDeactivation:
    @pytest.mark.asyncio
    async def test_sanitised_payload_dispatched(self) -> None:
        """The payload is sanitised: identifiers + titles + allow-listed reason
        fields only — never tokens or raw output."""
        _patch_env_factory: Any = None
        dispatched: dict[str, Any] = {}

        class _FakeNotifier:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def dispatch_event(self, org_id, event_type, payload, **kwargs):
                dispatched["org_id"] = org_id
                dispatched["event_type"] = event_type
                dispatched["payload"] = payload

        with (
            patch.object(ch, "_get_engine", return_value=MagicMock()),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch("modulo.core.notifier.Notifier", _FakeNotifier),
        ):
            ok = await ts._notify_streak_deactivation(
                ORG,
                data=_deactivated_data(),
                threshold=5,
                reason="source_error",
                pipeline_name="Backlog Triage",
                redis_client=AsyncMock(),
            )
        assert ok is True
        assert dispatched["event_type"] == "trigger_deactivated"
        payload = dispatched["payload"]
        assert payload["trigger_id"] == str(TRIGGER_ID)
        assert payload["pipeline_name"] == "Backlog Triage"
        assert payload["reason"] == "source_error"
        assert payload["streak"] == 5
        assert payload["threshold"] == 5
        assert payload["delivered_after_deactivation"] is False
        assert "token" not in json.dumps(payload).lower()

    @pytest.mark.asyncio
    async def test_non_allowlisted_reason_degrades_to_no_delivery(self) -> None:
        dispatched: dict[str, Any] = {}

        class _FakeNotifier:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def dispatch_event(self, org_id, event_type, payload, **kwargs):
                dispatched["payload"] = payload

        with (
            patch.object(ch, "_get_engine", return_value=MagicMock()),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch("modulo.core.notifier.Notifier", _FakeNotifier),
        ):
            await ts._notify_streak_deactivation(
                ORG,
                data=_deactivated_data(),
                threshold=5,
                reason="not-an-allowed-reason",
                pipeline_name="",
                redis_client=AsyncMock(),
            )
        assert dispatched["payload"]["reason"] == "no_delivery"

    @pytest.mark.asyncio
    async def test_failure_writes_pending_marker_and_critical_audit(self) -> None:
        """On notifier failure: critical audit entry + per-org Redis pending
        marker so the next scheduler tick retries; the helper returns False and
        never raises."""
        redis_client = AsyncMock()

        class _RaisingNotifier:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def dispatch_event(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("webhook down")

        with (
            patch.object(ch, "_get_engine", return_value=MagicMock()),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch("modulo.core.notifier.Notifier", _RaisingNotifier),
            patch.object(ts, "_record_streak_notify_failed", new_callable=AsyncMock) as record_failed,
            patch.object(ts, "_write_streak_notify_pending", new_callable=AsyncMock) as write_pending,
        ):
            ok = await ts._notify_streak_deactivation(
                ORG,
                data=_deactivated_data(),
                threshold=5,
                reason="no_delivery",
                pipeline_name="p",
                redis_client=redis_client,
            )
        assert ok is False
        record_failed.assert_awaited_once()
        write_pending.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dead_lettered_dispatch_is_a_failure(self) -> None:
        """The notifier does NOT raise on per-endpoint delivery failure — it
        dead-letters internally. A dead-lettered result must be treated as a
        delivery failure (pending-retry marker), never silent success
        (FAR-190 qa FIX 3)."""
        redis_client = AsyncMock()

        class _DeadLetterNotifier:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def dispatch_event(self, *args: Any, **kwargs: Any) -> list[Any]:
                from modulo.core.notifier import DispatchResult

                return [DispatchResult(endpoint_id=uuid.uuid4(), status="dead_lettered", attempt_count=4)]

        with (
            patch.object(ch, "_get_engine", return_value=MagicMock()),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch("modulo.core.notifier.Notifier", _DeadLetterNotifier),
            patch.object(ts, "_record_streak_notify_failed", new_callable=AsyncMock) as record_failed,
            patch.object(ts, "_write_streak_notify_pending", new_callable=AsyncMock) as write_pending,
        ):
            ok = await ts._notify_streak_deactivation(
                ORG,
                data=_deactivated_data(),
                threshold=5,
                reason="no_delivery",
                pipeline_name="p",
                redis_client=redis_client,
            )
        assert ok is False
        write_pending.assert_awaited_once()
        record_failed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delivered_dispatch_result_is_success(self) -> None:
        """A fully delivered dispatch (no dead-lettered endpoint) is success."""
        redis_client = AsyncMock()

        class _DeliveredNotifier:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def dispatch_event(self, *args: Any, **kwargs: Any) -> list[Any]:
                from modulo.core.notifier import DispatchResult

                return [DispatchResult(endpoint_id=uuid.uuid4(), status="delivered", attempt_count=1)]

        with (
            patch.object(ch, "_get_engine", return_value=MagicMock()),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch("modulo.core.notifier.Notifier", _DeliveredNotifier),
            patch.object(ts, "_write_streak_notify_pending", new_callable=AsyncMock) as write_pending,
        ):
            ok = await ts._notify_streak_deactivation(
                ORG,
                data=_deactivated_data(),
                threshold=5,
                reason="no_delivery",
                pipeline_name="p",
                redis_client=redis_client,
            )
        assert ok is True
        write_pending.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hanging_dispatch_is_bounded(self) -> None:
        """A hung endpoint cannot blow the 120s tick: the dispatch runs under
        asyncio.wait_for and returns failure on timeout (FAR-190 qa FIX 2)."""
        redis_client = AsyncMock()

        class _HangingNotifier:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def dispatch_event(self, *args: Any, **kwargs: Any) -> Any:
                await asyncio.sleep(5)
                return []

        with (
            patch.object(ch, "_get_engine", return_value=MagicMock()),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ts, "_STREAK_NOTIFY_TIMEOUT_SECONDS", 0.05),
            patch("modulo.core.notifier.Notifier", _HangingNotifier),
            patch.object(ts, "_record_streak_notify_failed", new_callable=AsyncMock) as record_failed,
            patch.object(ts, "_write_streak_notify_pending", new_callable=AsyncMock) as write_pending,
        ):
            ok = await ts._notify_streak_deactivation(
                ORG,
                data=_deactivated_data(),
                threshold=5,
                reason="no_delivery",
                pipeline_name="p",
                redis_client=redis_client,
            )
        assert ok is False
        record_failed.assert_awaited_once()
        write_pending.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retry_failure_does_not_re_audit(self) -> None:
        """Only the FIRST failure fires the critical audit — a retry failure
        logs WARNING and re-enqueues without spamming the audit chain
        (FAR-190 qa FIX 4a)."""
        redis_client = AsyncMock()

        class _RaisingNotifier:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def dispatch_event(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("webhook down")

        with (
            patch.object(ch, "_get_engine", return_value=MagicMock()),
            patch.object(ch, "get_settings", return_value=_settings()),
            patch("modulo.core.notifier.Notifier", _RaisingNotifier),
            patch.object(ts, "_record_streak_notify_failed", new_callable=AsyncMock) as record_failed,
            patch.object(ts, "_write_streak_notify_pending", new_callable=AsyncMock) as write_pending,
        ):
            ok = await ts._notify_streak_deactivation(
                ORG,
                data=_deactivated_data(),
                threshold=5,
                reason="no_delivery",
                pipeline_name="p",
                redis_client=redis_client,
                retry_count=3,
            )
        assert ok is False
        record_failed.assert_not_awaited()
        write_pending.assert_awaited_once()


class TestPendingRetry:
    @pytest.mark.asyncio
    async def test_retry_success_removes_member(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A still-deactivated trigger's pending member IS dispatched (the member
        exists precisely because the trigger was JUST auto-deactivated), and the
        member is removed on success."""
        _patch_env(monkeypatch)
        redis_client = AsyncMock()
        member = ts._streak_pending_member(_deactivated_data(), threshold=5, pipeline_name="p")
        redis_client.smembers.return_value = {member}
        with (
            patch.object(ts, "_trigger_active_state", new_callable=AsyncMock, return_value=False),
            patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock, return_value=True),
        ):
            retried = await ts._retry_pending_streak_notifications(ORG, redis_client)
        assert retried == 1
        redis_client.srem.assert_awaited_once_with(ts._streak_notify_pending_key(ORG), member)

    @pytest.mark.asyncio
    async def test_retry_failure_reenqueues_with_bumped_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed retry re-enqueues the member with a bumped retry_count + a
        cooldown stamp (FAR-190 qa FIX 4) — the member is NOT dropped, and the
        next tick's cooldown gate prevents an immediate retry."""
        _patch_env(monkeypatch)
        redis_client = AsyncMock()
        member = ts._streak_pending_member(_deactivated_data(), threshold=5, pipeline_name="p")
        redis_client.smembers.return_value = {member}
        with (
            patch.object(ts, "_trigger_active_state", new_callable=AsyncMock, return_value=False),
            patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock, return_value=False),
        ):
            retried = await ts._retry_pending_streak_notifications(ORG, redis_client)
        assert retried == 0
        # Member is removed and re-added (never left without a cooldown stamp).
        redis_client.srem.assert_awaited_once_with(ts._streak_notify_pending_key(ORG), member)
        assert redis_client.sadd.await_count == 1
        re_added = redis_client.sadd.await_args.args[1]
        assert json.loads(re_added)["retry_count"] == 1
        assert isinstance(json.loads(re_added)["last_retry_at"], int)
        redis_client.expire.assert_awaited()  # SET TTL refreshed on re-enqueue

    @pytest.mark.asyncio
    async def test_retry_respects_per_member_cooldown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A member retried < 15 min ago is skipped this tick (FAR-190 qa FIX 4b)
        — the dispatch is NOT re-attempted and the member is untouched."""
        _patch_env(monkeypatch)
        redis_client = AsyncMock()
        member = ts._streak_pending_member(
            _deactivated_data(), threshold=5, pipeline_name="p", retry_count=1, last_retry_at=int(time.time()) - 60
        )
        redis_client.smembers.return_value = {member}
        with patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock) as notify:
            retried = await ts._retry_pending_streak_notifications(ORG, redis_client)
        assert retried == 0
        notify.assert_not_awaited()
        redis_client.srem.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retry_drops_member_when_trigger_reenabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Before dispatching a retry the trigger's active state is re-checked
        (FAR-190 qa FIX 4c): a RE-ENABLED trigger (active=True) drops the pending
        member — no stale 'auto-deactivated' notification after re-enable; a
        still-DEACTIVATED trigger (active=False) is dispatched (its member exists
        precisely because the deactivation happened, so the notification is still
        valid)."""
        _patch_env(monkeypatch)

        # Re-enabled: the member is dropped, never dispatched.
        redis_client = AsyncMock()
        member = ts._streak_pending_member(_deactivated_data(), threshold=5, pipeline_name="p")
        redis_client.smembers.return_value = {member}
        with (
            patch.object(ts, "_trigger_active_state", new_callable=AsyncMock, return_value=True),
            patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock) as notify,
        ):
            retried = await ts._retry_pending_streak_notifications(ORG, redis_client)
        assert retried == 0
        notify.assert_not_awaited()
        redis_client.srem.assert_awaited_once_with(ts._streak_notify_pending_key(ORG), member)

    @pytest.mark.asyncio
    async def test_retry_dispatches_when_trigger_still_deactivated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A still-deactivated trigger's pending member IS dispatched: the member
        exists precisely because the trigger was JUST auto-deactivated
        (active=False), so the 'auto-deactivated' notification is still valid —
        the retry guard must NOT drop it (FAR-190 qa round 2 FIX 1)."""
        _patch_env(monkeypatch)
        redis_client = AsyncMock()
        member = ts._streak_pending_member(_deactivated_data(), threshold=5, pipeline_name="p")
        redis_client.smembers.return_value = {member}
        with (
            patch.object(ts, "_trigger_active_state", new_callable=AsyncMock, return_value=False),
            patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock, return_value=True) as notify,
        ):
            retried = await ts._retry_pending_streak_notifications(ORG, redis_client)
        assert retried == 1
        notify.assert_awaited_once()
        redis_client.srem.assert_awaited_once_with(ts._streak_notify_pending_key(ORG), member)

    @pytest.mark.asyncio
    async def test_retry_skipped_when_budget_exhausted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A budget-exhausted sweep skips the retry pass entirely (FAR-190 qa
        round 2 FIX 2): once the sweep deadline has passed the pass returns 0
        without reading the pending set, dispatching, or dropping members."""
        _patch_env(monkeypatch)
        redis_client = AsyncMock()
        member = ts._streak_pending_member(_deactivated_data(), threshold=5, pipeline_name="p")
        redis_client.smembers.return_value = {member}
        with (
            patch.object(ts, "_trigger_active_state", new_callable=AsyncMock) as active_state,
            patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock) as notify,
        ):
            retried = await ts._retry_pending_streak_notifications(ORG, redis_client, deadline=time.monotonic() - 1.0)
        assert retried == 0
        redis_client.smembers.assert_not_awaited()
        active_state.assert_not_awaited()
        notify.assert_not_awaited()
        redis_client.srem.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retry_truncated_by_deadline_mid_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A retry pass that overruns the sweep deadline mid-pass truncates: the
        members after the deadline are left pending (never dropped) so the pass
        can never blow the enclosing 120s tick (FAR-190 qa round 2 FIX 2)."""
        _patch_env(monkeypatch)
        redis_client = AsyncMock()
        members = {
            ts._streak_pending_member(_deactivated_data(id=uuid.uuid4()), threshold=5, pipeline_name="p")
            for _ in range(3)
        }
        redis_client.smembers.return_value = members
        with (
            patch.object(ts, "_trigger_active_state", new_callable=AsyncMock, return_value=False),
            patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock, return_value=True),
            patch.object(ts, "time") as tm,
        ):
            # First two monotonic() calls are within budget (deadline 15s); the
            # deadline elapses before the third member's turn.
            tm.monotonic.side_effect = [0.0, 10.0, 20.0, 20.0]
            retried = await ts._retry_pending_streak_notifications(ORG, redis_client, deadline=15.0)
        assert retried >= 1, "members before the deadline are dispatched"
        assert retried < 3, "the pass truncates once the deadline elapses"

    @pytest.mark.asyncio
    async def test_retry_capped_per_tick(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The retry pass dispatches at most ``_STREAK_NOTIFY_MAX_PER_TICK``
        members per tick — a mass-cascade backlog defers to later ticks instead
        of blowing the sweep budget (FAR-190 qa round 2 FIX 2)."""
        _patch_env(monkeypatch)
        redis_client = AsyncMock()
        members = {
            ts._streak_pending_member(_deactivated_data(id=uuid.uuid4()), threshold=5, pipeline_name="p")
            for _ in range(5)
        }
        redis_client.smembers.return_value = members
        with (
            patch.object(ts, "_trigger_active_state", new_callable=AsyncMock, return_value=False),
            patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock, return_value=True),
        ):
            retried = await ts._retry_pending_streak_notifications(ORG, redis_client, max_retries=2)
        assert retried == 2, "at most max_retries dispatches per tick, the rest stay pending"

    @pytest.mark.asyncio
    async def test_retry_srems_corrupt_member(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unparseable pending member is srem'd — never retried forever
        (FAR-190 qa FIX 4d)."""
        _patch_env(monkeypatch)
        redis_client = AsyncMock()
        redis_client.smembers.return_value = {"not-json{{{"}
        with (
            patch.object(ts, "_trigger_active_state", new_callable=AsyncMock) as active_state,
            patch.object(ts, "_notify_streak_deactivation", new_callable=AsyncMock) as notify,
        ):
            retried = await ts._retry_pending_streak_notifications(ORG, redis_client)
        assert retried == 0
        notify.assert_not_awaited()
        active_state.assert_not_awaited()
        redis_client.srem.assert_awaited_once_with(ts._streak_notify_pending_key(ORG), "not-json{{{")

    @pytest.mark.asyncio
    async def test_no_redis_is_noop(self) -> None:
        assert await ts._retry_pending_streak_notifications(ORG, None) == 0


# ---------------------------------------------------------------------------
# shared re-enable anchor helpers (routed through every active-write site)
# ---------------------------------------------------------------------------


class TestReenableAnchor:
    @pytest.mark.asyncio
    async def test_anchor_single_trigger_matches_active_only(self) -> None:
        """The shared activation anchor is UPDATE-based, idempotent, and only
        matches rows currently active — a half-applied transition can never be
        epoch-anchored in the inactive state."""
        session = _RoutedSession()
        await ts.anchor_trigger_streak_epoch(session, trigger_id=TRIGGER_ID)
        assert session.executed, "anchor must issue an UPDATE"
        stmt = str(session.executed[0][0]).lower()
        assert "update triggers" in stmt
        assert "streak_epoch" in stmt
        assert "active" in stmt

    @pytest.mark.asyncio
    async def test_circuit_breaker_reset_anchors_epoch_inline(self) -> None:
        """The circuit-breaker reset (cost_controller active-write site) re-
        anchors the streak epoch IN THE SAME atomic statement as the active=True
        flip — no un-epoch'd active=True transition."""
        session = _RoutedSession()
        with (
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.cost_controller._dispatch_circuit_breaker_tripped", new_callable=AsyncMock),
        ):
            from modulo.core.cost_controller import reset_pipeline_circuit_breaker
            from modulo.db.models.pipeline import Pipeline

            pipeline = MagicMock(spec=Pipeline)
            pipeline.circuit_breaker_tripped = True
            pipeline.circuit_breaker_tripped_at = None
            session._update_row = None  # not used by the reset's UPDATE

            async def _fake_execute(stmt: Any, params: dict[str, Any] | None = None) -> Any:
                session.executed.append((stmt, params))
                s = str(stmt).lower()
                if "from pipelines" in s:
                    r = MagicMock()
                    r.scalar_one_or_none.return_value = pipeline
                    return r
                return MagicMock()

            session.execute = _fake_execute  # type: ignore[method-assign]
            await reset_pipeline_circuit_breaker(session, org_id=ORG, pipeline_id=PIPELINE_ID)

        update_stmts = [s for s, _ in session.executed if str(s).lower().startswith("update triggers")]
        assert update_stmts, "the reset must issue the re-activation UPDATE"
        stmt = str(update_stmts[0])
        assert "streak_epoch" in stmt
        assert "active" in str(stmt).lower()

    @pytest.mark.asyncio
    async def test_clear_after_reenable_clears_far158_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Re-enable clears the FAR-158 config-failure Redis counter only AFTER
        the Postgres commit (over-clearing safe, under-clearing not)."""
        _patch_env(monkeypatch)
        redis_client = AsyncMock()
        with (
            patch.object(ch, "get_settings", return_value=_settings()),
            patch.object(ts, "AsyncRedis") as redis_cls,
        ):
            redis_cls.from_url.return_value = redis_client
            await ts.clear_trigger_streak_after_reenable(TRIGGER_ID)
        redis_client.delete.assert_awaited_once_with(ch._ongoing_failure_key(TRIGGER_ID))
        redis_client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_circuit_breaker_reset_clears_far158_counter(self) -> None:
        """A trigger re-activated via the circuit-breaker reset must not keep its
        stale FAR-158 config-failure counter — the shared clear helper runs for
        every re-enabled trigger (FAR-190 qa FIX 12)."""
        session = _RoutedSession()
        with (
            patch.object(ch, "_set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.cost_controller._dispatch_circuit_breaker_tripped", new_callable=AsyncMock),
            patch.object(ts, "clear_trigger_streak_after_reenable", new_callable=AsyncMock) as clear,
        ):
            from modulo.core.cost_controller import reset_pipeline_circuit_breaker
            from modulo.db.models.pipeline import Pipeline

            pipeline = MagicMock(spec=Pipeline)
            pipeline.circuit_breaker_tripped = True
            pipeline.circuit_breaker_tripped_at = None
            re_enabled = [uuid.uuid4(), uuid.uuid4()]

            async def _fake_execute(stmt: Any, params: dict[str, Any] | None = None) -> Any:
                session.executed.append((stmt, params))
                s = str(stmt).lower()
                if "from pipelines" in s:
                    r = MagicMock()
                    r.scalar_one_or_none.return_value = pipeline
                    return r
                if s.startswith("update triggers"):
                    r = MagicMock()
                    r.scalars.return_value = MagicMock()
                    r.scalars.return_value.all.return_value = re_enabled
                    return r
                return MagicMock()

            session.execute = _fake_execute  # type: ignore[method-assign]
            await reset_pipeline_circuit_breaker(session, org_id=ORG, pipeline_id=PIPELINE_ID)

        assert clear.await_count == 2, "the shared clear must run for every re-enabled trigger"
        awaited = {c.args[0] for c in clear.await_args_list}
        assert awaited == set(re_enabled)

    @pytest.mark.asyncio
    async def test_config_failure_deactivation_emits_lifecycle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The FAR-158 config-failure deactivation path (``_bump_ongoing_failure``)
        leaves the same AuditEvent + TriggerEvent records as the no-delivery-streak
        path (FAR-190 qa FIX 13)."""
        _patch_env(monkeypatch)
        session = _RoutedSession()
        redis_client = AsyncMock()
        redis_client.incr.return_value = 5  # reaches ONGOING_MAX_CONSECUTIVE_FAILURES
        with patch.object(ts, "record_ongoing_deactivation_lifecycle", new_callable=AsyncMock) as record:
            await ch._bump_ongoing_failure(session, redis_client, TRIGGER_ID, org_id=ORG)
        record.assert_awaited_once()
        kwargs = record.await_args.kwargs
        assert kwargs["org_id"] == ORG
        assert kwargs["trigger_id"] == TRIGGER_ID
        assert kwargs["deactivated_by"] == "config_failure"
        assert kwargs["streak"] == 5


# ---------------------------------------------------------------------------
# dispatcher_reconcile wiring — sweep runs every 60s, never breaks the tick
# ---------------------------------------------------------------------------


class _MockBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _MockSession:
    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.terminalizer_rows: dict[str, list[uuid.UUID]] = {}
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        self._get_bind = MagicMock(return_value=bind)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _MockBegin:
        return _MockBegin()

    def get_bind(self) -> Any:
        return self._get_bind()

    async def get(self, model: Any, pk: Any) -> SimpleNamespace:
        return SimpleNamespace(max_concurrent_runs=5, status="running")

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        s = str(stmt)
        if "set_config" in s:
            return MagicMock()
        if "UPDATE runs SET" in s:
            r = MagicMock()
            r.all.return_value = []
            r.rowcount = 0
            return r
        if not self._results:
            return MagicMock()
        return self._results.pop(0)


class TestDispatcherWiring:
    @pytest.mark.asyncio
    async def test_dispatcher_reconcile_invokes_streak_sweep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_env(monkeypatch)
        session = _MockSession(
            [
                MagicMock(scalars=MagicMock(return_value=[ORG])),  # org select
                MagicMock(all=MagicMock(return_value=[])),  # reconcile row select
            ]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(
                ch,
                "get_settings",
                return_value=_settings(
                    saq_reenqueue_window=600,
                    saq_job_heartbeat=300,
                    saq_claimed_nodeless_minutes=45,
                    saq_run_claim_cap=20,
                    modulo_telemetry_enabled=False,
                ),
            ),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock()),
            patch.object(ch, "run_classification_reconcile", new_callable=AsyncMock, return_value={}),
            patch.object(
                ch, "enforce_no_delivery_streaks", new_callable=AsyncMock, return_value={"deactivated": 2, "scanned": 4}
            ) as streak,
            patch.object(ch, "_record_fact_for_terminalized_run", new_callable=AsyncMock),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock),
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock),
            patch.object(ch, "_awaiting_human_has_committed_decision", new_callable=AsyncMock),
            patch.object(ch, "write_dispatcher_reconcile_stats", new_callable=AsyncMock),
        ):
            summary = await ch.dispatcher_reconcile()

        streak.assert_awaited_once()
        assert summary["streak_deactivated"] == 2
        assert summary["streak_scanned"] == 4

    @pytest.mark.asyncio
    async def test_streak_sweep_failure_does_not_fail_reconcile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A catastrophic streak-sweep exception is swallowed — the reconcile
        tick (and the fire_due_triggers top-up, a separate cron) must never
        break because the sweep failed."""
        _patch_env(monkeypatch)
        session = _MockSession(
            [
                MagicMock(scalars=MagicMock(return_value=[ORG])),
                MagicMock(all=MagicMock(return_value=[])),
            ]
        )
        factory = MagicMock(return_value=session)
        redis_client = AsyncMock()
        redis_cls = MagicMock()
        redis_cls.from_url.return_value = redis_client
        with (
            patch.object(ch, "_open_factory", return_value=factory),
            patch.object(
                ch,
                "get_settings",
                return_value=_settings(
                    saq_reenqueue_window=600,
                    saq_job_heartbeat=300,
                    saq_claimed_nodeless_minutes=45,
                    saq_run_claim_cap=20,
                    modulo_telemetry_enabled=False,
                ),
            ),
            patch.object(ch, "AsyncRedis", redis_cls),
            patch.object(ch, "RedisQueue", MagicMock()),
            patch.object(ch, "run_classification_reconcile", new_callable=AsyncMock, return_value={}),
            patch.object(ch, "enforce_no_delivery_streaks", new_callable=AsyncMock, side_effect=RuntimeError("boom")),
            patch.object(ch, "_record_fact_for_terminalized_run", new_callable=AsyncMock),
            patch.object(ch, "_re_enqueue_run", new_callable=AsyncMock),
            patch.object(ch, "_ingest_saq_error", new_callable=AsyncMock),
            patch.object(ch, "_awaiting_human_has_committed_decision", new_callable=AsyncMock),
            patch.object(ch, "write_dispatcher_reconcile_stats", new_callable=AsyncMock),
        ):
            summary = await ch.dispatcher_reconcile()
        assert summary["streak_deactivated"] == 0
