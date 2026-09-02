"""SAQ recovery + safety-net tests (plan §Verification / F3a) — fully mocked.

These tests exercise the at-most-once safety net and recovery invariants WITHOUT
live Redis/Postgres (fake sessions, fake Redis clients, patched dispatch). They
complement the real-infra tests in this directory (``test_dispatcher_reconcile``,
``test_fire_due_triggers``) with deterministic unit-level coverage:

  * retry off-by-one: ``retries=N`` means N total attempts (N-1 retries), and
    the after_process hook only fails a run when retries are truly exhausted.
  * no-SAQ-eviction re-dispatch: an evicted job hash is re-dispatched with a
    FRESH key_suffix, never via DEL/ZREM/LREM of SAQ internals (B2).
  * claim-token fence: heartbeat/mark_complete/fail_run_terminal use ATOMIC
    token-guarded UPDATEs — a superseded token raises ClaimSupersededError and
    the SAQ job hash is untouched.
  * event-loop-stall refusal: a live-heartbeat run refuses the next claim.
  * fire_due_triggers two-worker single-fire: the atomic next_fire_at advance
    produces exactly ONE fire job per epoch under concurrent invocation.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

import modulo.core.pipeline_execution as pe
from modulo.core import cron_helpers as ch

_ORG = uuid.uuid4()
_RUN_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# retry off-by-one — retries=N is N TOTAL attempts (N-1 retries)
# ---------------------------------------------------------------------------


class TestRetryOffByOne:
    def test_saq_retries_mean_total_attempts(self) -> None:
        """SAQ's ``retries`` knob is the TOTAL attempt budget (N-1 retries)."""
        from saq.job import Job

        # retryable only while attempts < retries — so retries=N admits exactly
        # N attempts, then the job is permanently FAILED (no Nth retry).
        assert Job(function="f", retries=5, attempts=5).retryable is False
        assert Job(function="f", retries=5, attempts=4).retryable is True
        assert Job(function="f", retries=1, attempts=1).retryable is False  # single attempt, zero retries

    def test_after_process_only_fails_run_when_retries_exhausted(self) -> None:
        from saq import Status

        from modulo.core.error_tracking.saq_hooks import _classify

        # FAILED (retries exhausted) execute_run job -> fail the run.
        out = _classify(
            "modulo.core.saq_worker.execute_run",
            Status.FAILED,
            "boom",
            {"run_id": "r-1", "org_id": "o-1"},
        )
        assert out["action"] == "fail_run"
        assert out["run_id"] == "r-1"

        # A retry-in-progress job (QUEUED/ACTIVE) must be a NOOP — never fail a
        # run mid-retry. This is the off-by-one guard: the run is failed only
        # once all N attempts are exhausted, never after attempt 1.
        for mid_retry in (Status.QUEUED, Status.ACTIVE, Status.NEW, Status.COMPLETE):
            assert (
                _classify("modulo.core.saq_worker.execute_run", mid_retry, None, {"run_id": "r-1"})["action"] == "noop"
            )

        # fire_* failure -> ingest_error (no run state).
        out2 = _classify("modulo.core.saq_worker.fire_cron_trigger", Status.FAILED, "x", {"org_id": "o-1"})
        assert out2["action"] == "ingest_error"


# ---------------------------------------------------------------------------
# partial-eviction repair — reconcile's DEL/ZREM/LREM/enqueue sequence
# ---------------------------------------------------------------------------


