"""Unit tests for the FAR-594 D8 runner capacity gate (core/runner_capacity.py)."""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.connector_hub.locking import _uuid_to_lock_keys
from modulo.core.runner_capacity import (
    HOST_RESOURCE_PROVIDERS,
    MARKER_STATE_CLEARED_AT_HITL,
    MARKER_STATE_SCRIPT_EXECUTING,
    RUNNER_PROVIDER_DOCKER,
    RUNNER_PROVIDER_E2B,
    RunnerCapacityDecision,
    RunnerCapacityDeniedError,
    RunnerMarkerSweepError,
    acquire_runner_dispatch_slot,
    build_dispatch_marker,
    build_hitl_tombstone,
    classify_sweep_action,
    mark_runner_dispatch_cleared_at_hitl,
    marker_is_fence_component,
    parse_marker_provider,
    parse_marker_state,
    parse_marker_written_at,
    reconcile_runner_dispatch_markers,
    runner_capacity_lock_keys,
    runner_marker_sweep_lock_keys,
    sweep_staleness_reference,
)

_ORG = uuid.uuid4()
_RUN = str(uuid.uuid4())
_CLAIM = "claim-token-abc"


class _FakeGateSettings:
    runner_capacity_gate_enabled = False
    runner_capacity_lock_timeout_ms = 2000


def _fake_session(factory_results: dict[str, Any] | None = None) -> Any:
    """Async session double: ``factory() -> session`` with ``begin()`` context.

    ``factory_results`` maps a statement substring to the ``fetchone()`` tuple;
    unmatched statements answer a MagicMock (truthy fetchone).
    """
    session = MagicMock()
    results = factory_results or {}

    async def _execute(stmt: Any, params: Any = None) -> Any:
        text_str = str(stmt)
        result = MagicMock()
        # Defaults first; statement-specific answers override below.
        result.fetchone.return_value = MagicMock()
        result.rowcount = 1
        for key, value in results.items():
            if key in text_str:
                result.fetchone.return_value = value
                break
        return result

    session.execute = AsyncMock(side_effect=_execute)

    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session_cm)
    factory.session = session
    return factory


def _patch_gate(monkeypatch: pytest.MonkeyPatch, *, flag_on: bool = False) -> None:
    class _S:
        runner_capacity_gate_enabled = flag_on
        runner_capacity_lock_timeout_ms = 2000

    import modulo.core.runner_capacity as rc

    monkeypatch.setattr(rc, "get_settings", lambda: _S())
    monkeypatch.setattr("modulo.db.rls.set_rls_org", AsyncMock())
    monkeypatch.setattr("modulo.db.rls.set_rls_execution_context", AsyncMock())


def _patch_gate_structural(monkeypatch: pytest.MonkeyPatch, executed: list[tuple[str, Any]], *, flag_on: bool) -> None:
    """Like :func:`_patch_gate` but the RLS stubs RECORD their statements
    (as ``(text, params)`` tuples) so the structural test can assert the RLS
    set_config class is present."""
    import modulo.core.runner_capacity as rc

    class _S:
        runner_capacity_gate_enabled = flag_on
        runner_capacity_lock_timeout_ms = 2000

    monkeypatch.setattr(rc, "get_settings", lambda: _S())

    async def _record_set_org(_session: Any, _org: uuid.UUID) -> None:
        executed.append(("SELECT set_config('app.organisation_id', :val, true)", None))

    async def _record_set_ctx(_session: Any) -> None:
        executed.append(("SELECT set_config('app.execution_context', 'true', true)", None))

    monkeypatch.setattr("modulo.db.rls.set_rls_org", _record_set_org)
    monkeypatch.setattr("modulo.db.rls.set_rls_execution_context", _record_set_ctx)


# ---------------------------------------------------------------------------
# Lock namespace derivation
# ---------------------------------------------------------------------------


def test_lock_keys_stable_per_org() -> None:
    assert runner_capacity_lock_keys(_ORG) == runner_capacity_lock_keys(_ORG)
    other = uuid.uuid4()
    assert runner_capacity_lock_keys(_ORG) != runner_capacity_lock_keys(other)


def test_lock_keys_never_share_legacy_keyspace() -> None:
    """The D8 reserved-prefix derivation must NEVER collide with the shared
    ``_uuid_to_lock_keys`` keyspace the raw org id hashes into (the legacy
    keyspace is shared with connector/trigger/system-config locks)."""
    legacy = _uuid_to_lock_keys(_ORG)
    d8 = runner_capacity_lock_keys(_ORG)
    assert legacy != d8
    # And for a large sample of org ids: no derivation ever coincides.
    for _ in range(200):
        org = uuid.uuid4()
        assert _uuid_to_lock_keys(org) != runner_capacity_lock_keys(org)


def test_sweep_dedup_keys_distinct_from_per_org_keys() -> None:
    """The sweep dedup lock uses a DISTINCT derivation suffix — it can never
    contend with (or deadlock against) a per-org gate key."""
    assert runner_marker_sweep_lock_keys() != runner_capacity_lock_keys(_ORG)
    for _ in range(200):
        assert runner_marker_sweep_lock_keys() != runner_capacity_lock_keys(uuid.uuid4())


# ---------------------------------------------------------------------------
# Marker vocabulary
# ---------------------------------------------------------------------------


def test_build_dispatch_marker_tierless_is_legacy_compatible() -> None:
    marker = build_dispatch_marker("run:1:node:a:2")
    parsed = json.loads(marker)
    assert parsed == {"state": "dispatching", "attempt_key": "run:1:node:a:2"}


def test_build_dispatch_marker_carries_provider_and_written_at() -> None:
    marker = build_dispatch_marker("k", RUNNER_PROVIDER_E2B)
    parsed = json.loads(marker)
    assert parsed["state"] == "dispatching"
    assert parsed["attempt_key"] == "k"
    assert parsed["provider"] == "e2b"
    assert parse_marker_written_at(marker) is not None


def test_tombstone_is_capacity_neutral_state() -> None:
    tombstone = build_hitl_tombstone()
    assert parse_marker_state(tombstone) == MARKER_STATE_CLEARED_AT_HITL
    assert not marker_is_fence_component(tombstone)


def test_fence_detection_only_for_script_executing() -> None:
    assert marker_is_fence_component(json.dumps({"state": MARKER_STATE_SCRIPT_EXECUTING, "attempt_key": "k"}))
    assert not marker_is_fence_component(build_dispatch_marker("k"))
    assert not marker_is_fence_component("dispatching")  # legacy bare literal
    assert not marker_is_fence_component(None)


def test_parse_helpers_never_raise_on_garbage() -> None:
    for garbage in (None, "", "dispatching", "{not json", "[1, 2]", "123"):
        assert parse_marker_state(garbage) is None
        assert parse_marker_provider(garbage) is None
        assert parse_marker_written_at(garbage) is None


