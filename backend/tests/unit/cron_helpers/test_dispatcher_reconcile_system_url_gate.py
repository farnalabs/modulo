"""FAR-1519: ``dispatcher_reconcile`` fails closed BEFORE any heartbeat write.

Establishes the readiness-gating chain's first link for a deployment that does
not wire ``MODULO_SYSTEM_DATABASE_URL`` (the shape the root docker-compose.yml
had before this ticket): ``_open_system_factory()`` -> ``_get_system_engine``
raises ``RuntimeError`` at the TOP of ``dispatcher_reconcile`` — before the
``try`` block that persists the failure heartbeat — so no stats blob is ever
written to the shared Redis key. ``/healthz/ready`` then reports
``dispatcher_reconcile has never run`` as ``unavailable`` (unit-tested in
``tests/unit/api/test_health.py``), and that tier GATES readiness with a 503
(``test_healthz_ready_dispatcher_unavailable_gates``, FAR-199).

The other two links are pinned by existing tests:
* ``_get_system_engine`` raises when the URL is unset —
  ``tests/unit/cron_helpers/test_cron_helpers.py::TestGetSystemEngine``;
* ``unavailable`` dispatcher stats 503 readiness —
  ``tests/unit/api/test_health.py::test_healthz_ready_dispatcher_unavailable_gates``.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import cron_helpers as ch


def _settings() -> MagicMock:
    """Stand-in settings: the attributes ``dispatcher_reconcile`` reads before
    opening the system factory, with the system URL deliberately UNSET."""
    return MagicMock(
        saq_runs_queue="runs",
        saq_reenqueue_window=600,
        saq_job_heartbeat=300,
        saq_claimed_nodeless_minutes=35,
        redis_url="redis://localhost:6379/0",
        saq_redis_pool_size=5,
        hitl_review_cancel_grace_seconds=3600,
        saq_run_claim_cap=20,
        modulo_system_database_url="",
        dispatcher_reconcile_budget_seconds=95,
        dispatcher_reconcile_terminalize_max_per_tick=25,
        dispatcher_reconcile_facts_max_per_tick=25,
    )


def _bind_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point cron_helpers' ``get_settings`` at a callable returning the stub.

    The stub must come back FROM the call (`get_settings()`), not be the
    replaced name itself — binding the mock directly makes every call return
    an auto-mock whose ``modulo_system_database_url`` is a truthy MagicMock,
    which would take the configured-URL branch instead of failing closed.
    """
    settings = _settings()
    monkeypatch.setattr(ch, "get_settings", lambda: settings)


@pytest.mark.asyncio
async def test_dispatcher_reconcile_fails_closed_before_writing_any_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset system URL -> RuntimeError before ANY stats persistence.

    If the raise happened after the heartbeat write, readiness would see a
    fresh-but-failed tick (degraded, non-gating) instead of the truthful
    ``never ran`` (unavailable, gating) — so the ORDER is the assertion: no
    Redis client is built, and the in-process stats dict is untouched.
    """
    monkeypatch.setattr(ch, "_SYSTEM_ENGINE", None)  # reset the engine singleton
    _bind_settings(monkeypatch)
    redis_cls = MagicMock()
    monkeypatch.setattr(ch, "AsyncRedis", redis_cls)
    last_run_before = ch._dispatcher_reconcile_stats.get("last_run_at")

    with pytest.raises(RuntimeError, match="MODULO_SYSTEM_DATABASE_URL is not set"):
        await ch.dispatcher_reconcile()

    assert not redis_cls.from_url.called, "the system-engine raise must fire before any Redis client is built"
    assert ch._dispatcher_reconcile_stats.get("last_run_at") == last_run_before, (
        "no heartbeat (in-process or persisted) may be written when the system URL is unset — "
        "readiness must see 'never ran', not a fabricated last_run_at"
    )


@pytest.mark.asyncio
async def test_dispatcher_reconcile_fail_closed_does_not_cache_a_broken_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed leaves the singleton unset, so the NEXT tick raises again
    (the cron keeps failing loudly every 60s instead of silently proceeding
    with a half-built engine)."""
    monkeypatch.setattr(ch, "_SYSTEM_ENGINE", None)
    _bind_settings(monkeypatch)
    monkeypatch.setattr(ch, "AsyncRedis", MagicMock())

    with pytest.raises(RuntimeError, match="MODULO_SYSTEM_DATABASE_URL is not set"):
        await ch.dispatcher_reconcile()
    assert ch._SYSTEM_ENGINE is None


@pytest.mark.asyncio
async def test_dispatcher_reconcile_delegates_to_the_system_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: with a factory available the tick proceeds (proves the two
    tests above fail for the URL reason, not because the call shape is wrong).
    A failing body then DOES persist the failure heartbeat — the behaviour the
    unset-URL path structurally cannot reach."""
    monkeypatch.setattr(ch, "_SYSTEM_ENGINE", None)
    _bind_settings(monkeypatch)
    factory = MagicMock()
    redis_client = AsyncMock()
    redis_cls = MagicMock()
    redis_cls.from_url.return_value = redis_client
    monkeypatch.setattr(ch, "AsyncRedis", redis_cls)

    with (
        patch.object(ch, "_open_system_factory", return_value=factory),
        patch.object(ch, "_dispatcher_reconcile_body", AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(ch, "set_dispatcher_reconcile_stats") as set_stats,
        pytest.raises(RuntimeError, match="boom"),
    ):
        await ch.dispatcher_reconcile()

    assert set_stats.called
    assert redis_client.set.await_count == 1