class TestNoSaqEvictionRedispatch:
    async def test_evicted_job_redispatched_without_saq_eviction(self) -> None:
        """A run whose SAQ job hash is missing is re-dispatched WITHOUT touching
        SAQ-internal structures (B2): no DEL/ZREM/LREM, no SAQ list reads. The
        re-dispatch carries a FRESH key_suffix so SAQ key dedupe never suppresses
        the recovery enqueue; the atomic claim UPDATE is the real dedupe."""
        run_id = str(_RUN_ID)
        row = SimpleNamespace(
            id=uuid.UUID(run_id),
            pipeline_id=uuid.uuid4(),
            status="running",
            dispatched_at=None,
            heartbeat_at=None,
            node_token_usage={},
            outputs_json={},
            started_at=None,
            dispatcher="saq",
            enqueue_failed_at=None,
        )

        redis = MagicMock()
        redis.delete = AsyncMock(return_value=1)
        redis.zrem = AsyncMock(return_value=1)
        redis.lrem = AsyncMock(return_value=1)
        redis.aclose = AsyncMock()

        class _Result:
            def __init__(self, rows: list[Any] | None = None, *, scalars: list[Any] | None = None) -> None:
                self._rows = rows or []
                self._scalars = scalars or []

            def scalars(self) -> list[Any]:
                return self._scalars

            def all(self) -> list[Any]:
                return self._rows

        class _FakeSession:
            def __init__(self, rows: list[Any]) -> None:
                self._rows = rows

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> _FakeSession:
                return self

            async def execute(self, stmt: object, params: dict[str, Any] | None = None) -> _Result:
                s = str(stmt)
                if "FROM organisations" in s:
                    return _Result(scalars=[_ORG])
                if "UPDATE runs SET" in s:
                    # Dedicated terminalizer UPDATEs (B4/B5) — none matched here.
                    return _Result()
                return _Result(rows=self._rows)

            async def get(self, model: Any, pk: Any) -> Any:
                return None

        class _FakeFactory:
            def __init__(self, rows: list[Any]) -> None:
                self._rows = rows

            def __call__(self) -> _FakeSession:
                return _FakeSession(self._rows)

        q = MagicMock()
        q.name = "runs"
        q.job = AsyncMock(return_value=None)  # job hash evicted -> None

        with (
            patch.object(
                ch,
                "get_settings",
                return_value=MagicMock(
                    saq_runs_queue="runs",
                    saq_reenqueue_window=600,
                    saq_job_heartbeat=300,
                    saq_redis_pool_size=50,
                    saq_run_claim_cap=20,
                    saq_claimed_nodeless_minutes=45,
                ),
            ),
            patch.object(ch, "_open_factory", return_value=_FakeFactory([row])),
            patch.object(ch, "_set_rls_org", AsyncMock()),
            patch.object(ch.AsyncRedis, "from_url", return_value=redis),
            patch.object(ch, "RedisQueue", return_value=q),
            patch.object(ch, "_ingest_saq_error", AsyncMock()),
            patch.object(ch, "_re_enqueue_run", AsyncMock(return_value=("enqueued", "job-2"))) as re_enqueue,
        ):
            summary = await ch.dispatcher_reconcile()

        assert summary["repaired"] == 1
        # B2: NO SAQ-internal eviction — no DEL/ZREM/LREM issued at all.
        redis.delete.assert_not_awaited()
        redis.zrem.assert_not_awaited()
        redis.lrem.assert_not_awaited()
        # The deterministic key was re-checked (job exists -> skip; gone -> re-enqueue).
        q.job.assert_awaited()
        # Normal re-enqueue via dispatch_run with a FRESH key_suffix.
        re_enqueue.assert_awaited_once()
        assert re_enqueue.await_args.kwargs["key_suffix"]


# ---------------------------------------------------------------------------
# claim-token fence — superseded heartbeat aborts before any side effect
# ---------------------------------------------------------------------------