# ---------------------------------------------------------------------------
# Capacity contract (flag matrix)
# ---------------------------------------------------------------------------


async def test_contract_flag_off_absent_key_means_no_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag-off + absent key = NO gate (enforced_cap semantics) — the D3b
    flag-window contract; the Docker-tier default activates ONLY with the
    gate flag."""
    _patch_gate(monkeypatch, flag_on=False)

    from modulo.db.crud.run import SandboxConcurrencyLimit

    async def _fake_limit(_session: Any, _org: uuid.UUID) -> SandboxConcurrencyLimit:
        return SandboxConcurrencyLimit(cap=4, is_default=True)

    monkeypatch.setattr("modulo.db.crud.run.get_sandbox_concurrency_limit", _fake_limit)
    from modulo.core.runner_capacity import read_runner_cap_contract

    cap, host_only = await read_runner_cap_contract(MagicMock(), _ORG)
    assert cap is None
    assert host_only is False


async def test_contract_flag_on_absent_key_activates_docker_tier_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, flag_on=True)

    from modulo.db.crud.run import SandboxConcurrencyLimit

    async def _fake_limit(_session: Any, _org: uuid.UUID) -> SandboxConcurrencyLimit:
        return SandboxConcurrencyLimit(cap=4, is_default=True)

    monkeypatch.setattr("modulo.db.crud.run.get_sandbox_concurrency_limit", _fake_limit)
    from modulo.core.runner_capacity import read_runner_cap_contract

    cap, host_only = await read_runner_cap_contract(MagicMock(), _ORG)
    assert cap == 4
    assert host_only is True
    assert {RUNNER_PROVIDER_DOCKER, "local"} == HOST_RESOURCE_PROVIDERS


async def test_contract_flag_on_explicit_value_gates_all_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, flag_on=True)

    from modulo.db.crud.run import SandboxConcurrencyLimit

    async def _fake_limit(_session: Any, _org: uuid.UUID) -> SandboxConcurrencyLimit:
        return SandboxConcurrencyLimit(cap=2, is_default=False)

    monkeypatch.setattr("modulo.db.crud.run.get_sandbox_concurrency_limit", _fake_limit)
    from modulo.core.runner_capacity import read_runner_cap_contract

    cap, host_only = await read_runner_cap_contract(MagicMock(), _ORG)
    assert cap == 2
    assert host_only is False


async def test_contract_flag_on_explicit_null_means_no_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, flag_on=True)

    from modulo.db.crud.run import SandboxConcurrencyLimit

    async def _fake_limit(_session: Any, _org: uuid.UUID) -> SandboxConcurrencyLimit:
        return SandboxConcurrencyLimit(cap=None, is_default=False)

    monkeypatch.setattr("modulo.db.crud.run.get_sandbox_concurrency_limit", _fake_limit)
    from modulo.core.runner_capacity import read_runner_cap_contract

    cap, _host_only = await read_runner_cap_contract(MagicMock(), _ORG)
    assert cap is None


# ---------------------------------------------------------------------------
# The gate — statement-class structural test (SQLAlchemy event-free fake)
# ---------------------------------------------------------------------------


async def test_gate_flag_on_statement_class_set_and_ordering(monkeypatch: pytest.MonkeyPatch) -> None:
    """Structural test (D8): the gate's statement CLASS set is exactly
    {RLS set_config, lock_timeout, own-row conditional fenced SELECT FOR
    UPDATE, per-org advisory lock, lock-free count SELECT, own-row conditional
    fenced marker UPDATE} — in the uniform row→advisory ordering — and every
    write/lock statement targets the gate's OWN run only."""
    statements: list[tuple[str, Any]] = []
    _patch_gate_structural(monkeypatch, statements, flag_on=True)
    session = MagicMock()

    async def _execute(stmt: Any, params: Any = None) -> Any:
        text_str = str(stmt)
        statements.append((text_str, params))
        result = MagicMock()
        if "claim_count" in text_str:
            result.fetchone.return_value = (7,)
        elif "pg_advisory_xact_lock" in text_str:
            result.fetchone.return_value = (True,)
        elif "set_config" in text_str:
            result.fetchone.return_value = None
        elif "UPDATE runs" in text_str:
            result.fetchone.return_value = (uuid.uuid4(),)
        else:
            # the count SELECT (via crud) — scalar_one path is on the result
            result.fetchone.return_value = None
            result.scalar_one.return_value = 0
        return result

    session.execute = AsyncMock(side_effect=_execute)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session_cm)

    async def _fake_limit(_session: Any, _org: uuid.UUID) -> Any:
        from modulo.db.crud.run import SandboxConcurrencyLimit

        return SandboxConcurrencyLimit(cap=2, is_default=False)

    monkeypatch.setattr("modulo.db.crud.run.get_sandbox_concurrency_limit", _fake_limit)

    from modulo.core.runner_capacity import resolve_runner_capacity_decision

    async def _fake_decision(_session: Any, _org: uuid.UUID, **_kw: Any) -> Any:
        from modulo.core.runner_capacity import RunnerCapacityDecision

        return RunnerCapacityDecision(cap=2, active=1, host_resource_only=False)

    monkeypatch.setattr(
        "modulo.core.runner_capacity.resolve_runner_capacity_decision",
        _fake_decision,
    )
    # resolve_runner_capacity_decision is called via the module-global name
    # inside acquire_runner_dispatch_slot; patch both spellings defensively.
    monkeypatch.setattr(
        "modulo.core.runner_capacity.count_active_runner_dispatches_for_decision",
        AsyncMock(return_value=1),
    )
    _ = resolve_runner_capacity_decision

    slot = await acquire_runner_dispatch_slot(
        factory, org_id=_ORG, run_id=_RUN, claim_token=_CLAIM, node_id="n1", provider="e2b"
    )
    assert slot.status == "acquired"
    assert slot.attempt_key == f"run:{_RUN}:node:n1:7"
    assert slot.marker_set is True

    texts = [text for text, _params in statements]
    joined = "\n".join(texts)
    # Statement CLASS set (flag-on):
    assert "set_config('app.organisation_id'" in joined  # RLS
    assert "set_config('app.execution_context'" in joined  # RLS
    assert "set_config('lock_timeout'" in joined  # degradation knob
    assert "FOR UPDATE" in joined  # own-row lock
    assert "claim_count" in joined  # attempt key rides the fenced SELECT
    assert "pg_advisory_xact_lock" in joined  # per-org advisory
    assert "UPDATE runs SET sandbox_dispatch_state" in joined  # fenced marker
    # Row-then-advisory ordering: the own-row SELECT precedes the advisory lock.
    own_row_idx = next(i for i, s in enumerate(texts) if "FOR UPDATE" in s)
    advisory_idx = next(i for i, s in enumerate(texts) if "pg_advisory_xact_lock" in s)
    assert own_row_idx < advisory_idx
    # No foreign-row writes/locks: every runs WRITE/LOCK statement carries the
    # gate's OWN run id in its bound params (the count SELECT is read-only —
    # reads of foreign rows are fine, writes/locks are not).
    for stmt_text, params in statements:
        if "UPDATE runs" in stmt_text or "FOR UPDATE" in stmt_text:
            assert params is not None, "write/lock statements must bind their params"
            assert str(params.get("rid")) == str(_RUN)


