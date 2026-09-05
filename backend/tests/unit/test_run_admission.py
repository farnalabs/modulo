"""Unit tests for modulo.core.run_admission — FAR-604 admission healing.

Mock/fake based — no Postgres, no Redis. Covers:
  * D1 slot reconciliation sweep: stale ``running`` rows are released with the
    house ``worker_lost`` code, logged per pipeline, journeys advanced
    fail-open; the sweep never touches pending rows; admission succeeds again
    once the leaked slot is released.
  * D2 queue coalescing: GitHub coalesce-key derivation, per-trigger flag,
    latest-wins pending-run fold (no new row), forged reserved keys stripped.
  * D3 dispatcher backpressure: depth/age gates, fail-open on read errors,
    webhook refusal (typed error + auditable event), cron skip-not-defer.
  * D4 config guard: pipeline create/update reject ``max_concurrent_runs`` < 1
    with a clear validation error.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

import modulo.core.run_admission as ra
from modulo.core.run_admission import (
    SlotReconciliationError,
    coalesce_enabled,
    derive_webhook_coalesce_key,
    evaluate_backpressure,
    reconcile_pipeline_slots,
)
from modulo.core.trigger_engine import PipelineBackpressureError, TriggerEngine

ORG_ID = uuid.UUID("18348064-eca3-4aa7-be96-8f6c9123efd0")
OTHER_ORG_ID = uuid.UUID("2b6d9f0a-1c3e-4d5b-8a7f-9e0d1c2b3a4c")
PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000006666")
TRIGGER_ID = uuid.UUID("00000000-0000-0000-0000-000000007777")
SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-000000008888")
RUN_ID = uuid.UUID("fb4b1368-68ca-4125-8091-ca8d7c25839e")


# ---------------------------------------------------------------------------
# D1 — slot reconciliation sweep
# ---------------------------------------------------------------------------


class _AsyncResult:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rowcount = len(rows or [])
        self._rows = rows or []

    def all(self) -> list[Any]:
        return self._rows


def _released_row() -> Any:
    return SimpleNamespace(
        id=RUN_ID,
        organisation_id=ORG_ID,
        pipeline_id=PIPELINE_ID,
    )


class _SweepConn:
    """Connection double: org enumeration + one guarded slot-release UPDATE.

    Records ``(statement, params)`` pairs so isolation tests can assert the
    GUC set_config values and the per-org UPDATE's bound org.
    """

    def __init__(
        self,
        statements: list[str],
        released: list[Any],
        orgs: list[uuid.UUID],
        params_seen: list[dict[str, object]],
    ) -> None:
        self._statements = statements
        self._released = released
        self._orgs = orgs
        self.params_seen = params_seen

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
        self._statements.append(str(stmt))
        if params is not None:
            self.params_seen.append(dict(params))
        if "SELECT id FROM organisations" in str(stmt):
            return _AsyncResult(rows=[(org,) for org in self._orgs])
        if "status = 'running'" in str(stmt):
            return _AsyncResult(rows=list(self._released))
        return _AsyncResult()


class _SweepEngine:
    def __init__(self, statements: list[str], released: list[Any], orgs: list[uuid.UUID] | None = None) -> None:
        self._statements = statements
        self._released = released
        self._orgs = orgs or [ORG_ID]
        # Shared across connections: each org loop opens a FRESH conn, so the
        # params must be recorded at engine level for isolation assertions.
        self.params_seen: list[dict[str, object]] = []

    def connect(self) -> _SweepConn:
        return _SweepConn(self._statements, self._released, self._orgs, self.params_seen)


class TestReconcilePipelineSlots:
    def _settings(self, stale_seconds: int = 1800) -> MagicMock:
        return MagicMock(slot_reconcile_stale_seconds=stale_seconds)

    async def test_releases_stale_running_slot_with_worker_lost_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        statements: list[str] = []
        engine = _SweepEngine(statements, [_released_row()])
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with patch.object(ra, "_advance_released_run", new_callable=AsyncMock) as advance:
            result = await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        assert result["released"] == 1
        assert result["per_pipeline"] == {str(PIPELINE_ID): 1}
        release_stmts = [s for s in statements if "status = 'running'" in s]
        assert len(release_stmts) == 1
        assert "error_code = 'worker_lost'" in release_stmts[0]
        assert "COALESCE(heartbeat_at, started_at, created_at)" in release_stmts[0]
        assert ":stale_seconds" in release_stmts[0]
        assert "set_config('app.organisation_id'" in " ".join(statements)
        advance.assert_awaited_once()

    async def test_explicit_stale_seconds_overrides_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        params_seen: list[dict[str, object]] = []
        statements: list[str] = []

        class _ParamConn(_SweepConn):
            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
                statements.append(str(stmt))
                if params is not None:
                    params_seen.append(dict(params))
                if "SELECT id FROM organisations" in str(stmt):
                    return _AsyncResult(rows=[(ORG_ID,)])
                return _AsyncResult()

        class _ParamEngine(_SweepEngine):
            def connect(self) -> _ParamConn:
                return _ParamConn(self._statements, self._released, self._orgs, self.params_seen)

        monkeypatch.setattr(ra, "get_settings", lambda: self._settings(stale_seconds=999999))
        await reconcile_pipeline_slots(_ParamEngine(statements, []), stale_seconds=60)  # type: ignore[arg-type]
        release_params = [p for p in params_seen if "stale_seconds" in p]
        assert release_params
        assert release_params[0]["stale_seconds"] == 60

    async def test_fresh_heartbeat_rows_are_never_swept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        statements: list[str] = []
        engine = _SweepEngine(statements, [])
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        result = await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        assert result["released"] == 0
        assert not result["per_pipeline"]
        release_stmts = [s for s in statements if "status = 'running'" in s]
        assert len(release_stmts) == 1
        # The predicate only matches rows whose heartbeat is OLDER than the
        # window — a fresh heartbeat (or awaiting_human, never SELECTed)
        # can never match it.
        assert "< now() - (:stale_seconds * interval '1 second')" in release_stmts[0]

    async def test_advance_failure_is_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        statements: list[str] = []
        engine = _SweepEngine(statements, [_released_row()])
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with patch(
            "modulo.core.analytics.record_fact_for_terminal_failed_run",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            result = await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        assert result["released"] == 1

    async def test_sweep_failure_raises_with_partial_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """F6: a sweep failure RAISES SlotReconciliationError (so the SAQ
        cron's retries=2 engages — a silently dead sweep must never re-open
        the FAR-604 wedge invisibly) instead of returning an error dict, and
        the raised error carries the PARTIAL counts achieved before the
        failure."""
        released_row = _released_row()
        org2 = OTHER_ORG_ID

        class _PartialConn:
            def __init__(self, fail_on_release: bool) -> None:
                self._fail_on_release = fail_on_release

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
                sql = str(stmt)
                if "SELECT id FROM organisations" in sql:
                    return _AsyncResult(rows=[(ORG_ID,), (org2,)])
                if "set_config" in sql:
                    return _AsyncResult()
                if "status = 'running'" in sql:
                    if self._fail_on_release:
                        # Org 2's release dies; org 1's already succeeded.
                        raise RuntimeError("db down")
                    return _AsyncResult(rows=[released_row])
                raise RuntimeError("db down")

        class _PartialEngine:
            def __init__(self) -> None:
                # conn 1 = org enumeration, conn 2 = org 1, conn 3 = org 2.
                self._n = 0

            def connect(self) -> _PartialConn:
                self._n += 1
                return _PartialConn(fail_on_release=self._n > 2)

        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with pytest.raises(SlotReconciliationError) as excinfo:
            await reconcile_pipeline_slots(_PartialEngine())  # type: ignore[arg-type]

        assert excinfo.value.released == 1
        assert excinfo.value.per_pipeline == {str(PIPELINE_ID): 1}
        assert str(excinfo.value.__cause__) == "db down"

    async def test_sweep_failure_still_advances_already_released_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """F6: the post-release advance is independent of the org-loop
        failure — rows released before a later org's connection died still
        get their journeys + facts advance."""
        released_row = _released_row()
        org2 = OTHER_ORG_ID

        class _PartialConn:
            def __init__(self, fail_on_release: bool) -> None:
                self._fail_on_release = fail_on_release

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def begin(self) -> Self:
                return self

            async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
                sql = str(stmt)
                if "SELECT id FROM organisations" in sql:
                    return _AsyncResult(rows=[(ORG_ID,), (org2,)])
                if "set_config" in sql:
                    return _AsyncResult()
                if "status = 'running'" in sql:
                    if self._fail_on_release:
                        raise RuntimeError("db down")
                    return _AsyncResult(rows=[released_row])
                raise RuntimeError("db down")

        class _PartialEngine:
            def __init__(self) -> None:
                # conn 1 = org enumeration, conn 2 = org 1, conn 3 = org 2.
                self._n = 0

            def connect(self) -> _PartialConn:
                self._n += 1
                return _PartialConn(fail_on_release=self._n > 2)

        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with (
            patch.object(ra, "_advance_released_run", new_callable=AsyncMock) as advance,
            pytest.raises(SlotReconciliationError),
        ):
            await reconcile_pipeline_slots(_PartialEngine())  # type: ignore[arg-type]

        advance.assert_awaited_once()
        assert advance.await_args.args[1] == RUN_ID
        assert advance.await_args.args[2] == ORG_ID

    async def test_two_org_sweep_only_touches_enumerated_org(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """F7 isolation: the per-org release is GUC-gated. Org 2 is NOT in the
        enumeration, so the sweep never sets org 2's GUC and never binds an
        UPDATE to it — org 2's stale running row is untouched even though the
        pipeline/key would match."""
        org2 = OTHER_ORG_ID
        statements: list[str] = []
        engine = _SweepEngine(statements, [_released_row()], orgs=[ORG_ID])
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with patch.object(ra, "_advance_released_run", new_callable=AsyncMock):
            await reconcile_pipeline_slots(engine)  # type: ignore[arg-type]

        set_config_vals = [p["val"] for p in engine.params_seen if "val" in p]
        assert set_config_vals == [str(ORG_ID)], "the GUC must be set for the enumerated org only"
        bound_oids = [p["oid"] for p in engine.params_seen if "oid" in p]
        assert bound_oids == [str(ORG_ID)], "the per-org UPDATE must bind the enumerated org"
        assert str(org2) not in set_config_vals
        assert str(org2) not in bound_oids
        # The UPDATE is defence-in-depth on top of the GUC: the org predicate
        # is IN the statement itself, so a missed set_config can never widen
        # the sweep across orgs.
        release_stmts = [s for s in statements if "status = 'running'" in s]
        assert all("organisation_id = :oid" in s for s in release_stmts)


class TestAdmissionAfterRelease:
    """The incident's contract: a leaked slot blocks admission; after the
    sweep releases it (slot count drops below max_concurrent_runs), dispatch
    admits again."""

    async def test_capacity_gate_admits_once_leaked_slot_released(self) -> None:
        from modulo.core import dispatch

        run = SimpleNamespace(pipeline_id=PIPELINE_ID, status="pending")
        pipeline = SimpleNamespace(max_concurrent_runs=2)

        async def _get_run(session: Any, rid: Any) -> Any:
            return run

        async def _get_pipeline(session: Any, pid: Any) -> Any:
            return pipeline

        session = MagicMock()
        session.get = AsyncMock(side_effect=_get_pipeline)

        with (
            patch("modulo.db.crud.run.get_run", side_effect=_get_run),
            patch("modulo.db.crud.run.count_active_runs_for_pipeline", new_callable=AsyncMock) as count,
        ):
            # Before release: 2 slots counted for a 2-slot pipeline — wedged.
            count.return_value = 2
            assert await dispatch._capacity_deferred(session, RUN_ID) is True
            # After the sweep releases the leaked slot: 1 real slot in use.
            count.return_value = 1
            assert await dispatch._capacity_deferred(session, RUN_ID) is False


# ---------------------------------------------------------------------------
# D2 — coalesce-key derivation + pending-run folding
# ---------------------------------------------------------------------------


class TestDeriveWebhookCoalesceKey:
    def test_github_pr_event(self) -> None:
        payload = {
            "repository": {"full_name": "farnalabs/modulo"},
            "pull_request": {"number": 42},
        }
        assert derive_webhook_coalesce_key(payload) == "github:farnalabs/modulo:pr:42"

    def test_github_issue_event(self) -> None:
        payload = {
            "repository": {"full_name": "farnalabs/modulo"},
            "issue": {"number": 7},
        }
        assert derive_webhook_coalesce_key(payload) == "github:farnalabs/modulo:issue:7"

    def test_github_push_without_pr_number_returns_none(self) -> None:
        payload = {"repository": {"full_name": "farnalabs/modulo"}, "ref": "refs/heads/main"}
        assert derive_webhook_coalesce_key(payload) is None

    def test_non_github_payload_returns_none(self) -> None:
        assert derive_webhook_coalesce_key({"foo": "bar"}) is None
        assert derive_webhook_coalesce_key(None) is None
        assert derive_webhook_coalesce_key({"repository": "not-a-dict"}) is None

    def test_key_stable_across_synchronize_redeliveries(self) -> None:
        push_a = {
            "action": "synchronize",
            "repository": {"full_name": "o/r"},
            "pull_request": {"number": 5},
        }
        push_b = {
            "action": "synchronize",
            "repository": {"full_name": "o/r"},
            "pull_request": {"number": 5, "head": {"sha": "different"}},
        }
        assert derive_webhook_coalesce_key(push_a) == derive_webhook_coalesce_key(push_b)


class TestCoalesceEnabled:
    def test_default_on(self) -> None:
        assert coalesce_enabled(None) is True
        assert coalesce_enabled({}) is True

    def test_explicit_flag(self) -> None:
        assert coalesce_enabled({"coalesce_pending": False}) is False
        assert coalesce_enabled({"coalesce_pending": True}) is True


class TestCoalescePendingRun:
    def _session(self, dialect: str, run: Any = None, candidates: list[Any] | None = None) -> Any:
        session = MagicMock()
        session.flush = AsyncMock()
        session.execute = AsyncMock()
        if dialect == "postgresql":
            result = MagicMock()
            result.scalar_one_or_none.return_value = run
            session.execute.return_value = result
        else:
            result = MagicMock()
            scalars = MagicMock()
            scalars.all.return_value = candidates or []
            result.scalars.return_value = scalars
            session.execute.return_value = result
        return session

    async def test_second_event_for_same_key_updates_pending_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pending = SimpleNamespace(
            id=RUN_ID,
            status="pending",
            input_payload={"_coalesce_key": "github:o/r:pr:1", "old": True},
            input_hash="oldhash",
            created_at=datetime.now(UTC) - timedelta(minutes=10),
        )
        session = self._session("postgresql", run=pending)
        monkeypatch.setattr("modulo.db.crud.run._get_dialect_name", AsyncMock(return_value="postgresql"))
        from modulo.db.crud.run import coalesce_pending_run

        updated = await coalesce_pending_run(
            session,
            org_id=ORG_ID,
            pipeline_id=PIPELINE_ID,
            coalesce_key="github:o/r:pr:1",
            input_payload={"new": "payload"},
        )

        assert updated is pending
        assert updated.input_payload["new"] == "payload"
        assert updated.input_payload["_coalesce_key"] == "github:o/r:pr:1"
        assert updated.input_hash != "oldhash"
        assert updated.created_at > datetime.now(UTC) - timedelta(seconds=5)
        session.flush.assert_awaited_once()

    async def test_no_pending_match_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = self._session("postgresql", run=None)
        monkeypatch.setattr("modulo.db.crud.run._get_dialect_name", AsyncMock(return_value="postgresql"))
        from modulo.db.crud.run import coalesce_pending_run

        updated = await coalesce_pending_run(
            session,
            org_id=ORG_ID,
            pipeline_id=PIPELINE_ID,
            coalesce_key="github:o/r:pr:2",
            input_payload={"a": 1},
        )
        assert updated is None

    async def test_different_key_inserts_fresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pending = SimpleNamespace(
            id=RUN_ID,
            status="pending",
            input_payload={"_coalesce_key": "github:o/r:pr:1"},
            input_hash="h",
            created_at=datetime.now(UTC),
        )
        session = self._session("postgresql", run=None)
        monkeypatch.setattr("modulo.db.crud.run._get_dialect_name", AsyncMock(return_value="postgresql"))
        from modulo.db.crud.run import coalesce_pending_run

        updated = await coalesce_pending_run(
            session,
            org_id=ORG_ID,
            pipeline_id=PIPELINE_ID,
            coalesce_key="github:o/r:pr:9",
            input_payload={"a": 1},
        )
        assert updated is None
        assert pending.input_payload["_coalesce_key"] == "github:o/r:pr:1"

    async def test_forged_reserved_key_is_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pending = SimpleNamespace(
            id=RUN_ID,
            status="pending",
            input_payload={},
            input_hash="h",
            created_at=datetime.now(UTC),
        )
        session = self._session("postgresql", run=pending)
        monkeypatch.setattr("modulo.db.crud.run._get_dialect_name", AsyncMock(return_value="postgresql"))
        from modulo.db.crud.run import coalesce_pending_run

        await coalesce_pending_run(
            session,
            org_id=ORG_ID,
            pipeline_id=PIPELINE_ID,
            coalesce_key="github:o/r:pr:1",
            input_payload={"_coalesce_key": "forged", "data": 1},
        )
        assert pending.input_payload["_coalesce_key"] == "github:o/r:pr:1"

    async def test_non_postgres_matches_key_in_python(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stale = SimpleNamespace(
            id=uuid.uuid4(),
            organisation_id=ORG_ID,
            status="pending",
            input_payload={"_coalesce_key": "github:o/r:pr:1"},
            created_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        session = self._session("sqlite", candidates=[stale])
        monkeypatch.setattr("modulo.db.crud.run._get_dialect_name", AsyncMock(return_value="sqlite"))
        from modulo.db.crud.run import coalesce_pending_run

        updated = await coalesce_pending_run(
            session,
            org_id=ORG_ID,
            pipeline_id=PIPELINE_ID,
            coalesce_key="github:o/r:pr:1",
            input_payload={"refreshed": True},
        )
        assert updated is stale
        assert updated.input_payload["refreshed"] is True

    async def test_non_postgres_fold_never_crosses_orgs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """F7 isolation: a delivery in org B must never fold into org A's
        pending run, even when the pipeline id and coalesce key match. The
        non-PostgreSQL candidate scan has NO RLS to lean on, so the fold
        binds the delivery's org explicitly (the SQL WHERE and the Python
        match both carry it) — the result is no fold and NO mutation of
        org A's row."""
        org_a = OTHER_ORG_ID
        pending = SimpleNamespace(
            id=RUN_ID,
            organisation_id=org_a,
            status="pending",
            input_payload={"_coalesce_key": "github:o/r:pr:1", "user": "a"},
            created_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        session = self._session("sqlite", candidates=[pending])
        monkeypatch.setattr("modulo.db.crud.run._get_dialect_name", AsyncMock(return_value="sqlite"))
        from modulo.db.crud.run import coalesce_pending_run

        updated = await coalesce_pending_run(
            session,
            org_id=ORG_ID,
            pipeline_id=PIPELINE_ID,
            coalesce_key="github:o/r:pr:1",
            input_payload={"refreshed": True},
        )
        assert updated is None
        assert pending.input_payload == {"_coalesce_key": "github:o/r:pr:1", "user": "a"}
        session.flush.assert_not_awaited()


# ---------------------------------------------------------------------------
# D3 — dispatcher backpressure
# ---------------------------------------------------------------------------


class TestEvaluateBackpressure:
    def _session(self, max_concurrent: int | None) -> Any:
        session = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = max_concurrent
        session.execute = AsyncMock(return_value=result)
        return session

    async def test_queue_over_depth_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ra, "get_settings", lambda: MagicMock(trigger_backpressure_max_age_seconds=3600))
        session = self._session(2)
        with patch(
            "modulo.db.crud.run.get_pipeline_queue_depth",
            new_callable=AsyncMock,
            return_value=(7, datetime.now(UTC)),
        ):
            skip, reason = await evaluate_backpressure(session, pipeline_id=PIPELINE_ID)
        assert skip is True
        assert "queue_depth=7" in reason
        assert "limit=6" in reason

    async def test_under_limits_admits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ra, "get_settings", lambda: MagicMock(trigger_backpressure_max_age_seconds=3600))
        session = self._session(5)
        with patch(
            "modulo.db.crud.run.get_pipeline_queue_depth",
            new_callable=AsyncMock,
            return_value=(3, datetime.now(UTC)),
        ):
            skip, reason = await evaluate_backpressure(session, pipeline_id=PIPELINE_ID)
        assert skip is False
        assert reason == ""

    async def test_depth_limit_floors_at_5(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ra, "get_settings", lambda: MagicMock(trigger_backpressure_max_age_seconds=3600))
        session = self._session(1)
        with patch(
            "modulo.db.crud.run.get_pipeline_queue_depth",
            new_callable=AsyncMock,
            return_value=(6, datetime.now(UTC)),
        ):
            skip, reason = await evaluate_backpressure(session, pipeline_id=PIPELINE_ID)
        assert skip is True
        assert "limit=5" in reason

    async def test_oldest_pending_age_over_threshold_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ra, "get_settings", lambda: MagicMock(trigger_backpressure_max_age_seconds=3600))
        session = self._session(5)
        old = datetime.now(UTC) - timedelta(hours=2)
        with patch(
            "modulo.db.crud.run.get_pipeline_queue_depth",
            new_callable=AsyncMock,
            return_value=(2, old),
        ):
            skip, reason = await evaluate_backpressure(session, pipeline_id=PIPELINE_ID)
        assert skip is True
        assert "oldest_age=" in reason

    async def test_empty_queue_admits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ra, "get_settings", lambda: MagicMock(trigger_backpressure_max_age_seconds=3600))
        session = self._session(5)
        with patch(
            "modulo.db.crud.run.get_pipeline_queue_depth",
            new_callable=AsyncMock,
            return_value=(0, None),
        ):
            skip, _reason = await evaluate_backpressure(session, pipeline_id=PIPELINE_ID)
        assert skip is False

    async def test_missing_pipeline_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ra, "get_settings", lambda: MagicMock(trigger_backpressure_max_age_seconds=3600))
        session = self._session(None)
        skip, reason = await evaluate_backpressure(session, pipeline_id=PIPELINE_ID)
        assert skip is False
        assert reason == ""

    async def test_read_error_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ra, "get_settings", lambda: MagicMock(trigger_backpressure_max_age_seconds=3600))
        session = MagicMock()
        session.execute = AsyncMock(side_effect=RuntimeError("db down"))
        skip, _reason = await evaluate_backpressure(session, pipeline_id=PIPELINE_ID)
        assert skip is False


