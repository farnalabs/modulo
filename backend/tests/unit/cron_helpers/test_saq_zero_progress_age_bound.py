"""FAR-1715 — the zero-progress age bound is a Settings knob, read at call time.

``saq_zero_progress_max_age_minutes`` (``SAQ_ZERO_PROGRESS_MAX_AGE_MINUTES``)
is the ONE bound shared by dispatcher_reconcile's nodeless router / mid-graph
age gate and the stale_run_recovery sweep's independent SAQ zero-progress
branch. Default behaviour is unchanged: unset (or a settings double that lacks
the field) resolves to the pre-FAR-1715 module-constant derivation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import modulo.core.cron_helpers as ch


class TestSaqZeroProgressMaxAgeReader:
    def test_default_constant_is_the_historical_derivation(self) -> None:
        """The fallback constant stays the pre-FAR-1715 derivation exactly:
        ``max(SAQ_RUN_TIMEOUT // 60, 120) + 15`` = 135 at the shipped 7200s."""
        assert max(ch.SAQ_RUN_TIMEOUT // 60, 120) + 15 == ch._MID_GRAPH_WEDGE_MAX_AGE_MINUTES
        assert ch._MID_GRAPH_WEDGE_MAX_AGE_MINUTES == 135

    def test_reader_falls_back_to_the_constant_for_a_settings_double(self) -> None:
        """A settings double that merely lacks the field (MagicMock) resolves
        to the default-config derivation — the same contract as
        ``_nodeless_in_flight_floor_seconds``."""
        with patch.object(ch, "get_settings", return_value=MagicMock()):
            assert ch._saq_zero_progress_max_age_minutes() == ch._MID_GRAPH_WEDGE_MAX_AGE_MINUTES

    def test_reader_falls_back_to_the_constant_when_the_knob_is_none(self) -> None:
        """An unset (None) knob — the shipped default — keeps the historical
        bound: behaviour-neutral at the default."""
        settings = MagicMock()
        settings.saq_zero_progress_max_age_minutes = None
        with patch.object(ch, "get_settings", return_value=settings):
            assert ch._saq_zero_progress_max_age_minutes() == 135

    def test_reader_reads_an_explicit_pin(self) -> None:
        """An explicit knob value is returned verbatim — the operator's pin
        drives BOTH the reconcile paths and the stale_run_recovery branch."""
        settings = MagicMock()
        settings.saq_zero_progress_max_age_minutes = 200
        with patch.object(ch, "get_settings", return_value=settings):
            assert ch._saq_zero_progress_max_age_minutes() == 200

    def test_reader_uses_an_already_resolved_settings_object(self) -> None:
        """Callers holding their own Settings (e.g. stale_run_recovery_sweep)
        pass it in — the knob is read from THAT object, never a second
        ``get_settings()`` entry."""
        settings = MagicMock()
        settings.saq_zero_progress_max_age_minutes = 180
        # get_settings would explode if re-entered — it must NOT be called.
        with patch.object(ch, "get_settings", side_effect=AssertionError("re-entered get_settings")):
            assert ch._saq_zero_progress_max_age_minutes(settings) == 180

    def test_reader_result_is_an_int(self) -> None:
        """The bound travels into SQL bind params — it must never be a
        MagicMock stand-in."""
        with patch.object(ch, "get_settings", return_value=MagicMock()):
            assert isinstance(ch._saq_zero_progress_max_age_minutes(), int)