async def test_gate_denies_at_capacity_with_marker_rolled_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, flag_on=True)
    executed: list[str] = []
    session = MagicMock()

    async def _execute(stmt: Any, params: Any = None) -> Any:
        text_str = str(stmt)
        executed.append(text_str)
        result = MagicMock()
        if "claim_count" in text_str:
            result.fetchone.return_value = (7,)
        elif "pg_advisory_xact_lock" in text_str:
            result.fetchone.return_value = (True,)
        else:
            result.fetchone.return_value = None
            result.scalar_one.return_value = 9
        return result

    session.execute = AsyncMock(side_effect=_execute)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session_cm)

    async def _fake_decision(_session: Any, _org: uuid.UUID, **_kw: Any) -> Any:
        from modulo.core.runner_capacity import RunnerCapacityDecision

        return RunnerCapacityDecision(cap=4, active=9, host_resource_only=True)

    monkeypatch.setattr("modulo.core.runner_capacity.resolve_runner_capacity_decision", _fake_decision)

    with pytest.raises(RunnerCapacityDeniedError, match="at capacity"):
        await acquire_runner_dispatch_slot(factory, org_id=_ORG, run_id=_RUN, claim_token=_CLAIM, node_id="n1")

    # The denied transaction never reached the marker UPDATE (rolled back).
    assert not any("UPDATE runs SET sandbox_dispatch_state" in s for s in executed)


async def test_gate_fenced_when_claim_superseded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, flag_on=False)
    factory = _fake_session({"claim_count": None})  # own-row SELECT answers no row
    slot = await acquire_runner_dispatch_slot(factory, org_id=_ORG, run_id=_RUN, claim_token=_CLAIM, node_id="n1")
    assert slot.status == "fenced"
    assert slot.attempt_key is None
    assert slot.marker_set is False


async def test_gate_fail_open_still_writes_marker_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate failure policy: a DB error fails OPEN, and the dispatch marker
    is STILL written best-effort in its OWN transaction — a fail-open dispatch
    must never go markerless."""
    _patch_gate(monkeypatch, flag_on=False)

    executed: list[str] = []
    session = MagicMock()

    async def _execute(stmt: Any, params: Any = None) -> Any:
        text_str = str(stmt)
        executed.append(text_str)
        result = MagicMock()
        result.fetchone.return_value = MagicMock()
        result.rowcount = 1
        if "claim_count" in text_str:
            result.fetchone.return_value = (5,)
        elif "get_sandbox" in text_str or "organisations" in text_str:
            raise RuntimeError("db down mid-gate")
        return result

    session.execute = AsyncMock(side_effect=_execute)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session_cm)

    async def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr("modulo.db.crud.run.get_sandbox_concurrency_limit", _boom)

    from modulo.core.pipeline_engine.node_runner import _sandbox_acquire_dispatch_marker

    key = await _sandbox_acquire_dispatch_marker(
        session_factory=factory,
        claim_lease=_CLAIM,
        org_id=str(_ORG),
        run_id=_RUN,
        node_id="n1",
        provider="e2b",
    )
    assert key == f"run:{_RUN}:node:n1:5"
    # The best-effort write happened in its own transaction (a separate
    # session acquire) and carried the marker.
    assert any("UPDATE runs SET sandbox_dispatch_state" in s for s in executed)


async def test_gate_lock_timeout_degrades_to_retryable_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLSTATE 55P03 (lock_not_available — what SET LOCAL lock_timeout
    actually raises) → retryable RunnerCapacityDeniedError + the distinct
    ``runner.capacity.lock_degraded`` event."""
    _patch_gate(monkeypatch, flag_on=True)

    from sqlalchemy.exc import DBAPIError

    inner = Exception("lock timeout")
    inner.sqlstate = "55P03"  # type: ignore[attr-defined]
    session = MagicMock()

    async def _execute(stmt: Any, params: Any = None) -> Any:
        if "claim_count" in str(stmt):
            result = MagicMock()
            result.fetchone.return_value = (1,)
            return result
        if "pg_advisory_xact_lock" in str(stmt):
            raise DBAPIError("stmt", {}, inner)
        result = MagicMock()
        result.fetchone.return_value = None
        return result

    session.execute = AsyncMock(side_effect=_execute)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session_cm)

    with caplog_at_level_warning() as caplog_ctx, pytest.raises(RunnerCapacityDeniedError):
        await acquire_runner_dispatch_slot(factory, org_id=_ORG, run_id=_RUN, claim_token=_CLAIM, node_id="n1")
    assert any("runner.capacity.lock_degraded" in m for m in caplog_ctx.messages)


