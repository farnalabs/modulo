"""Unit tests for modulo.core.pipeline_execution.

Tests are mock/fake based — no Postgres required. Real Postgres concurrency
behaviour (two concurrent claims -> exactly one) lives in
``tests/integration/test_pipeline_execution.py`` (marked ``integration``).
"""

from __future__ import annotations

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


def _make_settings(**overrides: object) -> MagicMock:
    """Mock Settings with the SAQ/legacy claim staleness plumbing values."""
    base = {
        "run_claim_stale_seconds": 450,
        "saq_never_dispatched_window": 300,
        "saq_worker_lost_window": 600,
        "saq_job_heartbeat": 300,
        "run_heartbeat_seconds": 30,
        "saq_worker_db_pool_size": 2,
        "saq_redis_pool_size": 20,
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
        assert await pe.claim_run_async(engine, "run-1", "org-1") is True  # type: ignore[arg-type]
        assert calls[0]["params"]["stale_seconds"] == 450  # type: ignore[index]

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
        assert await pe.claim_run_async(engine, "run-1", "org-1") is False  # type: ignore[arg-type]


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
    async def test_writes_complete_enum(self) -> None:
        run = SimpleNamespace(status="running", completed_at=None)
        with (
            patch.object(pe, "get_run", AsyncMock(return_value=run)),
            patch.object(pe, "async_sessionmaker") as mock_factory,
            patch.object(pe, "set_rls_org", AsyncMock()),
        ):
            session = MagicMock()
            session.begin.return_value.__aenter__ = AsyncMock(return_value=session)
            session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
            factory = MagicMock()
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_factory.return_value = factory

            engine = MagicMock()
            await pe.mark_complete(engine, str(uuid.uuid4()), str(uuid.uuid4()))  # type: ignore[arg-type]

        assert run.status == "complete"
        assert run.completed_at is not None

    async def test_does_not_overwrite_terminal_status(self) -> None:
        run = SimpleNamespace(status="failed")
        with (
            patch.object(pe, "get_run", AsyncMock(return_value=run)),
            patch.object(pe, "async_sessionmaker") as mock_factory,
            patch.object(pe, "set_rls_org", AsyncMock()),
        ):
            session = MagicMock()
            session.begin.return_value.__aenter__ = AsyncMock(return_value=session)
            session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
            factory = MagicMock()
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_factory.return_value = factory

            engine = MagicMock()
            await pe.mark_complete(engine, str(uuid.uuid4()), str(uuid.uuid4()))  # type: ignore[arg-type]

        assert run.status == "failed"
        assert not hasattr(run, "completed_at")

    async def test_noop_when_run_missing(self) -> None:
        run_id = str(uuid.uuid4())
        org_id = str(uuid.uuid4())
        with (
            patch.object(pe, "get_run", AsyncMock(return_value=None)) as mock_get_run,
            patch.object(pe, "async_sessionmaker") as mock_factory,
            patch.object(pe, "set_rls_org", AsyncMock()) as mock_set_rls,
        ):
            session = MagicMock()
            session.begin.return_value.__aenter__ = AsyncMock(return_value=session)
            session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
            factory = MagicMock()
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_factory.return_value = factory

            engine = MagicMock()
            await pe.mark_complete(engine, run_id, org_id)  # type: ignore[arg-type]

        # The run lookup is still performed with the right identifiers…
        mock_set_rls.assert_awaited_once()
        mock_get_run.assert_awaited_once()
        args = mock_get_run.await_args.args
        assert args[1] == uuid.UUID(run_id)


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

    async def test_heartbeat_loop_uses_configured_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        heartbeat_mock = AsyncMock()
        monkeypatch.setattr(pe, "heartbeat_once", heartbeat_mock)
        monkeypatch.setattr(pe.asyncio, "sleep", AsyncMock(side_effect=[None, KeyboardInterrupt()]))
        engine = MagicMock()

        with pytest.raises(KeyboardInterrupt):
            await pe.heartbeat_loop(engine, "run-1", "org-1", interval_seconds=45)  # type: ignore[arg-type]

        assert pe.asyncio.sleep.await_count == 2
        assert pe.asyncio.sleep.await_args.args[0] == 45  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Stale-run recovery sweep — legacy windows match today's beat-sweep values
# ---------------------------------------------------------------------------


class TestStaleRunRecoverySweep:
    async def test_uses_legacy_300_600_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        statements: list[str] = []

        class _AsyncResult:
            rowcount = 0

            def all(self) -> list[Any]:
                return []

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
                statements.append(str(stmt))
                return _AsyncResult()

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
        assert len(statements) == 4
        never_sql = statements[0]
        stranded_sql = statements[1]
        capacity_sql = statements[2]
        lost_sql = statements[3]
        assert "never_dispatched" in never_sql
        assert "RETURNING id, organisation_id" in stranded_sql
        assert "org_capacity_limited" in stranded_sql
        assert "pipeline_capacity" in stranded_sql
        assert "capacity_timeout" in capacity_sql
        assert "worker_lost" in lost_sql
        assert ":nd_window" in never_sql
        assert ":redispatch_ttl" in stranded_sql
        assert ":fail_ttl" in stranded_sql
        assert ":ttl" in capacity_sql
        assert ":wl_window" in lost_sql
        # never_dispatched must not kill reason-marked capacity-blocked runs.
        assert "org_capacity_limited" in never_sql
        assert "pipeline_capacity" in never_sql

    async def test_explicit_windows_override_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        params_seen: list[dict[str, object]] = []

        class _AsyncResult:
            rowcount = 1

            def all(self) -> list[Any]:
                return []

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
                params_seen.append(params or {})
                return _AsyncResult()

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
        assert params_seen[0]["nd_window"] == 300
        assert params_seen[1]["redispatch_ttl"] == pe._STRANDED_REDISPATCH_TTL_MINUTES
        assert params_seen[1]["fail_ttl"] == pe.CAPACITY_TIMEOUT_TTL_MINUTES
        assert params_seen[2]["ttl"] == pe.CAPACITY_TIMEOUT_TTL_MINUTES
        assert params_seen[3]["wl_window"] == 900

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
                if "RETURNING id, organisation_id" in str(stmt):
                    return _AsyncResult(rowcount=1, rows=[_Row()])
                return _AsyncResult()

        class _AsyncEngine:
            def connect(self) -> _AsyncConn:
                return _AsyncConn()

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
                return []

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
                return _AsyncResult()

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
                return []

        class _AsyncConn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
                params_seen.append(params or {})
                return _AsyncResult()

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
        assert params_seen[1]["fail_ttl"] == pe.CAPACITY_TIMEOUT_TTL_MINUTES
        redispatch_mock.assert_not_awaited()

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
    "SAQ_E2B_IDEMPOTENCY",
    "SAQ_TEST_PAUSE",
    "SAQ_REENQUEUE_WINDOW",
    "SAQ_NEVER_DISPATCHED_WINDOW",
    "SAQ_WORKER_LOST_WINDOW",
    "SAQ_WORKER_DB_POOL_SIZE",
    "SAQ_REDIS_POOL_SIZE",
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
        assert s.saq_e2b_idempotency is True
        assert s.saq_test_pause is False
        assert s.saq_reenqueue_window == 600
        assert s.saq_never_dispatched_window == 300
        assert s.saq_worker_lost_window == 600
        assert s.saq_worker_db_pool_size == 10
        assert s.saq_redis_pool_size == 20

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RUN_CLAIM_STALE_SECONDS", "500")
        monkeypatch.setenv("SAQ_REDIS_POOL_SIZE", "8")
        s = self._settings(monkeypatch)
        assert s.run_claim_stale_seconds == 500
        assert s.saq_redis_pool_size == 8

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


_COUNTABLE_STATUSES = {"pending", "running", "awaiting_human", "claimed", "waiting_for_lock"}


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
        assert statuses == {"running", "awaiting_human", "claimed", "waiting_for_lock"}

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
        "saq_redis_pool_size": 20,
    }
    base.update(overrides)
    return MagicMock(**base)


