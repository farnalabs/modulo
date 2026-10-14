"""Unit tests for the FAR-1627 worker-shutdown run handoff primitives.

Locks the pieces that do not need a real database:

* the process-local in-flight registry semantics (register / re-register
  replaces / discard / snapshot / clear),
* the ``track_inflight_run`` cancellation contract — a normal or failed exit
  deregisters, ``asyncio.CancelledError`` (SAQ shutdown) KEEPS the entry so
  the shutdown hook can release it,
* the SAQ settings wiring — ``_base_worker_settings`` installs
  ``_shutdown_handoff_hook`` as the worker ``shutdown`` lifecycle callback,
* the error-code registration — ``harness.worker_lost`` is a registered
  canonical code that ``map_legacy_code`` passes through unchanged (never the
  misleading ``harness.dispatch_failed``), while the legacy raw ``worker_lost``
  alias keeps its historical target.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import modulo.core.saq_worker as sw
from modulo.core.pipeline_engine.error_codes import (
    ERROR_CODE_REGISTRY,
    LEGACY_ALIASES,
    ErrorCodeSpec,
    map_legacy_code,
)
from modulo.core.worker_handoff import (
    WORKER_LOST_ERROR_CODE,
    InFlightRunRegistry,
    get_inflight_registry,
    track_inflight_run,
)


def test_registry_register_discard_snapshot() -> None:
    reg = InFlightRunRegistry()
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    org = uuid.uuid4()
    reg.register(run_id=run_a, org_id=org, claim_token="tok-a")
    reg.register(run_id=run_b, org_id=org, claim_token="tok-b")
    assert len(reg) == 2
    snap = reg.snapshot()
    assert {e.run_id for e in snap} == {run_a, run_b}
    assert all(e.org_id == org for e in snap)

    reg.discard(run_a)
    assert len(reg) == 1
    assert reg.snapshot()[0].run_id == run_b
    # Discarding an unknown id is a silent no-op (idempotency).
    reg.discard(run_a)
    assert len(reg) == 1

    reg.clear()
    assert len(reg) == 0
    assert not reg.snapshot()


def test_registry_reregister_replaces_claim_token() -> None:
    """A SAQ retry re-claims with a FRESH token — the latest claim wins.

    The handback is token-fenced, so a stale token in the registry would
    fence the run OUT of its own handback; re-registration (which
    ``track_inflight_run`` does on every claim) must replace the entry.
    """
    reg = InFlightRunRegistry()
    run_id = uuid.uuid4()
    org = uuid.uuid4()
    reg.register(run_id=run_id, org_id=org, claim_token="tok-1")
    reg.register(run_id=run_id, org_id=org, claim_token="tok-2")
    assert len(reg) == 1
    assert reg.snapshot()[0].claim_token == "tok-2"


async def test_track_inflight_run_discards_on_normal_exit() -> None:
    reg = InFlightRunRegistry()
    run_id = uuid.uuid4()
    org = uuid.uuid4()
    async with track_inflight_run(run_id=run_id, org_id=org, claim_token="tok", registry=reg):
        assert len(reg) == 1
    assert len(reg) == 0


async def test_track_inflight_run_discards_on_non_cancel_exception() -> None:
    """A failed job's fate is owned by the after_process hook / terminal writer."""
    reg = InFlightRunRegistry()
    run_id = uuid.uuid4()
    org = uuid.uuid4()
    with pytest.raises(RuntimeError, match="boom"):
        async with track_inflight_run(run_id=run_id, org_id=org, claim_token="tok", registry=reg):
            raise RuntimeError("boom")
    assert len(reg) == 0


async def test_track_inflight_run_keeps_entry_on_cancellation() -> None:
    """THE FAR-1627 contract: SAQ shutdown cancellation must leave the entry.

    ``Worker.stop()`` cancels the in-flight job task, so
    ``asyncio.CancelledError`` tears through ``execute_run`` — the run has
    lost its executor and is exactly what the shutdown hook must release.
    """
    reg = InFlightRunRegistry()
    run_id = uuid.uuid4()
    org = uuid.uuid4()
    with pytest.raises(asyncio.CancelledError):
        async with track_inflight_run(run_id=run_id, org_id=org, claim_token="tok", registry=reg):
            raise asyncio.CancelledError
    assert len(reg) == 1
    entry = reg.snapshot()[0]
    assert entry.run_id == run_id
    assert entry.claim_token == "tok"