async def test_gate_deadlock_is_distinct_alarm(monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLSTATE 40P01 (deadlock — expected impossible under the uniform
    ordering) is a SEPARATE ``runner.capacity.deadlock_degraded`` alarm."""
    _patch_gate(monkeypatch, flag_on=True)

    from sqlalchemy.exc import DBAPIError

    inner = Exception("deadlock detected")
    inner.sqlstate = "40P01"  # type: ignore[attr-defined]
    session = MagicMock()

    async def _execute(stmt: Any, params: Any = None) -> Any:
        if "claim_count" in str(stmt):
            result = MagicMock()
            result.fetchone.return_value = (1,)
            return result
        if "pg_advisory_xact_lock" in str(stmt):
            raise DBAPIError("stmt", {}, inner)
        result = MagicMock()
        result.fetchone.return_value = None
        return result

    session.execute = AsyncMock(side_effect=_execute)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session_cm)

    with caplog_at_level_error() as caplog_ctx, pytest.raises(RunnerCapacityDeniedError):
        await acquire_runner_dispatch_slot(factory, org_id=_ORG, run_id=_RUN, claim_token=_CLAIM, node_id="n1")
    assert any("runner.capacity.deadlock_degraded" in m for m in caplog_ctx.messages)


class _CaplogCtx:
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages


def caplog_at_level_warning() -> Any:
    """Minimal caplog shim: capture warning-level records for the gate logger."""
    messages: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _Handler(level=logging.WARNING)
    logging.getLogger("modulo.core.runner_capacity").addHandler(handler)
    logging.getLogger("modulo.core.runner_capacity").setLevel(logging.WARNING)

    class _CM:
        def __enter__(self) -> _CaplogCtx:
            return _CaplogCtx(messages)

        def __exit__(self, *_a: object) -> None:
            logging.getLogger("modulo.core.runner_capacity").removeHandler(handler)

    return _CM()


def caplog_at_level_error() -> Any:
    messages: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _Handler(level=logging.ERROR)
    logging.getLogger("modulo.core.runner_capacity").addHandler(handler)
    logging.getLogger("modulo.core.runner_capacity").setLevel(logging.ERROR)

    class _CM:
        def __enter__(self) -> _CaplogCtx:
            return _CaplogCtx(messages)

        def __exit__(self, *_a: object) -> None:
            logging.getLogger("modulo.core.runner_capacity").removeHandler(handler)

    return _CM()


async def test_gate_missing_claim_context_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, flag_on=True)
    slot = await acquire_runner_dispatch_slot(MagicMock(), org_id=_ORG, run_id=_RUN, claim_token=None, node_id="n1")
    assert slot.status == "fail_open"


# ---------------------------------------------------------------------------
# HITL tombstone
# ---------------------------------------------------------------------------


async def test_hitl_tombstone_fenced_and_only_when_marker_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gate(monkeypatch, flag_on=True)
    executed: list[tuple[str, Any]] = []
    session = MagicMock()

    async def _execute(stmt: Any, params: Any = None) -> Any:
        text_str = str(stmt)
        executed.append((text_str, params))
        result = MagicMock()
        result.fetchone.return_value = (uuid.uuid4(),) if "UPDATE runs" in text_str else None
        return result

    session.execute = AsyncMock(side_effect=_execute)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session_cm)

    tombstoned = await mark_runner_dispatch_cleared_at_hitl(factory, org_id=_ORG, run_id=_RUN, claim_token=_CLAIM)
    assert tombstoned is True
    joined = "\n".join(text for text, _params in executed)
    marker_params = [params for text, params in executed if "UPDATE runs" in text and params]
    assert marker_params, "the tombstone UPDATE must carry its params"
    assert "cleared_at_hitl" in json.dumps(marker_params[0])
    assert "sandbox_dispatch_state IS NOT NULL" in joined  # never resurrects
    assert "claim_token" in joined  # claim-token-fenced
    # F1: the exactly-once fence is NEVER tombstoned — the WHERE excludes a
    # live script_executing component (mirrors marker_is_fence_component).
    assert 'NOT LIKE \'%"state": "script_executing"%\'' in joined


async def test_hitl_tombstone_never_overwrites_live_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    """F1: a run with a LIVE script_executing fence + interrupt → the fence
    SURVIVES; no tombstone is written (the UPDATE matches 0 rows because the
    WHERE excludes the fence)."""
    _patch_gate(monkeypatch, flag_on=True)
    session = MagicMock()

    async def _execute(stmt: Any, params: Any = None) -> Any:
        result = MagicMock()
        # Simulate Postgres: the fenced UPDATE's WHERE excludes the fence
        # component → 0 rows matched → nothing written.
        result.fetchone.return_value = None
        return result

    session.execute = AsyncMock(side_effect=_execute)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session_cm)

    tombstoned = await mark_runner_dispatch_cleared_at_hitl(factory, org_id=_ORG, run_id=_RUN, claim_token=_CLAIM)
    assert tombstoned is False, "a live script_executing fence must never be tombstoned"


async def test_hitl_tombstone_flag_off_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """F6 flag-off contract: the tombstone vocabulary is D8-only — with the
    rollout flag OFF the interrupt path writes NOTHING (the pre-D8 marker
    simply survives the park)."""
    _patch_gate(monkeypatch, flag_on=False)
    factory = _fake_session()
    executed: list[str] = []

    session = factory.return_value.__aenter__.return_value

    async def _execute(stmt: Any, params: Any = None) -> Any:
        executed.append(str(stmt))
        result = MagicMock()
        result.fetchone.return_value = (uuid.uuid4(),)
        return result

    session.execute = AsyncMock(side_effect=_execute)

    tombstoned = await mark_runner_dispatch_cleared_at_hitl(factory, org_id=_ORG, run_id=_RUN, claim_token=_CLAIM)
    assert tombstoned is False
    assert not any("UPDATE runs" in s for s in executed), "flag-off must not write the tombstone"


# ---------------------------------------------------------------------------
# Sweep decision (pure) — the D8 rule set (a)-(e)
# ---------------------------------------------------------------------------


_NOW = datetime.now(UTC)


def _sweep_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "complete",
        "error_code": None,
        "marker_json": build_dispatch_marker("k"),
        "stale_reference": _NOW - timedelta(hours=30),
        "now": _NOW,
        "stale_seconds": 25 * 3600,
        "recoverable": False,
        "row_fresh": False,
    }
    base.update(overrides)
    return base


def test_sweep_a_fence_precedence_over_staleness() -> None:
    """Rule (a): a script_executing fence component on a NON-terminal run is
    NEVER cleared — even when far staler than the threshold (precedence over
    the staleness rule)."""
    action = classify_sweep_action(
        **_sweep_kwargs(
            status="running",
            marker_json=json.dumps({"state": MARKER_STATE_SCRIPT_EXECUTING, "attempt_key": "k"}),
            stale_reference=_NOW - timedelta(days=30),
        )
    )
    assert action == "keep_fence"


def test_sweep_b_recoverable_beats_staleness() -> None:
    action = classify_sweep_action(
        **_sweep_kwargs(
            status="running",
            stale_reference=_NOW - timedelta(days=30),
            recoverable=True,
        )
    )
    assert action == "keep_recoverable"


def test_sweep_c_terminal_anomaly_codes_exempt() -> None:
    """Rule (c): terminal runs carrying the rollback detector's anomaly error
    codes KEEP their markers."""
    from modulo.core.rollback_thresholds import SCRIPT_ANOMALY_ERROR_CODES

    for code in sorted(SCRIPT_ANOMALY_ERROR_CODES):
        action = classify_sweep_action(
            **_sweep_kwargs(status="failed", error_code=code, stale_reference=_NOW - timedelta(days=30))
        )
        assert action == "keep_anomaly", code


def test_sweep_c_anomaly_outranks_the_fence_on_terminal_rows() -> None:
    """qa F14: on TERMINAL rows the anomaly exemption is applied BEFORE the
    fence rule — a crash-leaked fence on an anomaly-code run keeps its marker
    (the rollback evaluator owns it)."""
    fence = json.dumps({"state": MARKER_STATE_SCRIPT_EXECUTING, "attempt_key": "k"})
    action = classify_sweep_action(
        **_sweep_kwargs(
            status="failed",
            error_code="script.budget_killed",
            marker_json=fence,
            stale_reference=_NOW - timedelta(days=30),
        )
    )
    assert action == "keep_anomaly"


def test_sweep_d_terminal_crash_leaked_fence_clears_past_staleness() -> None:
    """qa F14: a crash-leaked fence on a terminal NON-anomaly run is cleared
    past the (longer) staleness cap — previously it was pinned forever by the
    fence rule."""
    fence = json.dumps({"state": MARKER_STATE_SCRIPT_EXECUTING, "attempt_key": "k"})
    assert classify_sweep_action(**_sweep_kwargs(status="complete", marker_json=fence)) == "clear_terminal"
    # A just-terminalised run (marker fresh) keeps it — mid-teardown.
    assert (
        classify_sweep_action(
            **_sweep_kwargs(status="complete", marker_json=fence, stale_reference=_NOW - timedelta(hours=2))
        )
        == "keep_live"
    )


def test_sweep_d_terminal_non_fence_cleared() -> None:
    action = classify_sweep_action(**_sweep_kwargs(status="complete"))
    assert action == "clear_terminal"


def test_sweep_d_stale_running_transitions_terminal() -> None:
    action = classify_sweep_action(**_sweep_kwargs(status="running"))
    assert action == "transition_stale_running"


def test_sweep_d_fresh_row_never_terminalised() -> None:
    """qa F14: a freshly-resumed long-parked run (old tombstone/previous-
    attempt marker but a LIVE updated_at/heartbeat) is never terminalised by
    the sweep — only its stale marker is cleared."""
    action = classify_sweep_action(**_sweep_kwargs(status="running", row_fresh=True))
    assert action == "clear_stale"


def test_sweep_d_stale_awaiting_human_cleared_not_terminalised() -> None:
    """A teardown-failure leak parked in awaiting_human: the stale non-fence
    marker is cleared; the run keeps its lifecycle (only RUNNING rows
    transition terminal — a parked run is not killed over a stale marker)."""
    action = classify_sweep_action(**_sweep_kwargs(status="awaiting_human"))
    assert action == "clear_stale"


def test_sweep_d_unknown_status_is_non_terminal() -> None:
    """qa F14: an ``unknown``-status run (FAR-410 recovery status) is
    NON-terminal — its stale marker is cleared but the run is never
    terminalised by the sweep (the operator re-runs it)."""
    action = classify_sweep_action(**_sweep_kwargs(status="unknown"))
    assert action == "clear_stale"


def test_sweep_e_live_long_running_marker_kept() -> None:
    """Negative test: a LIVE long-running run's (fresh) marker is NOT cleared."""
    action = classify_sweep_action(**_sweep_kwargs(status="running", stale_reference=_NOW - timedelta(hours=2)))
    assert action == "keep_live"


def test_sweep_staleness_reference_falls_back_to_updated_at() -> None:
    legacy_marker = json.dumps({"state": "dispatching", "attempt_key": "k"})  # tier-less, no written_at
    updated = _NOW - timedelta(hours=30)
    assert sweep_staleness_reference(legacy_marker, updated) == updated
    assert sweep_staleness_reference(None, updated) == updated
    fresh_marker = build_dispatch_marker("k", "e2b")
    assert sweep_staleness_reference(fresh_marker, updated) is not None
    assert sweep_staleness_reference(fresh_marker, updated) != updated


# ---------------------------------------------------------------------------
# Sweep orchestration (fake factory)
# ---------------------------------------------------------------------------


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.id = kw.get("id", uuid.uuid4())
        self.status = kw.get("status", "running")
        self.error_code = kw.get("error_code")
        self.sandbox_dispatch_state = kw.get("sandbox_dispatch_state")
        self.updated_at = kw.get("updated_at", _NOW - timedelta(hours=30))


class _FakeSweepFactory:
    """Minimal factory double for the sweep: call 1 returns the DEDUP-lock
    session (a ``connection()`` that answers the session-scoped
    ``pg_try_advisory_lock``), call 2 the org-index session, later calls the
    per-org passes (cursor-paged candidate rows + recorded writes)."""

    def __init__(self, rows: list[_Row]) -> None:
        self.rows = rows
        self.cleared: list[uuid.UUID] = []
        self.transitioned: list[uuid.UUID] = []
        self.calls = 0

    def _lock_session(self) -> MagicMock:
        """The DEDUP-lock session — the sweep uses it DIRECTLY (no ``async
        with``): ``connection()`` + ``close()`` on what ``factory()`` returned."""
        conn = MagicMock()

        async def _conn_execute(stmt: Any, params: Any = None) -> Any:
            result = MagicMock()
            result.scalar_one.return_value = True
            return result

        conn.execute = AsyncMock(side_effect=_conn_execute)
        conn.commit = AsyncMock()
        session = MagicMock()
        session.connection = AsyncMock(return_value=conn)
        session.close = AsyncMock()
        return session

    def _wrap(self, session: MagicMock) -> MagicMock:
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=session)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=False)
        session_cm.close = AsyncMock()
        return session_cm

    def _org_session(self) -> MagicMock:
        session = MagicMock()
        outer = self

        async def _execute(stmt: Any, params: Any = None) -> Any:
            text_str = str(stmt)
            result = MagicMock()
            if "FROM organisations" in text_str:
                result.all.return_value = [(uuid.uuid4(),)]
            elif "SELECT id, status, error_code" in text_str:
                after = (params or {}).get("after")
                result.all.return_value = [row for row in outer.rows if after is None or row.id > after]
            elif "count" in text_str.lower():
                # The recoverability count + the live-capacity count.
                result.scalar_one.return_value = 0
            elif "status = 'failed'" in text_str:
                outer.transitioned.append(params["rid"])
                result.fetchone.return_value = (params["rid"],)
            elif "sandbox_dispatch_state = NULL" in text_str:
                outer.cleared.append(params["rid"])
                result.fetchone.return_value = (params["rid"],)
            else:
                result.all.return_value = []
                result.fetchone.return_value = None
                result.scalar_one.return_value = 0
            return result

        session.execute = AsyncMock(side_effect=_execute)
        session.close = AsyncMock()
        return self._wrap(session)

    def __call__(self) -> Any:
        self.calls += 1
        # Each sweep invocation follows the same session sequence:
        # dedup-lock → org-index → one org pass (single-org fakes).
        position = (self.calls - 1) % 3
        if position == 0:
            return self._lock_session()
        if position == 1:
            return self._org_session()
        return self._org_session()