# ---------------------------------------------------------------------------
# D2/D3 — webhook seam integration
# ---------------------------------------------------------------------------


def _delivery(cfg: dict[str, Any] | None) -> Any:
    from modulo.core.trigger_engine import _WebhookDelivery

    trigger = SimpleNamespace(
        id=TRIGGER_ID,
        pipeline_id=PIPELINE_ID,
        config_json=cfg,
        trigger_type="webhook",
    )
    return _WebhookDelivery(
        org_id=ORG_ID,
        trigger=trigger,
        raw_body=b"{}",
        raw_payload={
            "repository": {"full_name": "o/r"},
            "pull_request": {"number": 1},
        },
        snapshot_id=SNAPSHOT_ID,
    )


class TestWebhookSeamIntegration:
    def _engine_with_session(self) -> tuple[TriggerEngine, MagicMock]:
        engine = TriggerEngine()
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        return engine, session

    async def test_coalesced_delivery_returns_existing_run_without_new_row(self) -> None:
        engine, session = self._engine_with_session()
        delivery = _delivery({})
        coalesced_run = SimpleNamespace(id=RUN_ID, status="pending")

        with (
            patch(
                "modulo.core.trigger_engine.coalesce_pending_run",
                new_callable=AsyncMock,
                return_value=coalesced_run,
            ) as coalesce,
            patch(
                "modulo.core.trigger_engine.create_run",
                new_callable=AsyncMock,
            ) as create_run_mock,
            patch.object(engine, "_log_event", new_callable=AsyncMock) as log_event,
            patch.object(engine, "_store_raw_payload", new_callable=AsyncMock),
        ):
            run, event = await engine._create_webhook_run(
                session,
                delivery,
                input_payload={"mapped": True},
                payload_hash="hash",
                rate_limit=SimpleNamespace(key=None),
            )

        assert run is coalesced_run
        assert event is not None
        coalesce.assert_awaited_once()
        create_run_mock.assert_not_awaited()
        assert log_event.await_args.kwargs["result"] == "coalesced"
        assert log_event.await_args.kwargs["run_id"] == RUN_ID

    async def test_flag_off_inserts_fresh_run(self) -> None:
        engine, session = self._engine_with_session()
        delivery = _delivery({"coalesce_pending": False})
        fresh_run = SimpleNamespace(id=uuid.uuid4(), status="pending")

        with (
            patch(
                "modulo.core.trigger_engine.coalesce_pending_run",
                new_callable=AsyncMock,
            ) as coalesce,
            patch(
                "modulo.core.trigger_engine.create_run",
                new_callable=AsyncMock,
                return_value=fresh_run,
            ) as create_run_mock,
            patch(
                "modulo.core.trigger_engine.evaluate_backpressure",
                new_callable=AsyncMock,
                return_value=(False, ""),
            ),
            patch.object(engine, "_log_event", new_callable=AsyncMock),
            patch.object(engine, "_store_raw_payload", new_callable=AsyncMock),
        ):
            run, _event = await engine._create_webhook_run(
                session,
                delivery,
                input_payload={"mapped": True},
                payload_hash="hash",
                rate_limit=SimpleNamespace(key=None),
            )

        assert run is fresh_run
        coalesce.assert_not_awaited()
        assert create_run_mock.await_args.kwargs["coalesce_key"] is None

    async def test_backpressured_delivery_is_refused_and_audited(self) -> None:
        engine, session = self._engine_with_session()
        delivery = _delivery({})

        with (
            patch(
                "modulo.core.trigger_engine.coalesce_pending_run",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "modulo.core.trigger_engine.evaluate_backpressure",
                new_callable=AsyncMock,
                return_value=(True, "queue_depth=8 limit=6"),
            ),
            patch(
                "modulo.core.trigger_engine.create_run",
                new_callable=AsyncMock,
            ) as create_run_mock,
            patch.object(engine, "_log_event", new_callable=AsyncMock) as log_event,
            pytest.raises(PipelineBackpressureError),
        ):
            await engine._create_webhook_run(
                session,
                delivery,
                input_payload={},
                payload_hash="hash",
                rate_limit=SimpleNamespace(key=None),
            )

        create_run_mock.assert_not_awaited()
        assert log_event.await_args.kwargs["result"] == "backpressure_skipped"
        assert "queue_depth=8" in log_event.await_args.kwargs["error_detail"]

    async def test_replay_bypasses_coalesce_and_backpressure(self) -> None:
        engine, session = self._engine_with_session()
        delivery = _delivery({})
        fresh_run = SimpleNamespace(id=uuid.uuid4(), status="pending")

        with (
            patch(
                "modulo.core.trigger_engine.coalesce_pending_run",
                new_callable=AsyncMock,
            ) as coalesce,
            patch(
                "modulo.core.trigger_engine.evaluate_backpressure",
                new_callable=AsyncMock,
            ) as backpressure,
            patch(
                "modulo.core.trigger_engine.create_run",
                new_callable=AsyncMock,
                return_value=fresh_run,
            ),
            patch.object(engine, "_log_event", new_callable=AsyncMock),
            patch.object(engine, "_store_raw_payload", new_callable=AsyncMock),
        ):
            await engine._create_webhook_run(
                session,
                delivery,
                input_payload={},
                payload_hash="hash",
                rate_limit=SimpleNamespace(key=None),
                is_replay=True,
            )

        coalesce.assert_not_awaited()
        backpressure.assert_not_awaited()