class TestSaqWorkerSettings:
    def test_runs_settings_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.core.saq_worker as sw

        monkeypatch.setattr(sw, "get_settings", lambda: _saq_settings())
        settings = sw.runs_settings()
        assert settings["queue"].name == "runs"
        assert settings["concurrency"] == 20
        assert settings["shutdown_grace_period_s"] == 30
        assert settings["cancellation_hard_deadline_s"] == 60
        assert settings["dequeue_timeout"] == 5
        assert settings["timers"] == {"schedule": 5, "worker_info": 89, "sweep": 60, "abort": 1}

    def test_system_settings_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.core.saq_worker as sw

        monkeypatch.setattr(sw, "get_settings", lambda: _saq_settings())
        settings = sw.system_settings()
        assert settings["queue"].name == "system"
        assert settings["concurrency"] == 20
        assert settings["shutdown_grace_period_s"] == 30
        assert settings["cancellation_hard_deadline_s"] == 60
        assert settings["dequeue_timeout"] == 5
        assert settings["timers"] == {"schedule": 5, "worker_info": 89, "sweep": 60, "abort": 1}
        # PR B-2: system crons wired (fire_due_triggers, reconcile, claim-expiry,
        # retention, webhook-dedup, stale recovery).
        cron_names = {c.function.__name__ for c in settings["cron_jobs"]}
        assert cron_names == {
            "fire_due_triggers",
            "dispatcher_reconcile",
            "claim_expiry",
            "retention_cleanup",
            "webhook_dedup_cleanup",
            "stale_run_recovery",
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
        assert captured["max_connections"] == 20