async def test_sweep_clears_terminal_and_stale_markers_and_transitions_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_gate(monkeypatch, flag_on=False)

    class _S(_FakeGateSettings):
        runner_marker_stale_seconds = 25 * 3600
        saq_job_heartbeat = 30
        saq_reenqueue_window = 600
        saq_claimed_nodeless_minutes = 20

    import modulo.core.runner_capacity as rc

    monkeypatch.setattr(rc, "get_settings", lambda: _S())
    # The recoverability predicate build (cron_helpers import) is stubbed out —
    # the sweep's DECISION rules are under test here; the predicate wiring is
    # asserted separately.
    monkeypatch.setattr(rc, "_sweep_recoverability_predicate", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(rc, "_run_recoverable", AsyncMock(return_value=False))

    terminal = _Row(status="complete")
    stale_running = _Row(status="running")
    stale_awaiting = _Row(status="awaiting_human")
    fence = _Row(
        status="running",
        sandbox_dispatch_state=json.dumps({"state": MARKER_STATE_SCRIPT_EXECUTING, "attempt_key": "k"}),
    )
    anomaly = _Row(status="failed", error_code="script.side_effect_unknown")
    factory = _FakeSweepFactory([terminal, stale_running, stale_awaiting, fence, anomaly])

    result = await reconcile_runner_dispatch_markers(factory)  # type: ignore[arg-type]
    assert result["scanned"] == 5
    assert result["cleared"] == 3  # 2 marker clears + 1 stale-running transition clear
    assert set(factory.cleared) == {terminal.id, stale_awaiting.id}
    assert result["transitioned"] == 1
    assert set(factory.transitioned) == {stale_running.id}
    assert result["violations"] == 0  # cap-less org: activity is never a breach (qa F4)
    assert result["orgs_failed"] == 0
    # The fence component and the anomaly-code terminal run survive.
    assert fence.id not in factory.cleared
    assert anomaly.id not in factory.cleared
    assert fence.id not in factory.transitioned


async def test_sweep_cas_guards_against_concurrent_fresh_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2 CAS: the sweep's clear/transition UPDATEs re-check the EXACT marker
    text they classified — a concurrent fresh-marker commit (the marker no
    longer equals ``marker_seen``) makes the UPDATE match 0 rows and counts
    nothing."""
    _patch_gate(monkeypatch, flag_on=False)

    class _S(_FakeGateSettings):
        runner_marker_stale_seconds = 25 * 3600
        saq_job_heartbeat = 30
        saq_reenqueue_window = 600
        saq_claimed_nodeless_minutes = 20

    import modulo.core.runner_capacity as rc

    monkeypatch.setattr(rc, "get_settings", lambda: _S())
    monkeypatch.setattr(rc, "_sweep_recoverability_predicate", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(rc, "_run_recoverable", AsyncMock(return_value=False))

    stale_marker = json.dumps({"state": "cleared_at_hitl", "written_at": (_NOW - timedelta(hours=30)).isoformat()})
    stale_awaiting = _Row(status="awaiting_human", sandbox_dispatch_state=stale_marker)
    factory = _FakeSweepFactory([stale_awaiting])
    org_cm = factory._org_session()
    org_session = org_cm.__aenter__.return_value
    # Intercept: the CAS UPDATE answers 0 rows when marker_seen does not match
    # the row's CURRENT marker (simulating a concurrent fresh-marker commit
    # between the candidate SELECT and the UPDATE).
    fresh_marker = build_dispatch_marker("fresh", "e2b")
    stale_awaiting.sandbox_dispatch_state = fresh_marker

    async def _execute(stmt: Any, params: Any = None) -> Any:
        text_str = str(stmt)
        result = MagicMock()
        if "FROM organisations" in text_str:
            result.all.return_value = [(uuid.uuid4(),)]
        elif "SELECT id, status, error_code" in text_str:
            result.all.return_value = [stale_awaiting]
        elif "count" in text_str.lower():
            result.scalar_one.return_value = 0
        elif "sandbox_dispatch_state = NULL" in text_str:
            assert params["marker_seen"] == fresh_marker, "the CAS guard must bind the CURRENT marker"
            result.fetchone.return_value = None  # 0 rows: the classified text no longer matches
        else:
            result.all.return_value = []
            result.fetchone.return_value = None
            result.scalar_one.return_value = 0
        return result

    org_session.execute = AsyncMock(side_effect=_execute)
    calls = {"n": 0}

    def _factory() -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return factory._lock_session()
        if calls["n"] == 2:
            org_index = MagicMock()

            async def _org_index_execute(stmt: Any, params: Any = None) -> Any:
                result = MagicMock()
                result.all.return_value = [(uuid.uuid4(),)]
                return result

            org_index.execute = AsyncMock(side_effect=_org_index_execute)
            return factory._wrap(org_index)
        return org_cm

    result = await reconcile_runner_dispatch_markers(_factory)  # type: ignore[arg-type]
    assert result["cleared"] == 0, "a CAS-defeated clear counts nothing"
    assert not factory.cleared
    assert result["transitioned"] == 0


async def test_sweep_org_failure_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """F5: an org pass failure raises RunnerMarkerSweepError carrying the
    PARTIAL counts — a swallowed sweep failure is a silently dead safety net."""
    _patch_gate(monkeypatch, flag_on=False)

    class _S(_FakeGateSettings):
        runner_marker_stale_seconds = 25 * 3600
        saq_job_heartbeat = 30
        saq_reenqueue_window = 600
        saq_claimed_nodeless_minutes = 20

    import modulo.core.runner_capacity as rc

    monkeypatch.setattr(rc, "get_settings", lambda: _S())
    monkeypatch.setattr(rc, "_sweep_recoverability_predicate", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(rc, "_run_recoverable", AsyncMock(return_value=False))

    factory = _FakeSweepFactory([])
    calls = {"n": 0}

    def _factory() -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return factory._lock_session()
        if calls["n"] == 2:
            org_index = MagicMock()

            async def _org_index_execute(stmt: Any, params: Any = None) -> Any:
                result = MagicMock()
                result.all.return_value = [(uuid.uuid4(),), (uuid.uuid4(),)]
                return result

            org_index.execute = AsyncMock(side_effect=_org_index_execute)
            return factory._wrap(org_index)

        org_session = MagicMock()

        async def _org_execute(stmt: Any, params: Any = None) -> Any:
            raise RuntimeError("db down mid-sweep")

        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=org_session)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        org_session.begin = MagicMock(return_value=begin_cm)
        org_session.execute = AsyncMock(side_effect=_org_execute)
        org_session.close = AsyncMock()
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=org_session)
        session_cm.__aexit__ = AsyncMock(return_value=False)
        session_cm.close = AsyncMock()
        return session_cm

    with pytest.raises(RunnerMarkerSweepError):
        await reconcile_runner_dispatch_markers(_factory)  # type: ignore[arg-type]


def test_sweep_sql_sandbox_id_asymmetry() -> None:
    """Pin the sandbox_id asymmetry between the two CAS sweep UPDATEs.

    ``_CLEAR_MARKER_SQL`` PRESERVES ``sandbox_id`` — a cleared marker's run is
    already terminal (or parked), and the sandbox id is the evidence the D4
    workspace reconciler needs to find and destroy the container.
    ``_TRANSITION_STALE_RUNNING_SQL`` NULLS it — that run is killed, so its
    workspace must NOT be re-adopted (FAR-595 pinning test).
    """
    from modulo.core import runner_capacity as rc

    clear_sql = str(rc._CLEAR_MARKER_SQL)
    transition_sql = str(rc._TRANSITION_STALE_RUNNING_SQL)
    assert "sandbox_dispatch_state = NULL" in clear_sql
    assert "sandbox_id" not in clear_sql
    assert "sandbox_dispatch_state = NULL" in transition_sql
    assert "sandbox_id = NULL" in transition_sql


async def test_sweep_failed_org_pass_emits_no_marker_cleared_events(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Phantom-event guarantee (qa F14, FAR-595 pinning test): the
    ``runner.capacity.marker_cleared`` event is emitted AFTER the org
    transaction commits. An org pass that fails MID-TRANSACTION — even one
    whose clear UPDATE already executed inside the doomed transaction — must
    emit ZERO events: a rolled-back write never happened, and a phantom note
    would send the D4 reconciler after a container the row no longer
    references."""
    _patch_gate(monkeypatch, flag_on=False)

    class _S(_FakeGateSettings):
        runner_marker_stale_seconds = 25 * 3600
        saq_job_heartbeat = 30
        saq_reenqueue_window = 600
        saq_claimed_nodeless_minutes = 20

    import modulo.core.runner_capacity as rc

    monkeypatch.setattr(rc, "get_settings", lambda: _S())
    monkeypatch.setattr(rc, "_sweep_recoverability_predicate", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(rc, "_run_recoverable", AsyncMock(return_value=False))

    stale_marker = json.dumps({"state": "cleared_at_hitl", "written_at": (_NOW - timedelta(hours=30)).isoformat()})
    doomed_row = _Row(status="awaiting_human", sandbox_dispatch_state=stale_marker)
    factory = _FakeSweepFactory([])
    calls = {"n": 0}

    def _factory() -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return factory._lock_session()
        if calls["n"] == 2:
            org_index = MagicMock()

            async def _org_index_execute(stmt: Any, params: Any = None) -> Any:
                result = MagicMock()
                result.all.return_value = [(uuid.uuid4(),)]
                return result

            org_index.execute = AsyncMock(side_effect=_org_index_execute)
            return factory._wrap(org_index)

        org_session = MagicMock()

        async def _org_execute(stmt: Any, params: Any = None) -> Any:
            text_str = str(stmt)
            # The clear UPDATE executes INSIDE the transaction that is about
            # to fail — the outcome is classified but never committed.
            if "SELECT id, status, error_code" in text_str:
                result = MagicMock()
                result.all.return_value = [doomed_row]
                return result
            if "sandbox_dispatch_state = NULL" in text_str:
                result = MagicMock()
                result.fetchone.return_value = (doomed_row.id,)
                return result
            raise RuntimeError("db down mid-transaction")

        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=org_session)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        org_session.begin = MagicMock(return_value=begin_cm)
        org_session.execute = AsyncMock(side_effect=_org_execute)
        org_session.close = AsyncMock()
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=org_session)
        session_cm.__aexit__ = AsyncMock(return_value=False)
        session_cm.close = AsyncMock()
        return session_cm

    with (
        caplog.at_level(logging.WARNING, logger="modulo.core.runner_capacity"),
        pytest.raises(RunnerMarkerSweepError) as excinfo,
    ):
        await reconcile_runner_dispatch_markers(_factory)  # type: ignore[arg-type]

    # The failure is loud (F5) but carries no clear credit: the committed
    # counts are zero even though the clear UPDATE ran inside the transaction.
    assert excinfo.value.org_failures == 1
    assert excinfo.value.scanned == 1
    assert excinfo.value.cleared == 0
    assert excinfo.value.transitioned == 0
    assert any("runner.capacity.marker_sweep_org_failed" in r.message for r in caplog.records)
    cleared_events = [r for r in caplog.records if r.message == "runner.capacity.marker_cleared"]
    assert not cleared_events