class TestClaimTokenFence:
    async def test_superseded_heartbeat_raises_without_touching_job_hash(self) -> None:
        """Token A superseded by B: the ATOMIC guarded heartbeat UPDATE
        (``WHERE claim_token=:tok``) matches zero rows, so ClaimSupersededError
        is raised and the SAQ job hash is NEVER touched."""

        class _Result:
            def __init__(self, matched: bool) -> None:
                self._matched = matched

            def fetchone(self) -> Any | None:
                return ("id",) if self._matched else None

        class _Recorder:
            def __init__(self) -> None:
                self.statements: list[str] = []
                self.commits = 0

            def new_conn(self) -> _Conn:
                return _Conn(self)

        class _Conn:
            def __init__(self, rec: _Recorder) -> None:
                self.rec = rec

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> _Conn:
                return self

            async def execute(self, stmt: object, params: dict[str, Any] | None = None) -> _Result:
                self.rec.statements.append(str(stmt))
                if "UPDATE runs SET heartbeat_at" in str(stmt):
                    # The guarded UPDATE with claim_token=:tok matched zero rows
                    # (the successor rotated the token) -> superseded.
                    return _Result(matched=False)
                return _Result(matched=False)

            async def commit(self) -> None:
                self.rec.commits += 1

        class _Engine:
            def __init__(self, rec: _Recorder) -> None:
                self.rec = rec

            def connect(self) -> _Conn:
                return self.rec.new_conn()

        rec = _Recorder()
        job = MagicMock()
        job.update = AsyncMock()

        with pytest.raises(pe.ClaimSupersededError):
            await pe.heartbeat_once(_Engine(rec), str(_RUN_ID), str(_ORG), job=job, claim_token="tok-a")

        # The ATOMIC guarded UPDATE (WHERE claim_token=:tok) WAS attempted —
        # that is the fence. Rowcount 0 -> superseded -> raise.
        assert any("UPDATE runs SET heartbeat_at" in s and "claim_token" in s for s in rec.statements)
        # The SAQ job hash is untouched: job.update() only runs when the write
        # actually landed.
        job.update.assert_not_awaited()

    async def test_current_token_heartbeat_writes(self) -> None:
        """A matching token: the guarded UPDATE matches, the write lands, and the
        SAQ job hash IS touched exactly once."""

        class _Result:
            def __init__(self, matched: bool) -> None:
                self._matched = matched

            def fetchone(self) -> Any | None:
                return ("id",) if self._matched else None

        statements: list[str] = []

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def execute(self, stmt: object, params: dict[str, Any] | None = None) -> _Result:
                statements.append(str(stmt))
                if "UPDATE runs SET heartbeat_at" in str(stmt):
                    # The guarded UPDATE matched (claim_token still owned by this
                    # executor) -> the heartbeat write lands.
                    return _Result(matched=True)
                return _Result(matched=False)

            async def commit(self) -> None:
                return None

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        job = MagicMock()
        job.update = AsyncMock()
        await pe.heartbeat_once(_Engine(), str(_RUN_ID), str(_ORG), job=job, claim_token="tok-a")

        assert any("UPDATE runs SET heartbeat_at" in s for s in statements)
        job.update.assert_awaited_once()


# ---------------------------------------------------------------------------
# event-loop-stall refusal — live-heartbeat runs refuse the next claim
# ---------------------------------------------------------------------------


class TestEventLoopStallRefusal:
    def test_claim_sql_requires_stale_heartbeat_for_running_rows(self) -> None:
        sql = str(
            pe.build_claim_update(_stale_seconds=450, claim_token="tok-a").compile(
                dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True}
            )
        )
        # A successor can only claim a RUNNING row once the heartbeat is stale
        # past the 450s gate — the at-most-once refusal point for event-loop stalls.
        assert "status = 'running'" in sql
        assert "stale_seconds" in sql or "450" in sql

    async def test_claim_refused_when_heartbeat_fresh(self) -> None:
        """A fresh-heartbeat run refuses the next claim (no row matched)."""

        class _Result:
            def fetchone(self) -> Any | None:
                return None

        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> _Conn:
                return self

            async def execute(self, stmt: object, params: dict[str, Any] | None = None) -> _Result:
                return _Result()

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        with patch.object(pe, "_maybe_alert_retry_storm", AsyncMock()) as storm:
            claim_token = await pe.claim_run_async(_Engine(), str(_RUN_ID), str(_ORG))  # type: ignore[arg-type]

        assert claim_token is None
        storm.assert_not_awaited()


# ---------------------------------------------------------------------------
# fire_due_triggers — two workers, exactly one fire job per epoch
# ---------------------------------------------------------------------------