# ---------------------------------------------------------------------------
# D3 — cron skip-not-defer helper
# ---------------------------------------------------------------------------


class TestBackpressureSkipHelper:
    async def test_skip_stamps_last_fired_and_logs_event(self) -> None:
        from modulo.core.cron_helpers import _backpressure_skip

        session = MagicMock()
        session.execute = AsyncMock()
        trigger = SimpleNamespace(id=TRIGGER_ID)

        with patch("modulo.core.cron_helpers._log_event", new_callable=AsyncMock) as log_event:
            result = await _backpressure_skip(session, trigger, ORG_ID, "queue_depth=8 limit=6")

        assert result["status"] == "skipped"
        assert result["reason"] == "backpressure"
        assert "queue_depth=8" in result["detail"]
        assert log_event.await_args.kwargs["result"] == "backpressure_skipped"
        session.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# D4 — config guard
# ---------------------------------------------------------------------------


class TestPipelineConfigGuard:
    async def test_create_pipeline_rejects_zero(self) -> None:
        from modulo.db.crud.pipeline import create_pipeline

        session = MagicMock()
        with pytest.raises(ValueError, match="must be >= 1"):
            await create_pipeline(
                session,
                org_id=ORG_ID,
                name="wedged",
                account_id=uuid.uuid4(),
                max_concurrent_runs=0,
            )

    async def test_create_pipeline_rejects_negative(self) -> None:
        from modulo.db.crud.pipeline import create_pipeline

        session = MagicMock()
        with pytest.raises(ValueError, match="must be >= 1"):
            await create_pipeline(
                session,
                org_id=ORG_ID,
                name="wedged",
                account_id=uuid.uuid4(),
                max_concurrent_runs=-3,
            )

    async def test_update_pipeline_rejects_zero(self) -> None:
        from modulo.db.crud.pipeline import update_pipeline

        session = MagicMock()
        pipeline = SimpleNamespace(owner_team_id=None)
        result = MagicMock()
        result.scalar_one_or_none.return_value = pipeline
        session.execute = AsyncMock(return_value=result)

        with pytest.raises(ValueError, match="must be >= 1"):
            await update_pipeline(session, uuid.uuid4(), {"max_concurrent_runs": 0})

    async def test_update_pipeline_allows_absent_and_valid_values(self) -> None:
        from modulo.db.crud.pipeline import update_pipeline

        session = MagicMock()
        session.flush = AsyncMock()
        pipeline = SimpleNamespace(owner_team_id=None, max_concurrent_runs=5)
        result = MagicMock()
        result.scalar_one_or_none.return_value = pipeline
        session.execute = AsyncMock(return_value=result)

        with patch("modulo.db.crud.pipeline.apply_updates") as apply_updates:
            updated = await update_pipeline(session, uuid.uuid4(), {"name": "renamed"})
        assert updated is pipeline
        apply_updates.assert_called_once()

    def test_variant_group_request_rejects_zero(self) -> None:
        from modulo.api.routes.variants import CreateVariantGroupRequest

        with pytest.raises(ValidationError):
            CreateVariantGroupRequest(pipeline_id=PIPELINE_ID, name="g", max_concurrent_runs=0)

    def test_variant_group_request_accepts_one(self) -> None:
        from modulo.api.routes.variants import CreateVariantGroupRequest

        req = CreateVariantGroupRequest(pipeline_id=PIPELINE_ID, name="g", max_concurrent_runs=1)
        assert req.max_concurrent_runs == 1