async def test_saq_cron_persists_partial_counts_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    """F5 liveness contract: the SAQ cron wrapper persists the PARTIAL counts
    with ``error: sweep_failed`` BEFORE re-raising (SAQ retries engage)."""
    from modulo.core import saq_worker

    persisted: list[tuple[str, dict[str, Any]]] = []

    async def _persist(key: str, payload: dict[str, Any], ttl: int) -> None:
        persisted.append((key, payload))

    monkeypatch.setattr(saq_worker, "_persist_sweep_stats", _persist)

    async def _boom(_factory: Any) -> dict[str, Any]:
        raise RunnerMarkerSweepError(scanned=3, cleared=1, transitioned=0, org_failures=1)

    monkeypatch.setattr(
        "modulo.core.runner_capacity.reconcile_runner_dispatch_markers",
        _boom,
    )
    monkeypatch.setattr(saq_worker, "_make_session_factory", lambda: MagicMock())

    with pytest.raises(RunnerMarkerSweepError):
        await saq_worker.runner_marker_sweep({})
    key, payload = persisted[0]
    assert key == saq_worker.RUNNER_MARKER_SWEEP_STATS_KEY
    assert payload["scanned"] == 3
    assert payload["cleared"] == 1
    assert payload["orgs_failed"] == 1
    assert payload["error"] == "sweep_failed"