class TestFireDueTriggersSingleFire:
    async def test_two_concurrent_ticks_enqueue_exactly_one_fire_job(self) -> None:
        from datetime import UTC, datetime, timedelta

        trigger_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        cron_row = SimpleNamespace(
            id=trigger_id,
            pipeline_id=pipeline_id,
            config_json={"snapshot_id": str(uuid.uuid4())},
            cron_expression="0 0 1 1 *",
            cron_timezone="UTC",
            # Consumed by _enqueue_cron_fire's catchup marker:
            # int(row.next_fire_at.timestamp()); a UTC datetime is required.
            next_fire_at=datetime.now(UTC) + timedelta(days=365),
            # Also read by the catch-up scan the fake session answers (both
            # SELECTs contain "cron_expression"). Fresh last_fired_at makes the
            # yearly-cadence row catch-up-INELIGIBLE, so this test keeps
            # exercising only the single-fire advance path.
            last_fired_at=datetime.now(UTC) - timedelta(hours=1),
        )

        advance_wins = {"n": 0}

        class _Result:
            def __init__(self, rows: list[Any] | None = None, *, scalars: list[Any] | None = None) -> None:
                self._rows = rows or []
                self._scalars = scalars or []
                self.fetchone_row: Any | None = None

            def scalars(self) -> list[Any]:
                return self._scalars

            def all(self) -> list[Any]:
                return self._rows

            def fetchone(self) -> Any | None:
                return self.fetchone_row

        class _FakeSession:
            def __init__(self, rows: list[Any]) -> None:
                self._rows = rows

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> _FakeSession:
                return self

            async def execute(self, stmt: object, params: dict[str, Any] | None = None) -> _Result:
                s = str(stmt)
                if "FROM organisations" in s:
                    return _Result(scalars=[_ORG])
                if "UPDATE triggers SET next_fire_at" in s:
                    # The atomic advance — exactly ONE concurrent tick may win the
                    # epoch (models the row-lock + re-evaluated WHERE in SQL).
                    await asyncio.sleep(0)
                    r = _Result()
                    if advance_wins["n"] == 0:
                        advance_wins["n"] += 1
                        r.fetchone_row = (trigger_id,)
                    return r
                if "scheduled_reports" in s:
                    return _Result()  # no due reports
                if "cron_expression" in s:
                    return _Result(rows=self._rows)  # cron triggers
                return _Result()  # polling triggers: none due

        class _FakeFactory:
            def __init__(self, rows: list[Any]) -> None:
                self._rows = rows

            def __call__(self) -> _FakeSession:
                return _FakeSession(self._rows)

        redis = MagicMock()
        redis.aclose = AsyncMock()
        q = MagicMock()
        q.name = "runs"

        with (
            patch.object(ch, "get_settings", return_value=MagicMock(saq_runs_queue="runs", saq_redis_pool_size=50)),
            patch.object(ch, "_open_factory", return_value=_FakeFactory([cron_row])),
            patch.object(ch, "_set_rls_org", AsyncMock()),
            patch.object(ch.AsyncRedis, "from_url", return_value=redis),
            patch.object(ch, "RedisQueue", return_value=q),
            patch.object(ch, "_enqueue_fire_job_async", AsyncMock(return_value="job-1")) as enqueue,
            patch.object(ch, "_ingest_saq_error", AsyncMock()),
        ):
            results = await asyncio.gather(ch.fire_due_triggers(), ch.fire_due_triggers())

        total_enqueued = sum(r["cron_enqueued"] for r in results)
        assert total_enqueued == 1
        enqueue.assert_awaited_once()


# ---------------------------------------------------------------------------
# cost_probe cron hygiene — 5-field "*/5 * * * *" (bug class #680)
# ---------------------------------------------------------------------------


class TestCostProbeCron:
    def test_cost_probe_uses_5_field_every_5_min(self) -> None:
        import modulo.core.saq_worker as sw

        jobs = {c.function.__name__: c for c in sw._system_cron_jobs()}
        assert jobs["cost_probe"].cron == "*/5 * * * *"


# ---------------------------------------------------------------------------
# SAQ alerting layer (plan F1 probe 6 / F3a)
# ---------------------------------------------------------------------------