# ---------------------------------------------------------------------------
# D1 (HITL capacity) — pipeline capacity excludes human-waiting runs
# ---------------------------------------------------------------------------


def _status_values(stmt: Any) -> set[str]:
    """Extract the bound status IN-list from a captured count statement."""
    params = stmt.compile().params
    for key, value in params.items():
        if "status" in key and isinstance(value, (list, tuple, set, frozenset)):
            return {str(v) for v in value}
    raise AssertionError(f"no status IN-list found in params: {params}")


class TestPipelineCapacityExcludesAwaitingHuman:
    """FAR-604 D1: ``awaiting_human``/``hitl_parked`` runs must not consume a
    PIPELINE slot (a human decision may take days), while the ORG-level gate
    still counts them (the org-wide worker pool stays bounded)."""

    def test_pipeline_scope_capacity_statuses(self) -> None:
        from modulo.db.crud.run import _active_run_statuses
        from modulo.db.models.run import PIPELINE_CAPACITY_STATUSES

        assert _active_run_statuses(False, scope="pipeline") == set(PIPELINE_CAPACITY_STATUSES)
        assert "awaiting_human" not in PIPELINE_CAPACITY_STATUSES
        assert "hitl_parked" not in PIPELINE_CAPACITY_STATUSES
        assert {"running", "claimed", "unknown"} == PIPELINE_CAPACITY_STATUSES

    def test_org_scope_still_counts_human_waiting_runs(self) -> None:
        from modulo.db.crud.run import _active_run_statuses

        statuses = _active_run_statuses(False, scope="org")
        assert "awaiting_human" in statuses
        assert "hitl_parked" in statuses
        assert "pending" not in statuses

    def test_quota_scope_counts_all_non_terminal(self) -> None:
        from modulo.db.crud.run import _active_run_statuses
        from modulo.db.models.run import ACTIVE_RUN_STATUSES

        assert _active_run_statuses(True, scope="pipeline") == set(ACTIVE_RUN_STATUSES)
        assert _active_run_statuses(True, scope="org") == set(ACTIVE_RUN_STATUSES)

    async def test_pipeline_count_statement_excludes_human_waiting(self) -> None:
        from modulo.db.crud.run import count_active_runs_for_pipeline

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = 0
        session.execute = AsyncMock(return_value=result)
        await count_active_runs_for_pipeline(session, PIPELINE_ID, include_pending=False)

        stmt = session.execute.call_args[0][0]
        assert _status_values(stmt) == {"running", "claimed", "unknown"}

    async def test_org_count_statement_includes_human_waiting(self) -> None:
        from modulo.db.crud.run import count_active_runs_for_org

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = 0
        session.execute = AsyncMock(return_value=result)
        await count_active_runs_for_org(session, ORG_ID, include_pending=False)

        stmt = session.execute.call_args[0][0]
        values = _status_values(stmt)
        assert "awaiting_human" in values
        assert "hitl_parked" in values
        assert "pending" not in values


