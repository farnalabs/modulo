"""FAR-1359 — the run-age wedge backstop is independent of the run ceiling.

``cron_helpers``' mid-graph wedge terminalizer used to compute its age window from
the SAQ run timeout, which was a module constant in ``core/dispatch.py`` that
silently disagreed with the ``saq_run_timeout`` setting. Consolidating the
transport ceiling onto one deploy setting (``MODULO_MAX_RUN_SECONDS``) must NOT
drag the wedge window with it: raising the ceiling for a provider that can host
long-running agents must not leave a genuinely wedged run holding its org
concurrency slot for longer. These tests pin that decoupling.
"""

import importlib
import inspect

from modulo.core import cron_helpers
from modulo.settings import get_settings


def test_wedge_window_is_the_historical_135_minutes() -> None:
    """The shipped value is unchanged: max(7200 // 60, 120) + 15 = 135."""
    assert cron_helpers._MID_GRAPH_WEDGE_MAX_AGE_MINUTES == 135


def test_raising_the_run_ceiling_does_not_move_the_wedge_window(monkeypatch) -> None:
    """Re-read the module with a 24h ceiling pinned: the window must not move.

    Reloading is the honest way to prove it — the constant is computed at import
    time, so an in-place patch of the settings object would not exercise the
    import-time path at all.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "b" * 44)
    monkeypatch.setenv("MODULO_MAX_RUN_SECONDS", "86400")
    get_settings.cache_clear()
    try:
        reloaded = importlib.reload(cron_helpers)
        assert reloaded._MID_GRAPH_WEDGE_MAX_AGE_MINUTES == 135
    finally:
        get_settings.cache_clear()
        importlib.reload(cron_helpers)


def test_no_run_age_backstop_in_cron_helpers_reads_the_run_ceiling() -> None:
    """Structural guard: no executable line in the module may reference the
    ceiling, so no run-age backstop in it can move when an operator raises the
    ceiling. The retired ``SAQ_RUN_TIMEOUT`` module constant is asserted absent
    too. Comments are excluded — they legitimately name both symbols to explain
    why the two are decoupled.
    """
    code = "\n".join(line for line in inspect.getsource(cron_helpers).splitlines() if not line.lstrip().startswith("#"))
    assert "modulo_max_run_seconds" not in code
    assert "SAQ_RUN_TIMEOUT" not in code
    assert "_MID_GRAPH_WEDGE_MAX_AGE_MINUTES = 135" in code