# ---------------------------------------------------------------------------
# Sweep wiring (production callers — the silent-dead-sweep guard)
# ---------------------------------------------------------------------------


async def test_sweep_wired_into_dispatcher_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sweep is invoked by the 60s dispatcher_reconcile compensating-sweep
    block — a sweep with zero production callers is a silent critical."""
    from modulo.core import cron_helpers

    called = AsyncMock(return_value={"scanned": 0, "cleared": 0, "transitioned": 0, "violations": 0})
    monkeypatch.setattr("modulo.core.runner_capacity.reconcile_runner_dispatch_markers", called)
    summary = cron_helpers._dispatcher_summary()
    await cron_helpers._run_reconcile_sweeps(MagicMock(), summary)
    called.assert_awaited_once()


async def test_sweep_wired_into_saq_cron(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dedicated 5-min SAQ cron wrapper invokes the sweep (independent
    periodic path with a liveness key)."""
    from modulo.core import saq_worker

    called = AsyncMock(return_value={"scanned": 1, "cleared": 1, "transitioned": 0, "violations": 0, "orgs_failed": 0})
    monkeypatch.setattr("modulo.core.runner_capacity.reconcile_runner_dispatch_markers", called)
    result = await saq_worker.runner_marker_sweep({})
    called.assert_awaited_once()
    assert result["cleared"] == 1