# ---------------------------------------------------------------------------
# D2 (HITL capacity) — park-on-expiry sweep
# ---------------------------------------------------------------------------


def _parked_row() -> Any:
    return SimpleNamespace(
        id=RUN_ID,
        organisation_id=ORG_ID,
        pipeline_id=PIPELINE_ID,
    )


class _ParkConn:
    """Connection double: org enumeration + guarded park + parked_at stamp."""

    def __init__(
        self,
        statements: list[str],
        parked: list[Any],
        orgs: list[uuid.UUID],
        params_seen: list[dict[str, object]],
        fail_on_park: bool = False,
    ) -> None:
        self._statements = statements
        self._parked = parked
        self._orgs = orgs
        self.params_seen = params_seen
        self._fail_on_park = fail_on_park

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, stmt: object, params: dict[str, object] | None = None) -> _AsyncResult:
        sql = str(stmt)
        self._statements.append(sql)
        if params is not None:
            self.params_seen.append(dict(params))
        if "SELECT id FROM organisations" in sql:
            return _AsyncResult(rows=[(org,) for org in self._orgs])
        if "set_config" in sql:
            return _AsyncResult()
        if "status = 'hitl_parked'" in sql:
            if self._fail_on_park:
                raise RuntimeError("db down")
            return _AsyncResult(rows=list(self._parked))
        return _AsyncResult()