class TestSaqAlerting:
    def test_trigger_period_is_fixed_schedule_cadence(self) -> None:
        from datetime import UTC, datetime

        import modulo.core.error_tracking as et

        now = datetime.now(UTC)
        assert et._trigger_period_seconds("cron", "0 * * * *", "UTC", {}, now) == 3600
        assert et._trigger_period_seconds("cron", "0 0 * * *", "UTC", {}, now) == 86400
        assert et._trigger_period_seconds("cron", "*/5 * * * *", "UTC", {}, now) == 300
        assert et._trigger_period_seconds("polling", None, None, {"poll_interval_seconds": 3600}, now) == 3600
        assert et._trigger_period_seconds("cron", "not-a-cron", "UTC", {}, now) is None

    async def test_retry_storm_alert_threshold(self) -> None:
        import modulo.core.error_tracking as et
        from modulo.db import rls

        with (
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=_FakeFactory()),
            patch.object(rls, "set_rls_org", AsyncMock()),
            patch.object(et, "create_error_event", AsyncMock()) as create,
        ):
            # Below threshold: no alert.
            await et.emit_saq_retry_storm_alert(MagicMock(), str(_ORG), str(_RUN_ID), claim_count=2)
            create.assert_not_awaited()
            # At/above threshold: one error_event (source='saq') per storm.
            await et.emit_saq_retry_storm_alert(MagicMock(), str(_ORG), str(_RUN_ID), claim_count=5)
            create.assert_awaited_once()
            event_kwargs = create.await_args.kwargs
            assert event_kwargs["source"] == "saq"
            assert event_kwargs["level"] == "error"
            assert event_kwargs["context_json"]["claim_count"] == 5

    async def test_missed_fire_alerts_silent_low_cadence_trigger(self) -> None:
        from datetime import UTC, datetime, timedelta

        import modulo.core.error_tracking as et
        from modulo.db import rls

        trigger_id = uuid.uuid4()
        now = datetime.now(UTC)
        stale = SimpleNamespace(
            id=trigger_id,
            trigger_type="cron",
            cron_expression="0 * * * *",  # hourly — period >= 1h
            cron_timezone="UTC",
            config_json={},
            last_fired_at=now - timedelta(hours=2),  # missed by > period + grace
            created_at=now - timedelta(days=1),
        )
        et._missed_fire_cooldowns.clear()
        redis = AsyncMock()  # set() truthy -> cooldown acquired; aclose() awaited
        with (
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=_FakeFactory([stale])),
            patch.object(rls, "set_rls_org", AsyncMock()),
            # check_missed_fire_alerts builds its cooldown client from
            # get_settings().redis_url — the integration conftest intentionally
            # sets REDIS_URL="" (no real Redis for these fully-mocked tests).
            patch.object(et.AsyncRedis, "from_url", return_value=redis),
            patch.object(et, "create_error_event", AsyncMock()) as create,
        ):
            emitted = await et.check_missed_fire_alerts(MagicMock(), org_id=_ORG)

        assert emitted == 1
        create.assert_awaited_once()

    async def test_missed_fire_skips_fresh_and_brand_new_triggers(self) -> None:
        from datetime import UTC, datetime, timedelta

        import modulo.core.error_tracking as et
        from modulo.db import rls

        now = datetime.now(UTC)
        fresh = SimpleNamespace(
            id=uuid.uuid4(),
            trigger_type="cron",
            cron_expression="0 * * * *",
            cron_timezone="UTC",
            config_json={},
            last_fired_at=now - timedelta(minutes=5),  # recent — on schedule
            created_at=now - timedelta(days=1),
        )
        brand_new = SimpleNamespace(
            id=uuid.uuid4(),
            trigger_type="polling",
            cron_expression=None,
            cron_timezone=None,
            config_json={"poll_interval_seconds": 7200},
            last_fired_at=None,  # never fired yet
            created_at=now - timedelta(minutes=10),  # younger than period+grace
        )
        et._missed_fire_cooldowns.clear()
        redis = AsyncMock()
        with (
            patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=_FakeFactory([fresh, brand_new])),
            patch.object(rls, "set_rls_org", AsyncMock()),
            patch.object(et.AsyncRedis, "from_url", return_value=redis),
            patch.object(et, "create_error_event", AsyncMock()) as create,
        ):
            emitted = await et.check_missed_fire_alerts(MagicMock(), org_id=_ORG)

        assert emitted == 0
        create.assert_not_awaited()


class _Session:
    """Fake async session: __aenter__/begin + execute returning canned rows."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, stmt: object, params: dict[str, Any] | None = None) -> Any:
        result = MagicMock()
        result.all.return_value = self._rows
        return result


class _FakeFactory:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []

    def __call__(self) -> _Session:
        return _Session(self._rows)