async def test_sweep_violations_count_breach_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """F4 (8 lenses): the violations counter counts a CAP BREACH, not runner
    activity — a cap-less org with live markers is NEVER a violation."""
    _patch_gate(monkeypatch, flag_on=False)

    class _S(_FakeGateSettings):
        runner_marker_stale_seconds = 25 * 3600
        saq_job_heartbeat = 30
        saq_reenqueue_window = 600
        saq_claimed_nodeless_minutes = 20

    import modulo.core.runner_capacity as rc

    monkeypatch.setattr(rc, "get_settings", lambda: _S())
    monkeypatch.setattr(rc, "_sweep_recoverability_predicate", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(rc, "_run_recoverable", AsyncMock(return_value=False))

    factory = _FakeSweepFactory([_Row(status="running", sandbox_dispatch_state=build_dispatch_marker("k", "e2b"))])

    async def _uncapped(_session: Any, _org: uuid.UUID, **_kw: Any) -> Any:
        return RunnerCapacityDecision(cap=None, active=3, host_resource_only=False)

    monkeypatch.setattr("modulo.core.runner_capacity.resolve_runner_capacity_decision", _uncapped)
    result = await reconcile_runner_dispatch_markers(factory)  # type: ignore[arg-type]
    assert result["violations"] == 0, "a cap-less org's live markers are activity, not a breach"

    async def _breached(_session: Any, _org: uuid.UUID, **_kw: Any) -> Any:
        return RunnerCapacityDecision(cap=1, active=2, host_resource_only=False)

    monkeypatch.setattr("modulo.core.runner_capacity.resolve_runner_capacity_decision", _breached)
    result = await reconcile_runner_dispatch_markers(factory)  # type: ignore[arg-type]
    assert result["violations"] == 1, "a genuine breach (active > cap) is counted exactly once per org"


# ---------------------------------------------------------------------------
# All-three-paths convergence (executor resume gate uses the SAME derivation)
# ---------------------------------------------------------------------------


async def test_resume_gate_converges_onto_reserved_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """D8 convergence: the executor's resume gate (flag-on) takes the advisory
    lock with the SAME per-org key derivation as the dispatch gate (the
    reserved namespace — NOT the legacy ``_uuid_to_lock_keys`` keyspace) and
    counts the narrowed marker population."""
    _patch_gate(monkeypatch, flag_on=True)

    class _S(_FakeGateSettings):
        runner_capacity_gate_enabled = True

    # The executor imports get_settings from modulo.settings directly.
    monkeypatch.setattr("modulo.settings.get_settings", lambda: _S())

    from modulo.core.connector_hub.locking import _uuid_to_lock_keys
    from modulo.core.pipeline_engine.executor import PipelineExecutor, SandboxCapacityExceededError

    executor = PipelineExecutor(MagicMock())
    executed: list[tuple[str, Any]] = []
    session = MagicMock()

    async def _execute(stmt: Any, params: Any = None) -> Any:
        text_str = str(stmt)
        executed.append((text_str, params))
        result = MagicMock()
        if "pg_advisory_xact_lock" in text_str:
            result.fetchone.return_value = (True,)
        else:
            result.scalar_one.return_value = 2  # count: at cap
        return result

    session.execute = AsyncMock(side_effect=_execute)

    async def _fake_graph(_snapshot_id: Any) -> MagicMock:
        return {"nodes": [{"id": "s", "node_type": "sandbox_agent"}], "edges": []}

    monkeypatch.setattr(
        "modulo.core.pipeline_engine.executor._graph_contains_sandbox_agent",
        lambda _graph: True,
    )

    async def _fake_contract(_session: Any, _org: uuid.UUID) -> tuple[int | None, bool]:
        return 2, False

    monkeypatch.setattr("modulo.core.runner_capacity.read_runner_cap_contract", _fake_contract)
    from modulo.core.runner_capacity import count_active_runner_dispatches_for_decision

    async def _fake_count(_session: Any, _org: uuid.UUID, **_kw: Any) -> int:
        return 2

    monkeypatch.setattr(
        "modulo.core.runner_capacity.count_active_runner_dispatches_for_decision",
        _fake_count,
    )
    _ = count_active_runner_dispatches_for_decision

    with pytest.raises(SandboxCapacityExceededError):
        await executor._enforce_resume_sandbox_capacity(
            session, org_id=_ORG, run_id=uuid.uuid4(), snapshot_id=uuid.uuid4()
        )

    advisory = [(t, p) for t, p in executed if "pg_advisory_xact_lock" in t]
    assert advisory, "the flag-on resume gate must take the per-org advisory lock"
    k1, k2 = runner_capacity_lock_keys(_ORG)
    assert advisory[0][1] == {"k1": k1, "k2": k2}, "the resume gate uses the RESERVED namespace derivation"
    assert advisory[0][1]["k1"] != _uuid_to_lock_keys(_ORG)[0], "never the legacy keyspace"
    # qa F7: SET LOCAL lock_timeout precedes the advisory lock.
    assert any("set_config('lock_timeout'" in t for t, _p in executed)
    lock_idx = next(i for i, (t, _p) in enumerate(executed) if "set_config('lock_timeout'" in t)
    assert lock_idx < next(i for i, (t, _p) in enumerate(executed) if "pg_advisory_xact_lock" in t)


async def test_resume_gate_lock_timeout_maps_to_retryable_capacity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qa F7: a crowded per-org advisory lock raises SQLSTATE 55P03 (what
    SET LOCAL lock_timeout actually produces) — mapped to the RETRYABLE
    ``SandboxCapacityExceededError`` (the caller's ``capacity.org`` house
    failure), never an unhandled DBAPIError."""
    _patch_gate(monkeypatch, flag_on=True)

    class _S(_FakeGateSettings):
        runner_capacity_gate_enabled = True
        runner_capacity_lock_timeout_ms = 750

    monkeypatch.setattr("modulo.settings.get_settings", lambda: _S())

    from sqlalchemy.exc import DBAPIError

    from modulo.core.pipeline_engine.executor import PipelineExecutor, SandboxCapacityExceededError

    executor = PipelineExecutor(MagicMock())
    inner = Exception("lock timeout")
    inner.sqlstate = "55P03"  # type: ignore[attr-defined]
    session = MagicMock()

    async def _execute(stmt: Any, params: Any = None) -> Any:
        if "pg_advisory_xact_lock" in str(stmt):
            raise DBAPIError("stmt", {}, inner)
        result = MagicMock()
        result.scalar_one.return_value = 0
        return result

    session.execute = AsyncMock(side_effect=_execute)
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.executor._graph_contains_sandbox_agent",
        lambda _graph: True,
    )

    async def _fake_contract(_session: Any, _org: uuid.UUID) -> tuple[int | None, bool]:
        return 2, False

    monkeypatch.setattr("modulo.core.runner_capacity.read_runner_cap_contract", _fake_contract)

    with pytest.raises(SandboxCapacityExceededError) as exc_info:
        await executor._enforce_resume_sandbox_capacity(
            session, org_id=_ORG, run_id=uuid.uuid4(), snapshot_id=uuid.uuid4()
        )
    # The mapped error carries the 55P03 DBAPI chain (sqlstate_of extraction).
    dbapi_cause = exc_info.value.__cause__
    assert dbapi_cause is not None
    from modulo.db.sqlstates import sqlstate_of

    assert sqlstate_of(dbapi_cause) == "55P03"


def _unused(*_a: Any, **_kw: Any) -> None:  # pragma: no cover
    _ = asyncio