class _ParkEngine:
    def __init__(self, statements: list[str], parked: list[Any], orgs: list[uuid.UUID] | None = None) -> None:
        self._statements = statements
        self._parked = parked
        self._orgs = orgs or [ORG_ID]
        self.params_seen: list[dict[str, object]] = []

    def connect(self) -> _ParkConn:
        return _ParkConn(self._statements, self._parked, self._orgs, self.params_seen)


class TestParkExpiredHitlRuns:
    def _settings(self, grace_seconds: int = 86400) -> MagicMock:
        return MagicMock(hitl_park_grace_seconds=grace_seconds)

    async def test_parks_expired_unclaimed_gate_run(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        statements: list[str] = []
        engine = _ParkEngine(statements, [_parked_row()])
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with caplog.at_level("WARNING"):
            result = await ra.park_expired_hitl_runs(engine)  # type: ignore[arg-type]

        assert result == {"parked": 1}
        park_stmts = [s for s in statements if "status = 'hitl_parked'" in s]
        assert len(park_stmts) == 1
        # Predicate shape: only awaiting_human runs, past the grace window,
        # with an expired UNCLAIMED undecided gate and no other open gate.
        assert "runs.status = 'awaiting_human'" in park_stmts[0]
        assert "cancellation_requested = false" in park_stmts[0]
        assert "hc.expires_at < now() - (:grace_seconds * interval '1 second')" in park_stmts[0]
        assert "hc.account_id IS NULL" in park_stmts[0]
        assert "hc2.account_id IS NOT NULL" in park_stmts[0]
        # The parked_at stamp ran in the same org transaction.
        stamp_stmts = [s for s in statements if "parked_at = now()" in s]
        assert len(stamp_stmts) == 1
        stamp_params = [p for p in engine.params_seen if "parked_run_ids" in p]
        assert stamp_params and stamp_params[0]["parked_run_ids"] == [str(RUN_ID)]
        # Loud structured event per parked run.
        assert any("hitl_park.parked" in r.message for r in caplog.records)
        # RLS org context was set per org.
        assert "set_config('app.organisation_id'" in " ".join(statements)

    async def test_unexpired_or_claimed_gates_never_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        statements: list[str] = []
        engine = _ParkEngine(statements, [])
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        result = await ra.park_expired_hitl_runs(engine)  # type: ignore[arg-type]

        assert result == {"parked": 0}
        park_stmts = [s for s in statements if "status = 'hitl_parked'" in s]
        assert len(park_stmts) == 1
        # Idempotency by construction: a parked run no longer matches the
        # source status, so a second tick can never re-park.
        assert "runs.status = 'awaiting_human'" in park_stmts[0]

    async def test_explicit_grace_seconds_overrides_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        statements: list[str] = []
        engine = _ParkEngine(statements, [])
        monkeypatch.setattr(ra, "get_settings", lambda: self._settings(grace_seconds=999999))
        await ra.park_expired_hitl_runs(engine, grace_seconds=60)  # type: ignore[arg-type]

        park_stmts = [s for s in statements if "status = 'hitl_parked'" in s]
        assert park_stmts
        park_params = [p for p in engine.params_seen if "grace_seconds" in p]
        assert park_params and park_params[0]["grace_seconds"] == 60

    async def test_sweep_failure_raises_with_partial_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        parked_row = _parked_row()
        org2 = OTHER_ORG_ID

        class _PartialParkConn(_ParkConn):
            def __init__(self, fail_on_park: bool) -> None:
                super().__init__([], [parked_row] if not fail_on_park else [], [ORG_ID, org2], [], fail_on_park)

        class _PartialParkEngine:
            def __init__(self) -> None:
                # conn 1 = org enumeration, conn 2 = org 1, conn 3 = org 2.
                self._n = 0

            def connect(self) -> _PartialParkConn:
                self._n += 1
                return _PartialParkConn(fail_on_park=self._n > 2)

        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with pytest.raises(ra.HitlParkError) as excinfo:
            await ra.park_expired_hitl_runs(_PartialParkEngine())  # type: ignore[arg-type]

        assert excinfo.value.parked == 1
        assert str(excinfo.value.__cause__) == "db down"

    async def test_failure_still_logs_already_parked_rows(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Parks achieved before a later org's failure are still logged."""
        parked_row = _parked_row()
        org2 = OTHER_ORG_ID

        class _PartialParkConn(_ParkConn):
            def __init__(self, fail_on_park: bool) -> None:
                super().__init__([], [parked_row] if not fail_on_park else [], [ORG_ID, org2], [], fail_on_park)

        class _PartialParkEngine:
            def __init__(self) -> None:
                self._n = 0

            def connect(self) -> _PartialParkConn:
                self._n += 1
                return _PartialParkConn(fail_on_park=self._n > 2)

        monkeypatch.setattr(ra, "get_settings", lambda: self._settings())
        with caplog.at_level("WARNING"), pytest.raises(ra.HitlParkError):
            await ra.park_expired_hitl_runs(_PartialParkEngine())  # type: ignore[arg-type]

        assert any("hitl_park.parked" in r.message for r in caplog.records)
