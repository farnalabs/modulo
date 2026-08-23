"""Unit tests for modulo.core.pipeline_execution.

Tests are mock/fake based — no Postgres required. Real Postgres concurrency
behaviour (two concurrent claims -> exactly one) lives in
``tests/integration/test_pipeline_execution.py`` (marked ``integration``).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

import modulo.core.pipeline_execution as pe
from modulo.db.models.run import Run

# ---------------------------------------------------------------------------
# Fake engine / connection doubles (sync)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, row: object | None = None) -> None:
        self._row = row

    def fetchone(self) -> object | None:
        return self._row


class _FakeConn:
    def __init__(self, row: object | None = None, *, raise_on_execute: bool = False) -> None:
        self._row = row
        self._raise = raise_on_execute
        self.statements: list[str] = []
        self.params: list[dict[str, object]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    def execute(self, stmt: object, params: dict[str, object] | None = None) -> _FakeResult:
        self.statements.append(str(stmt))
        self.params.append(params or {})
        if self._raise:
            raise RuntimeError("boom")
        return _FakeResult(self._row)


class _FakeEngine:
    def __init__(self, row: object | None = None, *, raise_on_execute: bool = False) -> None:
        self.conn = _FakeConn(row, raise_on_execute=raise_on_execute)

    def connect(self) -> _FakeConn:
        return self.conn


class _AsyncResultRow:
    """Async fake result exposing ``fetchone`` (used by the fenced writers)."""

    def __init__(self, row: object | None = None) -> None:
        self._row = row

    def fetchone(self) -> object | None:
        return self._row


class _AsyncConnRow:
    """Async fake connection recording executed statements, returning a fixed row."""

    def __init__(self, row: object | None = None) -> None:
        self._row = row
        self.statements: list[str] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResultRow:
        self.statements.append(str(stmt))
        return _AsyncResultRow(self._row)

    async def commit(self) -> None:
        return None


class _AsyncEngineRow:
    def __init__(self, row: object | None = None) -> None:
        self.conn = _AsyncConnRow(row)

    def connect(self) -> _AsyncConnRow:
        return self.conn


def _make_settings(**overrides: object) -> MagicMock:
    """Mock Settings with the SAQ/legacy claim staleness plumbing values."""
    base = {
        "run_claim_stale_seconds": 450,
        "saq_never_dispatched_window": 300,
        "saq_worker_lost_window": 600,
        "saq_job_heartbeat": 300,
        "run_heartbeat_seconds": 30,
        "saq_worker_db_pool_size": 2,
        "saq_redis_pool_size": 50,
        "saq_run_claim_cap": 20,
    }
    base.update(overrides)
    return MagicMock(**base)


def _compiled(stmt: object, *, render_postcompile: bool = False) -> str:
    compile_kwargs = {"render_postcompile": True} if render_postcompile else {}
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs=compile_kwargs))


# ---------------------------------------------------------------------------
# Claim — SQL structure + staleness constants
# ---------------------------------------------------------------------------


class TestBuildClaimUpdate:
    def test_single_atomic_update_with_returning(self) -> None:
        stmt = pe.build_claim_update(stale_seconds=450)
        sql = _compiled(stmt)
        assert "UPDATE runs" in sql
        assert "SET status='running'" in sql
        assert "heartbeat_at=now()" in sql
        assert "claim_count=claim_count+1" in sql
        assert "RETURNING id" in sql
        # Atomicity is by construction: one UPDATE ... WHERE ... RETURNING.
        assert sql.count("UPDATE") == 1

    def test_claimable_statuses_and_staleness_gate(self) -> None:
        stmt = pe.build_claim_update(stale_seconds=450)
        sql = _compiled(stmt)
        # pending runs are always claimable; running runs need a stale heartbeat
        assert "status = 'pending'" in sql
        assert "status = 'running'" in sql
        assert "heartbeat_at" in sql
        assert "stale_seconds" in sql

    def test_claim_cap_is_bound(self) -> None:
        stmt = pe.build_claim_update(stale_seconds=450, claim_cap=20)
        sql = _compiled(stmt)
        assert "claim_count <" in sql


class TestClaimRunAsync:
    async def test_async_claim_uses_saq_stale_seconds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, object]] = []

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _FakeResult:
                calls.append({"stmt": str(stmt), "params": params or {}})
                return _FakeResult(("id",))

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        monkeypatch.setattr(pe, "get_settings", lambda: _make_settings())
        engine = _AsyncEngine()
        with patch.object(pe, "_maybe_alert_retry_storm", new=AsyncMock()) as storm:
            claim_token = await pe.claim_run_async(engine, "run-1", "org-1")  # type: ignore[arg-type]
        assert claim_token is not None
        # The RLS org-context set_config runs FIRST on the raw connection (C3) —
        # the claim UPDATE is the second statement.
        assert "set_config('app.organisation_id'" in calls[0]["stmt"]  # type: ignore[index]
        claim_call = calls[1]
        assert claim_call["params"]["stale_seconds"] == 450  # type: ignore[index]
        # Claim cap flows from settings (SAQ_RUN_CLAIM_CAP, default 20) when the
        # caller omits claim_cap — single source of truth shared with resume
        # (retro item 9). The old execute-only cap of 5 is retired.
        assert claim_call["params"]["claim_cap"] == 20  # type: ignore[index]
        # Every claim rotates to a fresh per-claim token (plan F3a), returned to
        # the caller so it can fence completion/heartbeat against successors.
        assert "claim_token=:tok" in claim_call["stmt"]  # type: ignore[index]
        assert claim_call["params"]["tok"] == claim_token  # type: ignore[index]
        storm.assert_awaited_once()

    async def test_async_claim_false_when_no_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _FakeResult:
                return _FakeResult(None)

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        monkeypatch.setattr(pe, "get_settings", lambda: _make_settings())
        engine = _AsyncEngine()
        assert await pe.claim_run_async(engine, "run-1", "org-1") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 'complete' fix — DB enum source of truth + mark_complete
# ---------------------------------------------------------------------------


class TestCompleteStatus:
    def test_db_enum_uses_complete_not_completed(self) -> None:
        from sqlalchemy import CheckConstraint

        enum_sql = "\n".join(
            str(getattr(c, "sqltext", c)) for c in Run.__table_args__ if isinstance(c, CheckConstraint)
        )
        assert "complete" in enum_sql
        assert "'completed'" not in enum_sql

    def test_shared_module_constant_matches_db_enum(self) -> None:
        assert pe.RUN_COMPLETE_STATUS == "complete"


class TestMarkComplete:
    """mark_complete is now a single conditional fenced UPDATE (A1) — no ORM
    round-trip, no E2B Redis key release."""

    async def test_writes_complete_enum(self) -> None:
        conn = _AsyncConnRow(("id",))
        engine = MagicMock()
        engine.connect.return_value = conn

        await pe.mark_complete(engine, str(uuid.uuid4()), str(uuid.uuid4()))  # type: ignore[arg-type]

        joined = " ".join(conn.statements)
        assert "UPDATE runs SET status='complete', completed_at=now()" in joined
        assert "status='running'" in joined
        assert "claim_token" in joined
        assert "cancellation_requested = false" in joined

    async def test_superseded_rowcount_zero_skips_write(self, caplog: pytest.LogCaptureFixture) -> None:
        # Rowcount 0 (superseded / not running / cancelled) → no-op with a warning.
        conn = _AsyncConnRow(None)
        engine = MagicMock()
        engine.connect.return_value = conn
        with caplog.at_level("WARNING", logger="modulo.core.pipeline_execution"):
            await pe.mark_complete(engine, str(uuid.uuid4()), str(uuid.uuid4()))  # type: ignore[arg-type]

        assert any("mark_complete skipped" in m for m in caplog.messages)


class TestFailRunTerminal:
    """fail_run_terminal is now a single conditional fenced UPDATE (A1)."""

    @pytest.mark.asyncio
    async def test_fails_running_run(self) -> None:
        conn = _AsyncConnRow(("id",))
        engine = MagicMock()
        engine.connect.return_value = conn

        ok = await pe.fail_run_terminal(  # type: ignore[arg-type]
            engine,
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            error_code="executor_stalled",
            error_detail="boom",
            claim_token="tok-a",
        )
        assert ok is True
        joined = " ".join(conn.statements)
        assert "SET status='failed'" in joined
        assert "error_code=:code" in joined
        assert "status='running'" in joined
        assert "claim_token = CAST(:tok AS text)" in joined

    @pytest.mark.asyncio
    async def test_superseded_or_terminal_returns_false(self) -> None:
        # Rowcount 0 (superseded / already terminal / cancelled) → False, no write.
        conn = _AsyncConnRow(None)
        engine = MagicMock()
        engine.connect.return_value = conn
        ok = await pe.fail_run_terminal(  # type: ignore[arg-type]
            engine,
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            error_code="executor_stalled",
            error_detail="boom",
            claim_token="tok-a",
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_fails_run_records_terminal_facts(self) -> None:
        """FAR-162 (P6'): a run terminal-failed via the raw writer also gets a
        compensating daily fact — it must be visible in analytics, never only
        in the ``runs`` row. The facts write is a fail-open best-effort."""
        run_id = str(uuid.uuid4())
        org_id = str(uuid.uuid4())
        conn = _AsyncConnRow(("id",))
        engine = MagicMock()
        engine.connect.return_value = conn

        with (
            patch.object(pe, "_advance_journeys_from_stored_refs", new_callable=AsyncMock) as advance,
            patch.object(pe, "_record_fact_for_terminal_failed_run", new_callable=AsyncMock) as record_facts,
        ):
            ok = await pe.fail_run_terminal(  # type: ignore[arg-type]
                engine,
                run_id,
                org_id,
                error_code="executor_heartbeat_lost",
                error_detail="boom",
                claim_token="tok-a",
            )

        assert ok is True
        advance.assert_awaited_once_with(engine, run_id, org_id, "failed")
        record_facts.assert_awaited_once_with(engine, run_id, org_id)

    @pytest.mark.asyncio
    async def test_facts_write_failure_is_fail_open(self) -> None:
        """A facts-write failure after the terminal UPDATE commits must NOT
        surface as a failed terminal-fail — the run is already failed."""
        run_id = str(uuid.uuid4())
        org_id = str(uuid.uuid4())
        conn = _AsyncConnRow(("id",))
        engine = MagicMock()
        engine.connect.return_value = conn

        async def _boom(*_a: object, **_kw: object) -> None:
            raise RuntimeError("facts boom")

        with (
            patch.object(pe, "_advance_journeys_from_stored_refs", new_callable=AsyncMock),
            patch.object(pe, "_record_fact_for_terminal_failed_run", new=_boom),
            patch.object(pe._log, "warning"),
        ):
            ok = await pe.fail_run_terminal(  # type: ignore[arg-type]
                engine, run_id, org_id, error_code="executor_stalled", error_detail="boom"
            )
        assert ok is True, "the terminal failure itself must still report success (facts are best-effort)"


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    async def test_heartbeat_once_writes_db_and_updates_job(self) -> None:
        executed: list[str] = []

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _FakeResult:
                executed.append(str(stmt))
                return _FakeResult()

            async def commit(self) -> None:
                return None

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        job = MagicMock()
        job.update = AsyncMock()

        await pe.heartbeat_once(_AsyncEngine(), "run-1", "org-1", job=job)  # type: ignore[arg-type]

        assert len(executed) == 2  # set_config + UPDATE runs SET heartbeat_at=now()
        assert "UPDATE runs SET heartbeat_at=now()" in executed[1]
        job.update.assert_awaited_once()

    async def test_heartbeat_once_without_job_skips_job_update(self) -> None:
        executed: list[str] = []

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _FakeResult:
                executed.append(str(stmt))
                return _FakeResult()

            async def commit(self) -> None:
                return None

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        await pe.heartbeat_once(_AsyncEngine(), "run-1", "org-1")  # type: ignore[arg-type]
        assert len(executed) == 2

    async def test_heartbeat_once_superseded_raises_and_skips_job(self) -> None:
        """A fenced heartbeat whose UPDATE matches zero rows (superseded / row
        gone) raises ClaimSupersededError and never touches the job hash
        (dist/runtime-core A1)."""
        executed: list[str] = []

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _FakeResult:
                executed.append(str(stmt))
                return _FakeResult(None)  # rowcount 0 — no RETURNING row

            async def commit(self) -> None:
                return None

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        job = MagicMock()
        job.update = AsyncMock()

        with pytest.raises(pe.ClaimSupersededError):
            await pe.heartbeat_once(_AsyncEngine(), "run-1", "org-1", job=job, claim_token="tok-a")  # type: ignore[arg-type]

        assert "claim_token=:tok" in executed[1]
        job.update.assert_not_awaited()

    async def test_heartbeat_loop_sets_superseded_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A superseded heartbeat sets the ``superseded`` Event and breaks."""

        async def _superseded(*_a: object, **_kw: object) -> None:
            raise pe.ClaimSupersededError("superseded")

        monkeypatch.setattr(pe, "heartbeat_once", _superseded)
        monkeypatch.setattr(pe.asyncio, "sleep", AsyncMock())
        superseded = asyncio.Event()
        await pe.heartbeat_loop(
            MagicMock(), "run-1", "org-1", interval_seconds=1, claim_token="tok-a", superseded=superseded
        )
        assert superseded.is_set()

    async def test_heartbeat_loop_fails_closed_after_3_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """3 consecutive heartbeat failures set the ``health_failed`` Event and break."""

        async def _failing(*_a: object, **_kw: object) -> None:
            raise RuntimeError("db down")

        monkeypatch.setattr(pe, "heartbeat_once", _failing)
        monkeypatch.setattr(pe.asyncio, "sleep", AsyncMock())
        health_failed = asyncio.Event()
        await pe.heartbeat_loop(
            MagicMock(), "run-1", "org-1", interval_seconds=1, claim_token="tok-a", health_failed=health_failed
        )
        assert health_failed.is_set()
        assert pe.asyncio.sleep.await_count == 3

    async def test_heartbeat_loop_uses_configured_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        heartbeat_mock = AsyncMock()
        monkeypatch.setattr(pe, "heartbeat_once", heartbeat_mock)
        monkeypatch.setattr(pe, "_read_current_claim_token", AsyncMock(return_value="tok-a"))
        monkeypatch.setattr(pe.asyncio, "sleep", AsyncMock(side_effect=[None, KeyboardInterrupt()]))
        engine = MagicMock()

        with pytest.raises(KeyboardInterrupt):
            await pe.heartbeat_loop(engine, "run-1", "org-1", interval_seconds=45)  # type: ignore[arg-type]

        assert pe.asyncio.sleep.await_count == 2
        assert pe.asyncio.sleep.await_args.args[0] == 45  # type: ignore[union-attr]
        # Every heartbeat is fenced with the executor's claim token (plan F3a).
        heartbeat_mock.assert_awaited_with(engine, "run-1", "org-1", job=None, claim_token="tok-a")

    async def test_heartbeat_loop_superseded_breaks_without_further_sleeps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single ClaimSupersededError breaks the loop after the FIRST
        heartbeat — no further sleeps run, the abort is prompt (plan F3a)."""
        calls = {"n": 0}

        async def _superseded(*_a: object, **_kw: object) -> None:
            calls["n"] += 1
            raise pe.ClaimSupersededError("superseded")

        monkeypatch.setattr(pe, "heartbeat_once", _superseded)
        monkeypatch.setattr(pe.asyncio, "sleep", AsyncMock())
        superseded = asyncio.Event()
        await pe.heartbeat_loop(
            MagicMock(), "run-1", "org-1", interval_seconds=1, claim_token="tok-a", superseded=superseded
        )
        assert superseded.is_set()
        assert calls["n"] == 1
        # Exactly ONE lead-in sleep then the superseded heartbeat breaks the loop.
        assert pe.asyncio.sleep.await_count == 1

    async def test_heartbeat_loop_transient_failure_then_success_continues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient Exception is counted but does NOT break the loop — the
        next successful heartbeat resets the counter (fail-open on a single
        hiccup; only 3 consecutive failures set ``health_failed``)."""
        heartbeat_mock = AsyncMock(side_effect=[RuntimeError("db hiccup"), None])
        monkeypatch.setattr(pe, "heartbeat_once", heartbeat_mock)
        monkeypatch.setattr(pe.asyncio, "sleep", AsyncMock(side_effect=[None, None, KeyboardInterrupt()]))

        with pytest.raises(KeyboardInterrupt):
            await pe.heartbeat_loop(MagicMock(), "run-1", "org-1", interval_seconds=1, claim_token="tok-a")

        assert heartbeat_mock.await_count == 2
        # The second heartbeat (after the transient) ran — the loop continued.
        assert heartbeat_mock.await_args_list[1].kwargs["claim_token"] == "tok-a"
        # Third iteration's lead-in sleep fired before KeyboardInterrupt broke out.
        assert pe.asyncio.sleep.await_count == 3

    async def test_heartbeat_loop_atomic_lease_rowcount_zero_breaks_and_sets_superseded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: ``heartbeat_once``'s atomic fenced UPDATE matching ZERO
        rows (the run's claim token was rotated by a successor — superseded
        lease) surfaces as ClaimSupersededError; the loop sets the superseded
        Event and breaks with NO further heartbeats."""
        executed: list[str] = []

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _FakeResult:
                executed.append(str(stmt))
                return _FakeResult(None)  # rowcount 0 — no RETURNING row

            async def commit(self) -> None:
                return None

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        monkeypatch.setattr(pe.asyncio, "sleep", AsyncMock())
        superseded = asyncio.Event()
        await pe.heartbeat_loop(  # type: ignore[arg-type]
            _AsyncEngine(), "run-1", "org-1", interval_seconds=1, claim_token="tok-a", superseded=superseded
        )
        assert superseded.is_set()
        # Exactly one lead-in sleep, then the rowcount-0 fenced UPDATE raises and breaks.
        assert pe.asyncio.sleep.await_count == 1
        assert any("claim_token=:tok" in s for s in executed)


# ---------------------------------------------------------------------------
# Stale-run recovery sweep — legacy windows match today's beat-sweep values
# ---------------------------------------------------------------------------


class TestStaleRunRecoverySweep:
    """The sweep is now scoped PER-ORG via set_config('app.organisation_id')
    (the RLS no-op fix, spec §9.4) — the org enumeration runs first in system
    context, then the four UPDATE branches run once per org."""

    org_row = (uuid.UUID("00000000-0000-0000-0000-0000000000aa"),)

    def _result(self, rows: list[Any] | None = None, rowcount: int = 0) -> Any:
        r = MagicMock()
        r.rowcount = rowcount
        r.all = MagicMock(return_value=rows or [])
        return r

    def _engine(self, statements: list[str] | None = None, params: list[dict[str, object]] | None = None) -> Any:
        statements = statements if statements is not None else []
        params = params if params is not None else []

        class _AsyncResult:
            def __init__(self, rows: list[Any] | None = None, rowcount: int = 0) -> None:
                self.rowcount = rowcount
                self._rows = rows or []

            def all(self) -> list[Any]:
                return self._rows

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, bind: dict[str, object] | None = None) -> _AsyncResult:
                statements.append(str(stmt))
                params.append(bind or {})
                if "SELECT id FROM organisations" in str(stmt):
                    return _AsyncResult(rows=[self._org_row()])
                if "RETURNING id, organisation_id" in str(stmt):
                    return _AsyncResult(rowcount=1, rows=[self._stranded_row()])
                return _AsyncResult(rowcount=self._rowcount)

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        return _AsyncEngine()

    def _org_row(self) -> tuple[uuid.UUID]:
        return self.org_row

    def _stranded_row(self) -> Any:
        row = MagicMock()
        row.id = uuid.uuid4()
        row.organisation_id = uuid.uuid4()
        return row

    async def test_uses_legacy_300_600_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        statements: list[str] = []
        org_row = (uuid.UUID("00000000-0000-0000-0000-0000000000aa"),)

        class _AsyncResult:
            rowcount = 0

            def __init__(self, is_org: bool = False) -> None:
                self._is_org = is_org

            def all(self) -> list[Any]:
                return [org_row] if self._is_org else []

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
                statements.append(str(stmt))
                return _AsyncResult(is_org="SELECT id FROM organisations" in str(stmt))

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        monkeypatch.setattr(pe, "get_settings", lambda: _make_settings())
        engine = _AsyncEngine()
        result = await pe.stale_run_recovery_sweep(engine)  # type: ignore[arg-type]

        assert result["never_dispatched_swept"] == 0
        assert result["worker_lost_swept"] == 0
        assert result["capacity_timeout_swept"] == 0
        assert result["stranded_capacity_redispatched"] == 0
        # Per-org: org enumeration + set_config + the four UPDATE branches.
        assert len(statements) == 6
        joined = " ".join(statements)
        assert "never_dispatched" in joined
        assert "RETURNING id, organisation_id" in joined
        assert "org_capacity_limited" in joined
        assert "pipeline_capacity" in joined
        assert "capacity_timeout" in joined
        assert "worker_lost" in joined
        assert ":nd_window" in joined
        assert ":redispatch_ttl" in joined
        assert ":fail_ttl" in joined
        assert ":ttl" in joined
        assert ":wl_window" in joined

    async def test_explicit_windows_override_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        params_seen: list[dict[str, object]] = []

        class _AsyncResult:
            rowcount = 1

            def all(self) -> list[Any]:
                return self._rows if self._is_org else []

        org_row = (uuid.UUID("00000000-0000-0000-0000-0000000000aa"),)
        _rows: list[Any] = []

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
                params_seen.append(params or {})
                r = _AsyncResult()
                if "SELECT id FROM organisations" in str(stmt):
                    r._is_org = True
                    r._rows = [org_row]
                else:
                    r._is_org = False
                    r._rows = []
                return r

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        monkeypatch.setattr(pe, "get_settings", lambda: _make_settings())
        engine = _AsyncEngine()
        result = await pe.stale_run_recovery_sweep(  # type: ignore[arg-type]
            engine, never_dispatched_window=300, worker_lost_window=900
        )

        assert result["never_dispatched_swept"] == 1
        assert result["worker_lost_swept"] == 1
        assert result["capacity_timeout_swept"] == 1
        # params: [0]=enumeration, [1]=set_config, [2]=nd_window, [3]=redispatch,
        # [4]=capacity, [5]=wl.
        assert params_seen[2]["nd_window"] == 300
        assert params_seen[3]["redispatch_ttl"] == pe._STRANDED_REDISPATCH_TTL_MINUTES
        assert params_seen[3]["fail_ttl"] == pe.CAPACITY_TIMEOUT_TTL_MINUTES
        assert params_seen[4]["ttl"] == pe.CAPACITY_TIMEOUT_TTL_MINUTES
        assert params_seen[5]["wl_window"] == 900

    async def test_stranded_capacity_blocked_run_is_redispatched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stale-heartbeat capacity-marked pending run is re-dispatched, not failed."""
        run_id = str(uuid.uuid4())
        org_id = str(uuid.uuid4())
        redispatch_mock = AsyncMock(return_value="enqueued")

        class _Row:
            id = run_id
            organisation_id = org_id

        class _AsyncResult:
            def __init__(self, rowcount: int = 0, rows: list[Any] | None = None) -> None:
                self.rowcount = rowcount
                self._rows = rows or []

            def all(self) -> list[Any]:
                return self._rows

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
                if "SELECT id FROM organisations" in str(stmt):
                    return _AsyncResult(rows=[self.org_row])
                if "RETURNING id, organisation_id" in str(stmt):
                    return _AsyncResult(rowcount=1, rows=[_Row()])
                return _AsyncResult()

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        _AsyncConn.org_row = (uuid.UUID("00000000-0000-0000-0000-0000000000aa"),)
        monkeypatch.setattr(pe, "get_settings", lambda: _make_settings())
        with patch.object(pe, "_re_dispatch_capacity_blocked", new=redispatch_mock):
            result = await pe.stale_run_recovery_sweep(_AsyncEngine())  # type: ignore[arg-type]

        assert result["stranded_capacity_redispatched"] == 1
        assert result["capacity_timeout_swept"] == 0
        assert result["redispatch_outcomes"] == {"enqueued": 1}
        redispatch_mock.assert_awaited_once_with(run_id, org_id)

    async def test_fresh_heartbeat_capacity_blocked_run_is_not_redispatched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live retry loop's fresh heartbeat is the fence — no re-dispatch."""
        redispatch_mock = AsyncMock()

        class _AsyncResult:
            rowcount = 0

            def all(self) -> list[Any]:
                return [org_row] if self._is_org else []

        org_row = (uuid.UUID("00000000-0000-0000-0000-0000000000aa"),)

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
                r = _AsyncResult()
                r._is_org = "SELECT id FROM organisations" in str(stmt)
                return r

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        monkeypatch.setattr(pe, "get_settings", lambda: _make_settings())
        with patch.object(pe, "_re_dispatch_capacity_blocked", new=redispatch_mock):
            result = await pe.stale_run_recovery_sweep(_AsyncEngine())  # type: ignore[arg-type]

        assert result["stranded_capacity_redispatched"] == 0
        redispatch_mock.assert_not_awaited()

    async def test_capacity_timeout_eligible_run_is_not_redispatched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A run already past the 120-min fail TTL must fail, never be resurrected."""
        redispatch_mock = AsyncMock()
        params_seen: list[dict[str, object]] = []

        class _AsyncResult:
            rowcount = 0

            def all(self) -> list[Any]:
                return [org_row] if self._is_org else []

        org_row = (uuid.UUID("00000000-0000-0000-0000-0000000000aa"),)

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
                params_seen.append(params or {})
                r = _AsyncResult()
                r._is_org = "SELECT id FROM organisations" in str(stmt)
                return r

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        monkeypatch.setattr(pe, "get_settings", lambda: _make_settings())
        with patch.object(pe, "_re_dispatch_capacity_blocked", new=redispatch_mock):
            result = await pe.stale_run_recovery_sweep(_AsyncEngine())  # type: ignore[arg-type]

        assert result["stranded_capacity_redispatched"] == 0
        assert result["capacity_timeout_swept"] == 0
        # The stranded branch bounds its window with the same fail_ttl as the
        # capacity_timeout branch so the two never overlap.
        assert params_seen[3]["fail_ttl"] == pe.CAPACITY_TIMEOUT_TTL_MINUTES
        redispatch_mock.assert_not_awaited()

    async def test_terminalised_runs_record_facts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FAR-162 (P6'): every run terminalised by the sweep gets a
        compensating daily fact — never_dispatched / capacity_timeout /
        worker_lost runs must be visible in the analytics failure/stall
        dimensions (the raw UPDATEs never run finalize_cost)."""
        never_run = uuid.uuid4()
        cap_run = uuid.uuid4()
        lost_run = uuid.uuid4()
        org_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
        records: list[tuple[Any, str, str]] = []

        class _AsyncResult:
            def __init__(self, rowcount: int = 0, rows: list[Any] | None = None) -> None:
                self.rowcount = rowcount
                self._rows = rows or []

            def all(self) -> list[Any]:
                return self._rows

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
                s = str(stmt)
                if "SELECT id FROM organisations" in s:
                    return _AsyncResult(rows=[(org_id,)])
                if "RETURNING id, organisation_id" in s:
                    return _AsyncResult()
                if "never_dispatched" in s:
                    return _AsyncResult(rowcount=1, rows=[(never_run,)])
                if "capacity_timeout" in s:
                    return _AsyncResult(rowcount=1, rows=[(cap_run,)])
                if "worker_lost" in s:
                    return _AsyncResult(rowcount=1, rows=[(lost_run,)])
                return _AsyncResult()

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        async def _record(engine: Any, run_id: str, org: str) -> None:
            records.append((engine, run_id, org))

        monkeypatch.setattr(pe, "get_settings", lambda: _make_settings())
        with (
            patch.object(pe, "_advance_journeys_from_stored_refs", new_callable=AsyncMock),
            patch.object(pe, "_record_fact_for_terminal_failed_run", new=_record),
        ):
            result = await pe.stale_run_recovery_sweep(_AsyncEngine())  # type: ignore[arg-type]

        assert result["never_dispatched_swept"] == 1
        assert result["capacity_timeout_swept"] == 1
        assert result["worker_lost_swept"] == 1
        assert sorted(r[1] for r in records) == sorted(str(r) for r in (never_run, cap_run, lost_run))
        assert all(r[2] == str(org_id) for r in records)

    async def test_sweep_facts_write_failure_is_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The sweep's compensating facts writes are best-effort: a failure must
        not fail the sweep or lose the terminalisation counts."""
        never_run = uuid.uuid4()
        org_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

        async def _boom(*_a: object, **_kw: object) -> None:
            raise RuntimeError("facts boom")

        class _AsyncResult:
            def __init__(self, rowcount: int = 0, rows: list[Any] | None = None) -> None:
                self.rowcount = rowcount
                self._rows = rows or []

            def all(self) -> list[Any]:
                return self._rows

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
                s = str(stmt)
                if "SELECT id FROM organisations" in s:
                    return _AsyncResult(rows=[(org_id,)])
                if "RETURNING id, organisation_id" in s:
                    return _AsyncResult()
                if "never_dispatched" in s:
                    return _AsyncResult(rowcount=1, rows=[(never_run,)])
                return _AsyncResult()

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        monkeypatch.setattr(pe, "get_settings", lambda: _make_settings())
        with (
            patch.object(pe, "_advance_journeys_from_stored_refs", new_callable=AsyncMock),
            patch.object(pe, "_record_fact_for_terminal_failed_run", new=_boom),
            patch.object(pe._log, "warning"),
        ):
            result = await pe.stale_run_recovery_sweep(_AsyncEngine())  # type: ignore[arg-type]

        assert result["never_dispatched_swept"] == 1
        assert "error" not in result, "a facts-write failure must not fail the sweep"

    async def test_never_dispatched_and_worker_lost_write_synthetic_detail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FAR-164 (option a): the genuinely detail-less sweep writers stamp a
        synthetic error_detail so the runs list always has something to show.
        Safe for the daily-watcher hang-death detector, which keys on
        ``error_code == 'node_cancelled'`` ONLY (never worker_lost /
        never_dispatched)."""
        params_seen: list[dict[str, object]] = []
        org_row = (uuid.UUID("00000000-0000-0000-0000-0000000000aa"),)

        class _AsyncResult:
            rowcount = 0

            def all(self) -> list[Any]:
                return [org_row] if self._is_org else []

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
                params_seen.append(params or {})
                r = _AsyncResult()
                r._is_org = "SELECT id FROM organisations" in str(stmt)
                return r

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        monkeypatch.setattr(pe, "get_settings", lambda: _make_settings())
        with (
            patch.object(pe, "_advance_journeys_from_stored_refs", new_callable=AsyncMock),
            patch.object(pe, "_record_fact_for_terminal_failed_run", new_callable=AsyncMock),
        ):
            await pe.stale_run_recovery_sweep(_AsyncEngine())  # type: ignore[arg-type]

        # params: [0]=enumeration, [1]=set_config, [2]=never_dispatched,
        # [3]=stranded, [4]=capacity_timeout, [5]=worker_lost.
        assert params_seen[2]["detail"] == "Run was not dispatched within the stale threshold."
        assert params_seen[4]["detail"] == "Waited in capacity queue past the TTL."
        assert params_seen[5]["detail"] == "Worker lost heartbeat for this run."

    async def test_returns_error_dict_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _FakeResult:
                raise RuntimeError("db down")

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

        monkeypatch.setattr(pe, "get_settings", lambda: _make_settings())
        with patch.object(pe._log, "exception"):
            result = await pe.stale_run_recovery_sweep(_AsyncEngine())  # type: ignore[arg-type]

        assert result["error"] == "sweep_failed"


# ---------------------------------------------------------------------------
# execute_run orchestration — claim-then-execute-then-complete
# ---------------------------------------------------------------------------


# TestExecuteRun removed in PR C (Celery code path)


# ---------------------------------------------------------------------------
# Settings plumbing — F4 SAQ settings section defaults
# ---------------------------------------------------------------------------


_SAQ_SETTINGS_ENV = (
    "RUN_CLAIM_STALE_SECONDS",
    "SAQ_JOB_HEARTBEAT",
    "RUN_HEARTBEAT_SECONDS",
    "SAQ_HARD_GATE",
    "SAQ_AUTH_PASSWORD",
    "SAQ_AUTH_USERNAME",
    "SAQ_RUN_RETRIES",
    "SAQ_RETRY_DELAY",
    "SAQ_TEST_PAUSE",
    "SAQ_REENQUEUE_WINDOW",
    "SAQ_NEVER_DISPATCHED_WINDOW",
    "SAQ_WORKER_LOST_WINDOW",
    "SAQ_WORKER_DB_POOL_SIZE",
    "SAQ_REDIS_POOL_SIZE",
    "SAQ_WORKER_CONCURRENCY",
    "SAQ_RUN_CLAIM_CAP",
)


class TestSaqSettingsDefaults:
    def _settings(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        from modulo.settings import Settings

        return Settings()

    def test_f4_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in _SAQ_SETTINGS_ENV:
            monkeypatch.delenv(var, raising=False)
        s = self._settings(monkeypatch)
        assert s.run_claim_stale_seconds == 450
        assert s.saq_job_heartbeat == 300
        assert s.run_heartbeat_seconds == 30
        assert s.saq_hard_gate is True
        assert s.saq_auth_password is None
        assert s.saq_auth_username is None
        assert s.saq_run_retries == 5
        assert s.saq_retry_delay == 60
        assert s.saq_test_pause is False
        assert s.saq_reenqueue_window == 600
        assert s.saq_never_dispatched_window == 300
        assert s.saq_worker_lost_window == 600
        assert s.saq_worker_db_pool_size == 65
        assert s.saq_redis_pool_size == 20
        assert s.saq_worker_concurrency == 5
        assert s.saq_run_claim_cap == 20

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUN_CLAIM_STALE_SECONDS", "500")
        monkeypatch.setenv("SAQ_REDIS_POOL_SIZE", "8")
        monkeypatch.setenv("SAQ_WORKER_CONCURRENCY", "8")
        s = self._settings(monkeypatch)
        assert s.run_claim_stale_seconds == 500
        assert s.saq_redis_pool_size == 8
        assert s.saq_worker_concurrency == 8

    def test_test_pause_refused_outside_debug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        from modulo.settings import Settings

        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)
        monkeypatch.setenv("SAQ_TEST_PAUSE", "true")
        monkeypatch.delenv("DEBUG", raising=False)
        with pytest.raises(ValidationError):
            Settings()


# ---------------------------------------------------------------------------
# count_active_runs_for_pipeline — include_pending flag
# ---------------------------------------------------------------------------


_COUNTABLE_STATUSES = {"pending", "running", "awaiting_human", "claimed"}


class TestCountActiveRuns:
    def _in_clause_statuses(self, stmt: object) -> set[str]:
        """Extract the statuses bound into the count query's IN clause."""
        statuses: set[str] = set()
        for value in stmt.compile(dialect=postgresql.dialect()).params.values():  # type: ignore[attr-defined]
            if isinstance(value, (list, tuple)):
                statuses.update(v for v in value if v in _COUNTABLE_STATUSES)
            elif value in _COUNTABLE_STATUSES:
                statuses.add(value)
        return statuses

    async def test_include_pending_false_excludes_pending(self) -> None:
        from modulo.db.crud.run import count_active_runs_for_pipeline

        executed: list[tuple[object, object]] = []

        class _Result:
            def scalar_one_or_none(self) -> int:
                return 0

        class _FakeAsyncSession:
            async def execute(self, stmt: object) -> _Result:
                executed.append((stmt, stmt))
                return _Result()

        session = _FakeAsyncSession()
        await count_active_runs_for_pipeline(session, uuid.uuid4(), include_pending=False)  # type: ignore[arg-type]
        statuses = self._in_clause_statuses(executed[0][1])
        assert statuses == {"running", "awaiting_human", "claimed"}

    async def test_include_pending_true_includes_pending(self) -> None:
        from modulo.db.crud.run import count_active_runs_for_pipeline

        executed: list[object] = []

        class _Result:
            def scalar_one_or_none(self) -> int:
                return 0

        class _FakeAsyncSession:
            async def execute(self, stmt: object) -> _Result:
                executed.append(stmt)
                return _Result()

        session = _FakeAsyncSession()
        await count_active_runs_for_pipeline(session, uuid.uuid4(), include_pending=True)  # type: ignore[arg-type]
        statuses = self._in_clause_statuses(executed[0])
        assert statuses == _COUNTABLE_STATUSES

    async def test_exclude_run_id_is_applied(self) -> None:
        stmt_sql: list[str] = []

        class _Result:
            def scalar_one_or_none(self) -> int:
                return 0

        class _FakeAsyncSession:
            async def execute(self, stmt: object) -> _Result:
                stmt_sql.append(_compiled(stmt, render_postcompile=True))
                return _Result()

        from modulo.db.crud.run import count_active_runs_for_pipeline

        rid = uuid.uuid4()
        session = _FakeAsyncSession()
        await count_active_runs_for_pipeline(session, uuid.uuid4(), include_pending=False, exclude_run_id=rid)  # type: ignore[arg-type]
        assert "id !=" in stmt_sql[0] or "runs.id !=" in stmt_sql[0]


# ---------------------------------------------------------------------------
# count_active_sandbox_runs_for_org — only sandbox-agent graph runs count
# ---------------------------------------------------------------------------


class TestCountActiveSandboxRuns:
    async def _count(self, graphs: list[dict[str, Any] | None]) -> int:
        from modulo.db.crud.run import count_active_sandbox_runs_for_org

        class _ScalarResult:
            def scalars(self) -> _ScalarResult:
                return self

            def __iter__(self):
                return iter(graphs)

        class _FakeAsyncSession:
            async def execute(self, stmt: object) -> _ScalarResult:
                return _ScalarResult()

        session = _FakeAsyncSession()
        return await count_active_sandbox_runs_for_org(session, uuid.uuid4())  # type: ignore[arg-type]

    async def test_counts_only_running_sandbox_agent_runs(self) -> None:
        sandbox = {"nodes": [{"id": "s", "node_type": "sandbox_agent"}]}
        plain = {"nodes": [{"id": "a", "node_type": "agent"}]}
        assert await self._count([sandbox, sandbox, plain, plain, None]) == 2

    async def test_zero_when_no_sandbox_graphs(self) -> None:
        plain = {"nodes": [{"id": "a", "node_type": "agent"}]}
        assert await self._count([plain, {"nodes": []}, {}]) == 0

    async def test_zero_when_no_running_runs(self) -> None:
        assert await self._count([]) == 0


# ---------------------------------------------------------------------------
# saq_worker — worker settings structure + fail-closed auth + queue knobs
# ---------------------------------------------------------------------------


def _saq_settings(**overrides: object) -> MagicMock:
    base = {
        "saq_runs_queue": "runs",
        "redis_url": "redis://localhost:6379/0",
        "saq_auth_password": "hunter2",
        "saq_auth_username": "modulo-saq",
        "saq_redis_pool_size": 50,
        "saq_worker_concurrency": 5,
        "modulo_library_sync_interval_seconds": 300,
    }
    base.update(overrides)
    return MagicMock(**base)


class TestSaqWorkerSettings:
    def test_runs_settings_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.core.saq_worker as sw

        monkeypatch.setattr(sw, "get_settings", lambda: _saq_settings())
        settings = sw.runs_settings()
        assert settings["queue"].name == "runs"
        assert settings["concurrency"] == 5
        assert settings["shutdown_grace_period_s"] == 30
        assert settings["cancellation_hard_deadline_s"] == 60
        assert settings["dequeue_timeout"] == 5
        assert settings["timers"] == {"schedule": 5, "worker_info": 89, "sweep": 60, "abort": 1}

    def test_system_settings_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.core.saq_worker as sw

        monkeypatch.setattr(sw, "get_settings", lambda: _saq_settings())
        settings = sw.system_settings()
        assert settings["queue"].name == "system"
        assert settings["concurrency"] == 5
        assert settings["shutdown_grace_period_s"] == 30
        assert settings["cancellation_hard_deadline_s"] == 60
        assert settings["dequeue_timeout"] == 5
        assert settings["timers"] == {"schedule": 5, "worker_info": 89, "sweep": 60, "abort": 1}
        # PR B-2: system crons wired (fire_due_triggers, reconcile, claim-expiry,
        # retention, webhook-dedup, stale recovery) + the cost probe (PR A2)
        # + the hourly missed-fire alert cron (retro item 4)
        # + the hitl_overdue notification sweep.
        cron_names = {c.function.__name__ for c in settings["cron_jobs"]}
        assert cron_names == {
            "analytics_facts_maintenance",
            "fire_due_triggers",
            "dispatcher_reconcile",
            "claim_expiry",
            "hitl_overdue",
            "retention_cleanup",
            "webhook_dedup_cleanup",
            "trigger_events_cleanup",
            "stale_run_recovery",
            "cost_probe",
            "check_missed_fire_alerts_cron",
            "journey_reconcile",
            "metrics_dump",
            "library_sync",
        }
        assert settings["after_process"] is not None

    def test_staging_queue_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.core.saq_worker as sw

        # Staging sets SAQ_RUNS_QUEUE=staging-runs; workers derive their queues.
        monkeypatch.setattr(sw, "get_settings", lambda: _saq_settings(saq_runs_queue="staging-runs"))
        assert sw.staging_runs_settings()["queue"].name == "staging-runs"
        assert sw.staging_system_settings()["queue"].name == "staging-system"

    def test_system_settings_fail_closed_without_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.core.saq_worker as sw

        monkeypatch.setattr(sw, "get_settings", lambda: _saq_settings(saq_auth_password=None))
        with pytest.raises(RuntimeError, match="SAQ_AUTH_PASSWORD"):
            sw.system_settings()

        monkeypatch.setattr(sw, "get_settings", lambda: _saq_settings(saq_auth_username=None))
        with pytest.raises(RuntimeError, match="SAQ_AUTH_USERNAME"):
            sw.system_settings()

    def test_redis_client_knobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.core.saq_worker as sw

        captured: dict[str, object] = {}

        def _fake_from_url(url: str, **kwargs: object) -> object:
            captured["url"] = url
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(sw, "get_settings", lambda: _saq_settings())
        monkeypatch.setattr(sw.aioredis, "from_url", _fake_from_url)
        sw.runs_settings()
        assert captured["socket_connect_timeout"] == 10
        assert captured["socket_keepalive"] is True
        assert captured["max_connections"] == 50


# ---------------------------------------------------------------------------
# Zombie-run protection — fail_run_terminal / zombie_watchdog /
# run_executor_with_watchdog (2026-08-05)
# ---------------------------------------------------------------------------


class TestZombieWatchdog:
    @pytest.mark.asyncio
    async def test_stands_down_on_first_progress(self) -> None:
        first = asyncio.Event()
        first.set()
        exec_task = asyncio.create_task(asyncio.sleep(999))
        with patch.object(pe, "fail_run_terminal", new_callable=AsyncMock) as fail:
            await pe.zombie_watchdog(  # type: ignore[arg-type]
                MagicMock(), "run-1", "org-1", first, exec_task=exec_task, grace_seconds=0.01
            )
        assert not exec_task.cancelled()
        fail.assert_not_awaited()
        exec_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await exec_task

    @pytest.mark.asyncio
    async def test_stands_down_when_executor_done(self) -> None:
        async def _done() -> None:
            return None

        exec_task = asyncio.create_task(_done())
        await exec_task
        with patch.object(pe, "fail_run_terminal", new_callable=AsyncMock) as fail:
            await pe.zombie_watchdog(  # type: ignore[arg-type]
                MagicMock(), "run-1", "org-1", asyncio.Event(), exec_task=exec_task, grace_seconds=0.01
            )
        fail.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fails_stalled_run_after_grace(self) -> None:
        exec_task = asyncio.create_task(asyncio.sleep(999))
        stall = asyncio.Event()
        with patch.object(pe, "fail_run_terminal", new_callable=AsyncMock, return_value=True) as fail:
            await pe.zombie_watchdog(  # type: ignore[arg-type]
                MagicMock(),
                "run-1",
                "org-1",
                asyncio.Event(),
                exec_task=exec_task,
                stall_requested=stall,
                grace_seconds=0.01,
            )
        # Cancellation is requested (may still be unwinding); let it land.
        assert exec_task.cancelling() or exec_task.cancelled()
        fail.assert_awaited_once()
        assert fail.await_args.kwargs["error_code"] == "executor_stalled"
        # The stall signal fires BEFORE fail_run_terminal so the wrapper can
        # tell a watchdog-initiated cancellation from a worker shutdown.
        assert stall.is_set()
        with contextlib.suppress(asyncio.CancelledError):
            await exec_task
        assert exec_task.cancelled()


class TestRunExecutorWithWatchdog:
    @pytest.mark.asyncio
    async def test_runs_execute_fn_and_returns_complete(self) -> None:
        executor = MagicMock()
        ran: list[str] = []

        async def _execute() -> object:
            ran.append("executed")
            return SimpleNamespace(status="complete")

        engine = MagicMock()
        with (
            patch.object(
                pe,
                "get_settings",
                return_value=MagicMock(saq_setup_grace_seconds=0.05, saq_node_default_timeout_seconds=1200),
            ),
            patch.object(pe, "heartbeat_loop", new_callable=AsyncMock),
            patch.object(pe, "fail_run_terminal", new_callable=AsyncMock),
        ):
            result = await pe.run_executor_with_watchdog(  # type: ignore[arg-type]
                engine,
                run_id=str(uuid.uuid4()),
                org_id=str(uuid.uuid4()),
                executor=executor,
                job=None,
                execute_fn=_execute,
            )
        assert result == {"status": "complete"}
        assert ran == ["executed"]
        # on_first_progress was wired to an asyncio.Event.set callable.
        assert callable(executor.on_first_progress)

    @pytest.mark.asyncio
    async def test_watchdog_cancels_hung_executor_and_fails_run(self) -> None:
        executor = MagicMock()
        started: list[str] = []

        async def _hang() -> None:
            started.append("started")
            await asyncio.sleep(999)

        engine = MagicMock()
        with (
            patch.object(
                pe,
                "get_settings",
                return_value=MagicMock(saq_setup_grace_seconds=0.05, saq_node_default_timeout_seconds=1200),
            ),
            patch.object(pe, "heartbeat_loop", new_callable=AsyncMock),
            patch.object(pe, "fail_run_terminal", new_callable=AsyncMock, return_value=True) as fail,
            patch.object(pe, "_read_run_status", new_callable=AsyncMock, return_value="failed") as read_status,
        ):
            result = await pe.run_executor_with_watchdog(  # type: ignore[arg-type]
                engine,
                run_id=str(uuid.uuid4()),
                org_id=str(uuid.uuid4()),
                executor=executor,
                job=None,
                execute_fn=_hang,
            )
        assert result == {"status": "failed"}
        assert started == ["started"]
        fail.assert_awaited_once()
        assert fail.await_args.kwargs["error_code"] == "executor_stalled"
        # The run row was terminal-failed by the watchdog → outcome is failed.
        read_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_watchdog_fail_write_completes_despite_cancellation_race(self) -> None:
        """Regression: watchdog cancels the executor while still mid-``fail_run_terminal``.

        The CancelledError lands in ``run_executor_with_watchdog`` ~1 event-loop
        step after ``exec_task.cancel()`` — while the watchdog is still inside
        its ``fail_run_terminal`` DB transaction. The wrapper must await the
        watchdog to completion (so the terminal write commits) and swallow the
        cancellation, NOT cancel the watchdog mid-write and leak a
        ``CancelledError`` into the SAQ worker. ``fail_run_terminal`` here is a
        real-async substitute with real ``await`` steps so the cancellation
        lands while the write is in flight.
        """
        executor = MagicMock()

        async def _hang() -> None:
            await asyncio.sleep(999)

        failed: list[str] = []

        async def _slow_fail(*args: Any, **kwargs: Any) -> bool:
            for _ in range(5):
                await asyncio.sleep(0.001)
            failed.append(kwargs["error_code"])
            return True

        engine = MagicMock()
        for _ in range(10):
            with (
                patch.object(
                    pe,
                    "get_settings",
                    return_value=MagicMock(saq_setup_grace_seconds=0.02, saq_node_default_timeout_seconds=1200),
                ),
                patch.object(pe, "heartbeat_loop", new_callable=AsyncMock),
                patch.object(pe, "fail_run_terminal", _slow_fail),
                patch.object(pe, "_read_run_status", new_callable=AsyncMock, return_value="failed"),
            ):
                result = await pe.run_executor_with_watchdog(  # type: ignore[arg-type]
                    engine,
                    run_id=str(uuid.uuid4()),
                    org_id=str(uuid.uuid4()),
                    executor=executor,
                    job=None,
                    execute_fn=_hang,
                )
        assert result == {"status": "failed"}
        assert failed == ["executor_stalled"] * 10

    @pytest.mark.asyncio
    async def test_worker_shutdown_cancellation_reraises(self) -> None:
        executor = MagicMock()

        async def _hang() -> None:
            await asyncio.sleep(999)

        engine = MagicMock()
        with (
            patch.object(
                pe,
                "get_settings",
                return_value=MagicMock(saq_setup_grace_seconds=60, saq_node_default_timeout_seconds=1200),
            ),
            patch.object(pe, "heartbeat_loop", new_callable=AsyncMock),
            patch.object(pe, "fail_run_terminal", new_callable=AsyncMock, return_value=True) as fail,
        ):
            task = asyncio.create_task(
                pe.run_executor_with_watchdog(  # type: ignore[arg-type]
                    engine,
                    run_id=str(uuid.uuid4()),
                    org_id=str(uuid.uuid4()),
                    executor=executor,
                    job=None,
                    execute_fn=_hang,
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        # Worker shutdown is NOT a watchdog stall — the run is never terminal-failed.
        fail.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_executor_exception_terminal_fails_with_executor_failed(self) -> None:
        """A generic executor exception is terminal-failed with
        ``executor_failed`` (token-guarded) and the outcome is ``failed`` — never
        a silent wrong-success completion (dist/runtime-core A2)."""
        executor = MagicMock()

        async def _boom() -> None:
            raise RuntimeError("boom")

        engine = MagicMock()
        with (
            patch.object(
                pe,
                "get_settings",
                return_value=MagicMock(saq_setup_grace_seconds=0.05, saq_node_default_timeout_seconds=1200),
            ),
            patch.object(pe, "heartbeat_loop", new_callable=AsyncMock),
            patch.object(pe, "fail_run_terminal", new_callable=AsyncMock) as fail,
        ):
            result = await pe.run_executor_with_watchdog(  # type: ignore[arg-type]
                engine,
                run_id=str(uuid.uuid4()),
                org_id=str(uuid.uuid4()),
                executor=executor,
                job=None,
                execute_fn=_boom,
            )
        assert result == {"status": "failed"}
        fail.assert_awaited_once()
        assert fail.await_args.kwargs["error_code"] == "executor_failed"
        assert fail.await_args.kwargs["claim_token"] is None

    @pytest.mark.asyncio
    async def test_node_hooks_fire_for_streamed_events(self) -> None:
        """Prove-the-fix: the executor's per-node start/completion callbacks are
        actually wired into the node-deadline watchdog (not silently dropped).

        ``run_executor_with_watchdog`` wires ``executor.on_node_started`` /
        ``executor.on_node_completed`` to the watchdog's events. This test drives
        those callbacks from a fake stream and asserts the watchdog observed them
        (a stalled node that started but never completed is failed with
        ``node_deadline_exceeded``). If the event-type string ever diverged from
        the wiring, the watchdog would silently never fire — so we pin the
        observable wiring here.
        """
        executor = MagicMock()
        executor._node_timeouts = {}  # real dict so the wiring computes a real deadline
        started: list[str] = []
        completed: list[str] = []

        async def _execute() -> object:
            # Mimic the real streamed events: a node starts, then completes.
            executor.on_first_progress()  # type: ignore[attr-defined]
            executor.on_node_started("n1")  # type: ignore[attr-defined]
            started.append("n1")
            await asyncio.sleep(0.02)
            executor.on_node_completed("n1")  # type: ignore[attr-defined]
            completed.append("n1")
            return SimpleNamespace(status="complete")

        engine = MagicMock()
        with (
            patch.object(
                pe,
                "get_settings",
                return_value=MagicMock(saq_setup_grace_seconds=60, saq_node_default_timeout_seconds=1200),
            ),
            patch.object(pe, "heartbeat_loop", new_callable=AsyncMock),
            patch.object(pe, "fail_run_terminal", new_callable=AsyncMock),
        ):
            result = await pe.run_executor_with_watchdog(  # type: ignore[arg-type]
                engine,
                run_id=str(uuid.uuid4()),
                org_id=str(uuid.uuid4()),
                executor=executor,
                job=None,
                execute_fn=_execute,
            )
        assert result == {"status": "complete"}
        # The hooks fired for the real (simulated) streamed events.
        assert started == ["n1"]
        assert completed == ["n1"]

    @pytest.mark.asyncio
    async def test_node_deadline_exceeded_fails_stalled_node(self) -> None:
        """Prove-the-fix at the ``run_executor_with_watchdog`` level: a node that
        starts via the wired ``on_node_started`` hook but never completes is
        terminal-failed with ``node_deadline_exceeded`` — the actual observable
        effect of this watchdog (FAR-369). Exercises the full wiring from the
        streamed node_started event through to the deadline failure.
        """
        executor = MagicMock()
        executor._node_timeouts = {"n1": 0.05}  # short deadline so the test is fast

        async def _hang() -> None:
            executor.on_first_progress()  # type: ignore[attr-defined]
            executor.on_node_started("n1")  # type: ignore[attr-defined]
            await asyncio.sleep(999)  # node never completes -> half-alive stall

        engine = MagicMock()
        with (
            patch.object(
                pe,
                "get_settings",
                return_value=MagicMock(saq_setup_grace_seconds=60, saq_node_default_timeout_seconds=1200),
            ),
            patch.object(pe, "heartbeat_loop", new_callable=AsyncMock),
            patch.object(pe, "fail_run_terminal", new_callable=AsyncMock, return_value=True) as fail,
            patch.object(pe, "_read_run_status", new_callable=AsyncMock, return_value="failed") as read_status,
        ):
            result = await pe.run_executor_with_watchdog(  # type: ignore[arg-type]
                engine,
                run_id=str(uuid.uuid4()),
                org_id=str(uuid.uuid4()),
                executor=executor,
                job=None,
                execute_fn=_hang,
            )
        assert result == {"status": "failed"}
        fail.assert_awaited_once()
        assert fail.await_args.kwargs["error_code"] == "node_deadline_exceeded"
        # The zombie watchdog (executor_stalled) must NOT fire — the node DID
        # start, so only the absolute node-deadline watchdog should win.
        assert fail.await_args.kwargs["error_code"] != "executor_stalled"
        read_status.assert_awaited_once()


# ---------------------------------------------------------------------------
# FAR-162 (P6') — pipeline_execution facts helper
# ---------------------------------------------------------------------------


class TestRecordFactForTerminalFailedRunHelper:
    """The pipeline_execution wrapper opens its own RLS-scoped session AFTER a
    raw terminal write commits, re-selects the Run ORM, and records the daily
    fact via the shared analytics helper — None-guarded and fail-open."""

    def _factory_and_session(self) -> tuple[MagicMock, AsyncMock]:
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        factory = MagicMock(return_value=session)
        return factory, session

    @pytest.mark.asyncio
    async def test_records_fact_for_reselected_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run_id = str(uuid.uuid4())
        org_id = str(uuid.uuid4())
        fake_run = MagicMock()
        factory, session = self._factory_and_session()
        record_facts = AsyncMock()
        monkeypatch.setattr(pe, "async_sessionmaker", lambda *a, **k: factory)
        monkeypatch.setattr(pe, "set_rls_org", AsyncMock())
        with (
            patch("modulo.core.analytics.record_fact_for_terminal_failed_run", record_facts),
            patch.object(pe, "get_run", new_callable=AsyncMock, return_value=fake_run),
        ):
            await pe._record_fact_for_terminal_failed_run(MagicMock(), run_id, org_id)  # type: ignore[arg-type]

        record_facts.assert_awaited_once_with(session, fake_run)

    @pytest.mark.asyncio
    async def test_missing_run_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run_id = str(uuid.uuid4())
        org_id = str(uuid.uuid4())
        factory, _session = self._factory_and_session()
        record_facts = AsyncMock()
        monkeypatch.setattr(pe, "async_sessionmaker", lambda *a, **k: factory)
        monkeypatch.setattr(pe, "set_rls_org", AsyncMock())
        with (
            patch("modulo.core.analytics.record_fact_for_terminal_failed_run", record_facts),
            patch.object(pe, "get_run", new_callable=AsyncMock, return_value=None),
        ):
            await pe._record_fact_for_terminal_failed_run(MagicMock(), run_id, org_id)  # type: ignore[arg-type]

        record_facts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failure_is_fail_open(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        run_id = str(uuid.uuid4())
        org_id = str(uuid.uuid4())
        fake_run = MagicMock()
        factory, _session = self._factory_and_session()
        monkeypatch.setattr(pe, "async_sessionmaker", lambda *a, **k: factory)
        monkeypatch.setattr(pe, "set_rls_org", AsyncMock())

        async def _boom(_s: object, _r: object) -> None:
            raise RuntimeError("facts boom")

        with (
            patch("modulo.core.analytics.record_fact_for_terminal_failed_run", new=_boom),
            patch.object(pe, "get_run", new_callable=AsyncMock, return_value=fake_run),
            caplog.at_level("WARNING", logger="modulo.core.pipeline_execution"),
        ):
            await pe._record_fact_for_terminal_failed_run(MagicMock(), run_id, org_id)  # type: ignore[arg-type]

        assert any("terminal_failed_facts_failed" in m for m in caplog.messages)


class TestResumeRun:
    """Core ``resume_run`` — claims with a fresh token and ignores the stale
    ``claim_token`` kwarg SAQ passes back on a retry (PR #1003)."""

    @pytest.mark.asyncio
    async def test_accepts_stale_claim_token_and_claims_with_fresh_token(self) -> None:
        job = MagicMock()
        job.update = AsyncMock()
        engine = MagicMock()

        async def _pass_through(aeng: Any, **kwargs: Any) -> dict[str, str]:
            await kwargs["execute_fn"]()
            return {"status": "complete"}

        with (
            patch.object(pe, "claim_resume_run_async", new_callable=AsyncMock, return_value="tok-fresh") as claim,
            patch.object(pe, "load_and_setup", new_callable=AsyncMock) as load,
            patch.object(pe, "mark_complete", new_callable=AsyncMock) as complete,
            patch.object(pe, "run_executor_with_watchdog", side_effect=_pass_through) as watchdog,
        ):
            run = MagicMock()
            run.input_payload = {"a": 1}
            executor = MagicMock()
            executor.resume = AsyncMock()
            load.return_value = (run, executor)
            result = await pe.resume_run(
                async_engine=engine,  # type: ignore[arg-type]
                run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f",
                org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70",
                resume_data={"action": "approved"},
                job=job,
                claim_cap=20,
                claim_token="stale-token-from-previous-attempt",
            )

        assert result == {"status": "complete"}
        # The stale kwarg is NOT forwarded — the claim function generates its own
        # fresh token, which is what threads through the resume and is stamped.
        claim.assert_awaited_once()
        assert "claim_token" not in claim.await_args.kwargs
        executor.resume.assert_awaited_once()
        assert executor.resume.await_args.kwargs["claim_token"] == "tok-fresh"
        complete.assert_awaited_once()
        assert complete.await_args.kwargs["claim_token"] == "tok-fresh"
        watchdog.assert_awaited_once()
        assert watchdog.await_args.kwargs["claim_token"] == "tok-fresh"
        job.update.assert_awaited_once()
        assert job.update.await_args.kwargs["kwargs"]["claim_token"] == "tok-fresh"

    @pytest.mark.asyncio
    async def test_stale_claim_token_not_claimed_returns_early(self) -> None:
        with (
            patch.object(pe, "claim_resume_run_async", new_callable=AsyncMock, return_value=None),
            patch.object(pe, "mark_complete", new_callable=AsyncMock) as complete,
        ):
            result = await pe.resume_run(
                async_engine=MagicMock(),  # type: ignore[arg-type]
                run_id="7b2f2e7e-3a0a-4f5c-9a0e-1a2b3c4d5e6f",
                org_id="8c3f3f8f-4b0b-4f6d-9b1f-2b3c4d5e6f70",
                claim_cap=20,
                claim_token="stale-token-from-previous-attempt",
            )
        assert result == {"status": "not_claimed"}
        complete.assert_not_awaited()


class TestNodeDeadlineWatchdog:
    """Absolute node-deadline watchdog (FAR-369): fails a node that does not
    COMPLETE within its configured timeout_seconds, independent of idle/activity.
    This catches the half-alive SSE stall that defeats the idle-watchdog.
    """

    @pytest.mark.asyncio
    async def test_node_completing_within_deadline_is_not_failed(self) -> None:
        exec_task = asyncio.create_task(asyncio.sleep(999))  # stays running
        started = asyncio.Event()
        completed = asyncio.Event()
        done = asyncio.Event()
        deadlines: dict[str, tuple[float, int]] = {}
        with patch.object(pe, "fail_run_terminal", new_callable=AsyncMock) as fail:
            wd = asyncio.create_task(
                pe.node_deadline_watchdog(  # type: ignore[arg-type]
                    MagicMock(),
                    "run-1",
                    "org-1",
                    {"n1": 1200},
                    exec_task=exec_task,
                    stall_requested=asyncio.Event(),
                    node_started_event=started,
                    node_completed_event=completed,
                    run_done_event=done,
                    node_deadlines=deadlines,
                    default_timeout=1200,
                )
            )
            # Node starts, then completes well within its 1200s deadline.
            deadlines["n1"] = (time.monotonic() + 1200, 1200)
            started.set()
            await asyncio.sleep(0.02)
            completed.set()
            await asyncio.sleep(0.05)
            # Mark the run finished so the watchdog stands down cleanly.
            done.set()
            await wd
        fail.assert_not_awaited()
        assert not exec_task.cancelled()

    @pytest.mark.asyncio
    async def test_node_stalling_past_deadline_is_failed(self) -> None:
        exec_task = asyncio.create_task(asyncio.sleep(999))
        started = asyncio.Event()
        completed = asyncio.Event()
        done = asyncio.Event()
        deadlines: dict[str, tuple[float, int]] = {}
        stall = asyncio.Event()
        with patch.object(pe, "fail_run_terminal", new_callable=AsyncMock, return_value=True) as fail:
            wd = asyncio.create_task(
                pe.node_deadline_watchdog(  # type: ignore[arg-type]
                    MagicMock(),
                    "run-1",
                    "org-1",
                    {"n1": 0.05},
                    exec_task=exec_task,
                    stall_requested=stall,
                    node_started_event=started,
                    node_completed_event=completed,
                    run_done_event=done,
                    node_deadlines=deadlines,
                    default_timeout=0.05,
                )
            )
            # Node starts but never completes -> deadline exceeded.
            deadlines["n1"] = (time.monotonic() + 0.05, 0.05)
            started.set()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(wd, timeout=2.0)
        fail.assert_awaited_once()
        assert fail.await_args.kwargs["error_code"] == "node_deadline_exceeded"
        # The stall signal fires BEFORE fail_run_terminal so the wrapper can tell
        # a watchdog-initiated cancellation from a worker shutdown.
        assert stall.is_set()
        assert exec_task.cancelled()

    @pytest.mark.asyncio
    async def test_parallel_fanout_stalled_sibling_is_failed(self) -> None:
        """Regression for the parallel-superstep deadline-evasion gap (review).

        A stalled node A must be failed even while a sibling B starts and
        completes alongside it (LangGraph runs siblings concurrently within a
        shared superstep). The single-track ``current_node`` model abandoned A's
        deadline; the per-node ``node_deadlines`` dict must keep tracking it.
        """
        exec_task = asyncio.create_task(asyncio.sleep(999))  # stays running
        started = asyncio.Event()
        completed = asyncio.Event()
        done = asyncio.Event()
        deadlines: dict[str, tuple[float, int]] = {}
        stall = asyncio.Event()
        with patch.object(pe, "fail_run_terminal", new_callable=AsyncMock, return_value=True) as fail:
            wd = asyncio.create_task(
                pe.node_deadline_watchdog(  # type: ignore[arg-type]
                    MagicMock(),
                    "run-1",
                    "org-1",
                    {"A": 0.05, "B": 1200},
                    exec_task=exec_task,
                    stall_requested=stall,
                    node_started_event=started,
                    node_completed_event=completed,
                    run_done_event=done,
                    node_deadlines=deadlines,
                    default_timeout=1200,
                )
            )
            # A starts and stalls (short deadline).
            deadlines["A"] = (time.monotonic() + 0.05, 0.05)
            started.set()
            await asyncio.sleep(0.01)
            # B starts (parallel fan-out) and completes — must NOT abandon A.
            deadlines["B"] = (time.monotonic() + 1200, 1200)
            started.set()
            await asyncio.sleep(0.01)
            completed.set()
            del deadlines["B"]
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(wd, timeout=2.0)
        fail.assert_awaited_once()
        assert fail.await_args.kwargs["error_code"] == "node_deadline_exceeded"
        assert fail.await_args.kwargs["error_detail"].find("A") != -1
        assert stall.is_set()
        assert exec_task.cancelled()

    @pytest.mark.asyncio
    async def test_does_not_fail_already_terminal_run(self) -> None:
        # A run whose executor task is already done must never be failed by the
        # watchdog, even if a node is "running" past its deadline.
        exec_task = asyncio.create_task(asyncio.sleep(0))
        await exec_task  # ensure completion
        started = asyncio.Event()
        completed = asyncio.Event()
        done = asyncio.Event()
        deadlines: dict[str, tuple[float, int]] = {}
        started.set()
        deadlines["n1"] = (time.monotonic() + 1200, 1200)
        with patch.object(pe, "fail_run_terminal", new_callable=AsyncMock) as fail:
            await pe.node_deadline_watchdog(  # type: ignore[arg-type]
                MagicMock(),
                "run-1",
                "org-1",
                {"n1": 1200},
                exec_task=exec_task,
                stall_requested=asyncio.Event(),
                node_started_event=started,
                node_completed_event=completed,
                run_done_event=done,
                node_deadlines=deadlines,
                default_timeout=1200,
            )
        fail.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_done_event_stands_down_without_failing(self) -> None:
        # If the run is marked done (completed normally) the watchdog must not
        # fail an in-flight node's stall.
        exec_task = asyncio.create_task(asyncio.sleep(999))
        started = asyncio.Event()
        completed = asyncio.Event()
        done = asyncio.Event()
        deadlines: dict[str, tuple[float, int]] = {}
        started.set()
        deadlines["n1"] = (time.monotonic() + 1200, 1200)
        done.set()  # run already finished
        with patch.object(pe, "fail_run_terminal", new_callable=AsyncMock) as fail:
            await pe.node_deadline_watchdog(  # type: ignore[arg-type]
                MagicMock(),
                "run-1",
                "org-1",
                {"n1": 1200},
                exec_task=exec_task,
                stall_requested=asyncio.Event(),
                node_started_event=started,
                node_completed_event=completed,
                run_done_event=done,
                node_deadlines=deadlines,
                default_timeout=1200,
            )
        fail.assert_not_awaited()
        assert not exec_task.cancelled()


class TestSetRlsOrg:
    async def test_sqlite_dialect_sets_session_info(self) -> None:
        """On non-Postgres backends RLS context is stored in session.info[org_id]."""
        session = MagicMock()
        session.info = {}
        bind_mock = MagicMock()
        bind_mock.dialect.name = "sqlite"
        session.get_bind = MagicMock(return_value=bind_mock)
        org_id = uuid.uuid4()

        await pe.set_rls_org(session, org_id)

        assert session.info["org_id"] == org_id

    async def test_postgres_dialect_sets_config(self) -> None:
        """On Postgres, RLS context is applied via SET LOCAL set_config."""
        session = MagicMock()
        session.info = {}
        bind_mock = MagicMock()
        bind_mock.dialect.name = "postgresql"
        session.get_bind = MagicMock(return_value=bind_mock)
        session.execute = AsyncMock()
        org_id = uuid.uuid4()

        await pe.set_rls_org(session, org_id)

        session.execute.assert_awaited_once()
        stmt, params = session.execute.await_args.args
        assert "set_config" in str(stmt).lower()
        assert params == {"oid": str(org_id)}