def test_process_registry_is_module_wide_singleton() -> None:
    assert get_inflight_registry() is get_inflight_registry()


def test_worker_lost_code_is_registered_and_passes_through() -> None:
    """``harness.worker_lost`` is canonical — never remapped, never unknown.

    The whole truthfulness point of FAR-1627 is that the surfaced code names
    worker loss; if the alias table or the unknown-fallback could rewrite it
    to ``harness.dispatch_failed``, the acceptance criterion fails.
    """
    assert WORKER_LOST_ERROR_CODE in ERROR_CODE_REGISTRY
    assert isinstance(ERROR_CODE_REGISTRY[WORKER_LOST_ERROR_CODE], ErrorCodeSpec)
    assert map_legacy_code(WORKER_LOST_ERROR_CODE) == "harness.worker_lost"
    assert map_legacy_code(WORKER_LOST_ERROR_CODE) != "harness.dispatch_failed"


def test_legacy_raw_worker_lost_alias_is_unchanged() -> None:
    """The historical raw spelling keeps its pre-FAR-1627 alias target.

    The legacy stale-run sweep wrote raw ``worker_lost`` for a different
    class (non-SAQ, 5+ claims); repointing that alias would silently
    reclassify historical rows. The handback writes the DOTTED key instead.
    """
    assert LEGACY_ALIASES["worker_lost"] == "harness.dispatch_failed"


def test_base_worker_settings_installs_shutdown_handoff_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring pin: the SAQ settings dict carries the handoff as ``shutdown``.

    ``Worker(**settings)`` passes it straight through to the lifecycle
    callback SAQ awaits in ``stop()`` after the drain/cancel — so this pin is
    what proves the production worker actually runs the handback on SIGTERM.
    """
    monkeypatch.setattr(
        sw,
        "get_settings",
        lambda: SimpleNamespace(
            saq_redis_pool_size=10,
            saq_worker_concurrency=2,
            redis_url="redis://localhost:6379/0",
        ),
    )
    monkeypatch.setattr(sw, "_build_queue", lambda _name: MagicMock())
    settings = sw._base_worker_settings("runs", [])
    assert settings["shutdown"] is sw._shutdown_handoff_hook
    # The hook is an async function — SAQ awaits lifecycle callbacks.
    assert asyncio.iscoroutinefunction(sw._shutdown_handoff_hook)


async def test_shutdown_handoff_hook_swallows_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail-open with a LOUD log: a handback failure must never fail shutdown.

    An exception escaping the hook would surface as a worker crash instead of
    a logged, backstopped degradation (the heartbeat-TTL sweeps remain the
    safety net).
    """
    import logging

    async def _boom(_engine: object) -> dict[str, int]:
        raise RuntimeError("db down")

    monkeypatch.setattr(sw, "_get_async_engine", lambda: MagicMock())
    monkeypatch.setattr("modulo.core.worker_handoff.handback_inflight_runs", _boom)
    with caplog.at_level(logging.ERROR):
        await sw._shutdown_handoff_hook({})
    assert any("shutdown_handoff_failed" in r.message for r in caplog.records)


async def test_shutdown_handoff_hook_logs_handed_back_count(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    async def _ok(_engine: object) -> dict[str, int]:
        return {"handed_back": 3, "skipped": 1}

    monkeypatch.setattr(sw, "_get_async_engine", lambda: MagicMock())
    monkeypatch.setattr("modulo.core.worker_handoff.handback_inflight_runs", _ok)
    with caplog.at_level(logging.WARNING):
        await sw._shutdown_handoff_hook({})
    assert any("handed_back=3" in r.message for r in caplog.records)
